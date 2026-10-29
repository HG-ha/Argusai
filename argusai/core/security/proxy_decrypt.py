"""Rule-driven decryption of captured request/response bodies.

A rule says *which* flows (a proxy-query expression), *where* the ciphertext
lives (whole body, JSON path, form/query field, header) and *how* to turn it
into plaintext — an ordered pipeline of steps over ``bytes``::

    decode(base64) → sym(aes-cbc, key, iv) → decompress(gzip)

Symmetric/RSA steps reuse the ``crypto_reproduce`` primitives; a ``script``
step runs a user ``decrypt(input, ctx)`` JS function in a persistent managed
Node worker for bespoke algorithms. Keys live in user settings only;
:func:`redact_rule` masks them before a rule is shown to the AI.

:class:`ProxyDecryptService` applies enabled rules plus zero-config decoding
(:mod:`proxy_decode`) to every new flow on a background thread, stores the
plaintext on ``TrafficFlow.decoded`` (searchable via ``dec:`` / ``*.body:``)
and feeds it to the passive analyzer.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import queue
import shutil
import subprocess
import threading
import time
import uuid
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote_to_bytes, urlsplit

from argusai.core.security.crypto_reproduce import (
    _do_aes,
    _do_des,
    _do_rc4,
    _do_sm4,
    _from_encoded,
    _xor_stream,
)
from argusai.core.security.proxy_decode import auto_decode_body
from argusai.core.security.proxy_flow import TrafficFlow
from argusai.core.security.proxy_query import CompiledQuery, FlowView, QueryError, _Budget, compile_query

log = logging.getLogger("argusai.proxy.decrypt")

MAX_RULES = 64
MAX_STEPS = 12
MAX_SCRIPT_CHARS = 512 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 256 * 1024
MAX_TARGETS = 16
SCRIPT_TIMEOUT_MS = 2000

STEP_OPS = ("decode", "sym", "rsa", "decompress", "slice", "script")
DECODE_CODECS = ("base64", "base64url", "hex", "url")
SYM_ALGOS = (
    "aes-cbc", "aes-ecb", "aes-ctr", "aes-gcm",
    "des-cbc", "des-ecb", "3des-cbc", "3des-ecb",
    "sm4-cbc", "sm4-ecb", "rc4", "xor",
)
DECOMPRESS_ALGOS = ("gzip", "zlib", "deflate", "zstd", "br")
KEY_FORMATS = ("auto", "utf8", "hex", "base64", "base64url")
APPLY_TO = ("response", "request", "both")
_KEY_SIZES = {"aes": (16, 24, 32), "des": (8,), "3des": (16, 24), "sm4": (16,)}
_IV_SIZES = {"aes": 16, "des": 8, "3des": 8, "sm4": 16}
_SECRET_FIELDS = ("key", "iv", "private_key", "password")
# ``iv: "@prefix"`` takes the IV / nonce from the head of the ciphertext.
IV_FROM_PREFIX = "@prefix"


class RuleError(ValueError):
    pass


class StepError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Rule model
# ---------------------------------------------------------------------------


@dataclass
class DecryptRule:
    id: str = dc_field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    enabled: bool = True
    match: str = ""           # proxy-query expression selecting flows
    apply_to: str = "response"
    # Where the ciphertext lives: "" (whole body), "json.data.payload",
    # "form.p", "query.data", "header.X-Data". A bare path means json.<path>.
    field: str = ""
    steps: list[dict[str, Any]] = dc_field(default_factory=list)
    note: str = ""
    updated_at: float = dc_field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "match": self.match,
            "apply_to": self.apply_to,
            "field": self.field,
            "steps": [dict(s) for s in self.steps],
            "note": self.note,
            "updated_at": self.updated_at,
        }

    @property
    def has_script(self) -> bool:
        return any(str(s.get("op") or "").lower() == "script" for s in self.steps)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DecryptRule":
        if not isinstance(raw, dict):
            raise RuleError("规则必须是对象")
        apply_to = str(raw.get("apply_to") or "response").lower()
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex[:12])[:32],
            name=str(raw.get("name") or "").strip()[:80],
            enabled=bool(raw.get("enabled", True)),
            match=str(raw.get("match") or "").strip(),
            apply_to=apply_to if apply_to in APPLY_TO else "response",
            field=str(raw.get("field") or "").strip(),
            steps=[_normalize_step(s) for s in (raw.get("steps") or []) if isinstance(s, dict)],
            note=str(raw.get("note") or "")[:2000],
            updated_at=float(raw.get("updated_at") or time.time()),
        )


def _normalize_step(raw: dict[str, Any]) -> dict[str, Any]:
    step = {k: v for k, v in raw.items() if v is not None}
    step["op"] = str(step.get("op") or "").strip().lower()
    for key in ("codec", "algo", "key_format", "iv_format", "padding"):
        if key in step:
            step[key] = str(step[key]).strip().lower()
    return step


def redact_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Rule dict with key material masked, for the AI bridge and logs."""
    out = dict(rule)
    steps = []
    for step in rule.get("steps") or []:
        masked = dict(step)
        for key in _SECRET_FIELDS:
            value = masked.get(key)
            if isinstance(value, str) and value and value != IV_FROM_PREFIX:
                masked[key] = _mask(value)
        if isinstance(masked.get("code"), str) and len(masked["code"]) > 400:
            masked["code"] = masked["code"][:400] + f"…(+{len(masked['code']) - 400} chars)"
        steps.append(masked)
    out["steps"] = steps
    return out


def _mask(value: str) -> str:
    if len(value) <= 6:
        return "***"
    return f"{value[:2]}***{value[-2:]} ({len(value)} chars)"


def validate_rule(rule: DecryptRule) -> None:
    """Raise :class:`RuleError` with a user-facing message when malformed."""
    if not rule.name:
        raise RuleError("请填写规则名称")
    if not rule.match:
        raise RuleError("请填写匹配表达式，例如 host:api.example.com path:/v2/")
    try:
        compile_query(rule.match)
    except QueryError as exc:
        raise RuleError(f"匹配表达式有误：{exc}") from exc
    if rule.field:
        kind, _, rest = rule.field.partition(".")
        if kind in ("form", "query", "header") and not rest:
            raise RuleError(f"字段 {rule.field} 缺少名称，例如 {kind}.data")
    if not rule.steps:
        raise RuleError("规则至少需要一个步骤")
    if len(rule.steps) > MAX_STEPS:
        raise RuleError(f"步骤过多（上限 {MAX_STEPS}）")
    for idx, step in enumerate(rule.steps, 1):
        _validate_step(idx, step)


def _validate_step(idx: int, step: dict[str, Any]) -> None:
    op = step.get("op")
    where = f"第 {idx} 步"
    if op not in STEP_OPS:
        raise RuleError(f"{where}：未知操作 {op!r}，可用 {', '.join(STEP_OPS)}")
    if op == "decode" and step.get("codec") not in DECODE_CODECS:
        raise RuleError(f"{where}：decode 的 codec 需为 {', '.join(DECODE_CODECS)}")
    if op == "decompress" and step.get("algo") not in DECOMPRESS_ALGOS:
        raise RuleError(f"{where}：decompress 的 algo 需为 {', '.join(DECOMPRESS_ALGOS)}")
    if op == "slice":
        for key in ("start", "end"):
            if key in step and step[key] not in ("", None):
                try:
                    int(step[key])
                except (TypeError, ValueError) as exc:
                    raise RuleError(f"{where}：slice 的 {key} 需为整数") from exc
    if op == "script":
        code = str(step.get("code") or "")
        if "decrypt" not in code:
            raise RuleError(f"{where}：脚本需要定义 function decrypt(input, ctx)")
        if len(code) > MAX_SCRIPT_CHARS:
            raise RuleError(f"{where}：脚本过长（上限 {MAX_SCRIPT_CHARS // 1024} KB）")
    if op == "rsa" and "BEGIN" not in str(step.get("private_key") or ""):
        raise RuleError(f"{where}：rsa 需要 PEM 格式的 private_key")
    if op == "sym":
        algo = step.get("algo")
        if algo not in SYM_ALGOS:
            raise RuleError(f"{where}：sym 不支持算法 {algo!r}，可用 {', '.join(SYM_ALGOS)}")
        for fmt_key in ("key_format", "iv_format"):
            if step.get(fmt_key, "auto") not in KEY_FORMATS:
                raise RuleError(f"{where}：{fmt_key} 需为 {', '.join(KEY_FORMATS)}")
        if not step.get("key"):
            raise RuleError(f"{where}：{algo} 缺少 key")
        family = algo.split("-", 1)[0]
        try:
            key = _from_encoded(step.get("key"), step.get("key_format", "auto"))
        except Exception as exc:
            raise RuleError(f"{where}：key 无法按 {step.get('key_format', 'auto')} 解析：{exc}") from exc
        sizes = _KEY_SIZES.get(family)
        if sizes and len(key) not in sizes:
            raise RuleError(f"{where}：{algo} 的 key 应为 {'/'.join(map(str, sizes))} 字节，当前 {len(key)} 字节")
        needs_iv = algo.endswith(("-cbc", "-ctr", "-gcm"))
        iv = str(step.get("iv") or "")
        if needs_iv and not iv:
            raise RuleError(f"{where}：{algo} 需要 iv（或填 {IV_FROM_PREFIX} 表示 IV 在密文开头）")
        if needs_iv and iv != IV_FROM_PREFIX and not algo.endswith("-gcm"):
            try:
                iv_bytes = _from_encoded(iv, step.get("iv_format", "auto"))
            except Exception as exc:
                raise RuleError(f"{where}：iv 无法解析：{exc}") from exc
            expected = _IV_SIZES.get(family)
            if expected and len(iv_bytes) != expected:
                raise RuleError(f"{where}：{algo} 的 iv 应为 {expected} 字节，当前 {len(iv_bytes)} 字节")


# ---------------------------------------------------------------------------
# Locating ciphertext
# ---------------------------------------------------------------------------


def _json_walk(node: Any, parts: list[str]) -> list[tuple[str, Any]]:
    """``data.items[*].enc`` style paths; returns ``(concrete path, value)``."""
    results: list[tuple[str, Any]] = [("", node)]
    for part in parts:
        nxt: list[tuple[str, Any]] = []
        name, _, index = part.partition("[")
        for path, value in results:
            if name:
                if not isinstance(value, dict) or name not in value:
                    continue
                path, value = (f"{path}.{name}" if path else name), value[name]
            if index:
                idx = index.rstrip("]")
                if not isinstance(value, list):
                    continue
                if idx == "*":
                    nxt.extend((f"{path}[{i}]", item) for i, item in enumerate(value[:MAX_TARGETS]))
                    continue
                try:
                    i = int(idx)
                    nxt.append((f"{path}[{i}]", value[i]))
                except (ValueError, IndexError):
                    continue
                continue
            nxt.append((path, value))
        results = nxt
    return results


def extract_targets(flow: TrafficFlow, part: str, field_spec: str) -> list[tuple[str, bytes]]:
    """``(location, ciphertext bytes)`` pairs a rule should decrypt in one part."""
    body = flow.request_body if part == "request" else flow.response_body
    spec = (field_spec or "").strip()
    if not spec:
        return [("<body>", body)] if body else []
    kind, _, name = spec.partition(".")
    if kind == "query":
        if part != "request":
            return []
        return [(f"query.{k}", v.encode("utf-8")) for k, v in parse_qsl(urlsplit(flow.url).query) if k == name][:MAX_TARGETS]
    if kind == "header":
        items = flow.raw_request_header_items if part == "request" else flow.raw_response_header_items
        return [(f"header.{k}", str(v).encode("utf-8")) for k, v in items if k.lower() == name.lower()][:MAX_TARGETS]
    if kind == "form":
        text = body.decode("utf-8", errors="replace")
        return [(f"form.{k}", v.encode("utf-8")) for k, v in parse_qsl(text, keep_blank_values=True) if k == name][:MAX_TARGETS]
    path = name if kind == "json" else spec
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    out: list[tuple[str, bytes]] = []
    for location, value in _json_walk(doc, [p for p in path.split(".") if p]):
        if value is None:
            continue
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        out.append((f"json.{location}", raw.encode("utf-8")))
    return out[:MAX_TARGETS]


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _decode(data: bytes, codec: str) -> bytes:
    text = data.decode("utf-8", errors="replace").strip()
    try:
        if codec == "hex":
            return binascii.unhexlify("".join(text.removeprefix("0x").split()))
        if codec == "base64":
            compact = "".join(text.split()).replace("-", "+").replace("_", "/")
            return base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True)
        if codec == "base64url":
            compact = "".join(text.split())
            return base64.urlsafe_b64decode(compact + "=" * (-len(compact) % 4))
        if codec == "url":
            return unquote_to_bytes(text)
    except (binascii.Error, ValueError) as exc:
        raise StepError(f"{codec} 解码失败：{exc}") from exc
    raise StepError(f"未知编码 {codec}")


def _sym(data: bytes, step: dict[str, Any]) -> bytes:
    algo = step["algo"]
    family, _, mode = algo.partition("-")
    key = _from_encoded(step.get("key", ""), step.get("key_format", "auto"))
    iv_spec = str(step.get("iv") or "")
    iv = b""
    if iv_spec == IV_FROM_PREFIX:
        size = 12 if mode == "gcm" else _IV_SIZES.get(family, 16)
        if len(data) <= size:
            raise StepError(f"密文太短，取不出 {size} 字节的 IV")
        iv, data = data[:size], data[size:]
    elif iv_spec:
        iv = _from_encoded(iv_spec, step.get("iv_format", "auto"))
    no_padding = step.get("padding") == "none"
    if algo == "xor":
        return _xor_stream(data, key)
    if algo == "rc4":
        return _do_rc4(key, data)
    if family == "aes":
        return _do_aes("decrypt", key, iv, mode, data, no_padding=no_padding)
    if family == "des":
        return _do_des("decrypt", key, iv, mode, data, triple=False, no_padding=no_padding)
    if family == "3des":
        return _do_des("decrypt", key, iv, mode, data, triple=True, no_padding=no_padding)
    if family == "sm4":
        return _do_sm4("decrypt", key, iv, mode, data, no_padding=no_padding)
    raise StepError(f"不支持的算法 {algo}")


def _rsa(data: bytes, step: dict[str, Any]) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    password = str(step.get("password") or "")
    key = serialization.load_pem_private_key(
        str(step.get("private_key") or "").encode("utf-8"),
        password=password.encode("utf-8") if password else None,
    )
    scheme = str(step.get("padding") or "pkcs1").lower()
    if scheme.startswith("oaep"):
        digest = hashes.SHA256() if scheme == "oaep-sha256" else hashes.SHA1()
        pad: Any = padding.OAEP(mgf=padding.MGF1(algorithm=digest), algorithm=digest, label=None)
    else:
        pad = padding.PKCS1v15()
    block = key.key_size // 8
    if len(data) % block:
        raise StepError(f"RSA 密文长度 {len(data)} 不是密钥长度 {block} 的整数倍")
    # Long payloads are commonly encrypted block by block.
    return b"".join(key.decrypt(data[i:i + block], pad) for i in range(0, len(data), block))


def _decompress(data: bytes, algo: str) -> bytes:
    try:
        if algo == "gzip":
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, MAX_OUTPUT_BYTES)
        if algo in ("zlib", "deflate"):
            try:
                return zlib.decompressobj(zlib.MAX_WBITS).decompress(data, MAX_OUTPUT_BYTES)
            except zlib.error:
                return zlib.decompressobj(-zlib.MAX_WBITS).decompress(data, MAX_OUTPUT_BYTES)
        if algo == "zstd":
            import io

            import zstandard

            reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True)
            return reader.read(MAX_OUTPUT_BYTES)
        if algo == "br":
            try:
                import brotli  # type: ignore[import-not-found]
            except ImportError as exc:
                raise StepError("未安装 brotli 库，无法解压 br") from exc
            return brotli.decompress(data)[:MAX_OUTPUT_BYTES]
    except StepError:
        raise
    except Exception as exc:
        raise StepError(f"{algo} 解压失败：{exc}") from exc
    raise StepError(f"未知压缩算法 {algo}")


ScriptRunner = Callable[[str, bytes, dict[str, Any]], bytes]


@dataclass
class PipelineResult:
    ok: bool
    output: bytes = b""
    error: str = ""
    failed_step: int = 0
    trace: list[dict[str, Any]] = dc_field(default_factory=list)

    def text(self) -> str | None:
        """UTF-8 text when the plaintext is printable, else None."""
        try:
            text = self.output.decode("utf-8")
        except UnicodeDecodeError:
            return None
        printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
        return text if not text or printable >= len(text) * 0.9 else None


def run_pipeline(
    data: bytes,
    steps: list[dict[str, Any]],
    *,
    script_runner: ScriptRunner | None = None,
    ctx: dict[str, Any] | None = None,
) -> PipelineResult:
    current = data
    trace: list[dict[str, Any]] = []
    for idx, step in enumerate(steps, 1):
        op = step.get("op")
        label = str(step.get("codec") or step.get("algo") or op)
        try:
            if op == "decode":
                current = _decode(current, step["codec"])
            elif op == "sym":
                current = _sym(current, step)
            elif op == "rsa":
                current = _rsa(current, step)
            elif op == "decompress":
                current = _decompress(current, step["algo"])
            elif op == "slice":
                start = int(step.get("start") or 0)
                end = step.get("end")
                current = current[start:int(end)] if end not in ("", None) else current[start:]
            elif op == "script":
                if script_runner is None:
                    raise StepError("脚本步骤需要 Node.js 运行时")
                current = script_runner(str(step.get("code") or ""), current, dict(ctx or {}))
            else:
                raise StepError(f"未知操作 {op}")
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            trace.append({"step": idx, "op": op, "label": label, "ok": False, "error": message})
            return PipelineResult(False, current, f"第 {idx} 步（{label}）：{message}", idx, trace)
        current = current[:MAX_OUTPUT_BYTES]
        trace.append({"step": idx, "op": op, "label": label, "ok": True, "size": len(current)})
    return PipelineResult(True, current, trace=trace)


# ---------------------------------------------------------------------------
# Node script worker
# ---------------------------------------------------------------------------

# One long-lived Node process; each rule's code is compiled once into its own
# vm context. ``vm`` is NOT a security boundary — script rules are trusted
# code, which is why the agent tool that saves them always needs confirmation.
_WORKER_JS = r"""'use strict';
const vm = require('vm');
const crypto = require('crypto');
const zlib = require('zlib');
const readline = require('readline');
const contexts = new Map();
function contextFor(code) {
  let ctx = contexts.get(code);
  if (ctx) return ctx;
  ctx = vm.createContext({
    Buffer, crypto, zlib, TextEncoder, TextDecoder, URLSearchParams, atob, btoa,
    console: { log() {}, warn() {}, error() {} },
  });
  new vm.Script(code, { filename: 'proxy-decrypt-rule.js' }).runInContext(ctx, { timeout: 2000 });
  if (typeof ctx.decrypt !== 'function') throw new Error('script must define function decrypt(input, ctx)');
  contexts.set(code, ctx);
  if (contexts.size > 32) contexts.delete(contexts.keys().next().value);
  return ctx;
}
const call = new vm.Script('decrypt(__argusInput, __argusCtx)');
readline.createInterface({ input: process.stdin }).on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  const reply = (body) => process.stdout.write(JSON.stringify(Object.assign({ id: msg.id }, body)) + '\n');
  try {
    const ctx = contextFor(msg.code);
    ctx.__argusInput = Buffer.from(msg.input || '', 'base64');
    ctx.__argusCtx = msg.ctx || {};
    const out = call.runInContext(ctx, { timeout: msg.timeout || 2000 });
    if (out && typeof out.then === 'function') throw new Error('decrypt must return synchronously');
    let bytes;
    if (out === undefined || out === null) bytes = Buffer.alloc(0);
    else if (typeof out === 'string') bytes = Buffer.from(out, 'utf8');
    else if (ArrayBuffer.isView(out)) bytes = Buffer.from(out.buffer, out.byteOffset, out.byteLength);
    else bytes = Buffer.from(JSON.stringify(out), 'utf8');
    reply({ ok: true, output: bytes.toString('base64') });
  } catch (e) {
    reply({ ok: false, error: String((e && e.message) || e) });
  }
});
"""


class NodeScriptRunner:
    """Run ``decrypt(input, ctx)`` rule scripts in a persistent Node worker."""

    def __init__(
        self,
        node_resolver: Callable[[], str] | None = None,
        work_dir: Path | None = None,
        timeout_ms: int = SCRIPT_TIMEOUT_MS,
    ) -> None:
        self.node_resolver = node_resolver
        self.work_dir = work_dir
        self.timeout_ms = timeout_ms
        self._proc: subprocess.Popen[bytes] | None = None
        self._replies: queue.Queue[dict[str, Any]] = queue.Queue()
        self._lock = threading.Lock()
        self._next_id = 0

    def _node(self) -> str:
        if self.node_resolver is not None:
            try:
                path = self.node_resolver()
                if path:
                    return path
            except Exception as exc:
                log.debug("managed node unavailable: %s", exc)
        path = shutil.which("node")
        if not path:
            raise StepError("找不到 Node.js：请在 设置 → 运行时 安装托管 Node 环境后再用脚本解密")
        return path

    def _start(self) -> subprocess.Popen[bytes]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        import tempfile

        from argusai.core.infra.win_stdio import quiet_subprocess_kwargs

        work = self.work_dir or Path(tempfile.gettempdir()) / "argusai-proxy-decrypt"
        work.mkdir(parents=True, exist_ok=True)
        worker = work / "decrypt-worker.js"
        if not worker.is_file() or worker.read_text(encoding="utf-8") != _WORKER_JS:
            worker.write_text(_WORKER_JS, encoding="utf-8")
        try:
            from argusai.core.reverse.runtime_manager import node_process_env

            env = node_process_env(work)
        except Exception:
            import os

            env = dict(os.environ)
        for key in ("NODE_OPTIONS", "NODE_REPL_HISTORY"):
            env.pop(key, None)
        self._proc = subprocess.Popen(
            [self._node(), str(worker)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(work),
            env=env,
            **quiet_subprocess_kwargs(),
        )
        self._replies = queue.Queue()
        threading.Thread(target=self._read_loop, args=(self._proc, self._replies), daemon=True,
                         name="proxy-decrypt-node").start()
        return self._proc

    @staticmethod
    def _read_loop(proc: subprocess.Popen[bytes], replies: queue.Queue[dict[str, Any]]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                replies.put(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue

    def __call__(self, code: str, data: bytes, ctx: dict[str, Any]) -> bytes:
        with self._lock:
            proc = self._start()
            self._next_id += 1
            msg_id = self._next_id
            payload = {
                "id": msg_id,
                "code": code,
                "input": base64.b64encode(data).decode("ascii"),
                "ctx": ctx,
                "timeout": self.timeout_ms,
            }
            try:
                assert proc.stdin is not None
                proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
                proc.stdin.flush()
            except OSError as exc:
                self.close()
                raise StepError(f"Node 工作进程已退出：{exc}") from exc
            deadline = time.monotonic() + self.timeout_ms / 1000 + 3
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # vm's timeout covers sync loops; this catches a wedged worker.
                    self.close()
                    raise StepError("脚本执行超时")
                try:
                    reply = self._replies.get(timeout=remaining)
                except queue.Empty:
                    continue
                if reply.get("id") == msg_id:
                    break
        if not reply.get("ok"):
            raise StepError(str(reply.get("error") or "脚本出错"))
        return base64.b64decode(reply.get("output") or "")

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

_MAX_TEXTS_CHARS = 512 * 1024


@dataclass
class _CompiledRule:
    rule: DecryptRule
    query: CompiledQuery


def _rule_matches(compiled: _CompiledRule, flow: TrafficFlow) -> bool:
    budget = _Budget(deadline=time.monotonic() + 1.0)
    return bool(compiled.query.predicate(FlowView(flow), budget))


def _apply_rule(
    rule: DecryptRule,
    flow: TrafficFlow,
    script_runner: ScriptRunner | None,
) -> dict[str, list[dict[str, Any]]]:
    """Run one rule over the parts it targets; ``{part: [item, ...]}``."""
    parts = ("request", "response") if rule.apply_to == "both" else (rule.apply_to,)
    out: dict[str, list[dict[str, Any]]] = {}
    for part in parts:
        for location, data in extract_targets(flow, part, rule.field):
            ctx = {
                "part": part,
                "location": location,
                "url": flow.url,
                "method": flow.method,
                "status": flow.response_status,
                "requestHeaders": dict(flow.request_headers),
                "responseHeaders": dict(flow.response_headers),
            }
            result = run_pipeline(data, rule.steps, script_runner=script_runner if rule.has_script else None, ctx=ctx)
            item: dict[str, Any] = {
                "source": "rule",
                "rule_id": rule.id,
                "rule_name": rule.name,
                "location": location,
                "ok": result.ok,
                "trace": result.trace,
            }
            if not result.ok:
                item["error"] = result.error
            else:
                text = result.text()
                if text is not None:
                    item["text"] = text[:MAX_TEXT_CHARS]
                    item["truncated"] = len(text) > MAX_TEXT_CHARS
                    item["kind"] = "json" if text.lstrip()[:1] in ("{", "[") else "text"
                else:
                    item["kind"] = "binary"
                    item["size"] = len(result.output)
                    item["hex_preview"] = result.output[:256].hex()
            out.setdefault(part, []).append(item)
    return out


class ProxyDecryptService:
    """Decode / decrypt captured flows on a background thread."""

    def __init__(self, node_resolver: Callable[[], str] | None = None) -> None:
        self.script_runner = NodeScriptRunner(node_resolver)
        self.auto_decode = True
        # Hooks set by the manager: passive analysis of plaintext and history
        # memory accounting for the stored plaintext.
        self.analyze: Callable[[TrafficFlow], None] | None = None
        self.on_decoded: Callable[[TrafficFlow, int], None] | None = None
        self._rules: list[_CompiledRule] = []
        self._rule_errors: dict[str, str] = {}
        self._version = 0
        self._seq = 0
        self._queue: queue.Queue[tuple[int, TrafficFlow] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def seq(self) -> int:
        """Bumps whenever any flow's decoded view changes (UI refresh cue)."""
        return self._seq

    @property
    def rule_errors(self) -> dict[str, str]:
        return dict(self._rule_errors)

    def set_rules(self, raw_rules: Iterable[dict[str, Any]], flows: Iterable[TrafficFlow] = ()) -> None:
        compiled: list[_CompiledRule] = []
        errors: dict[str, str] = {}
        for raw in list(raw_rules or [])[:MAX_RULES]:
            try:
                rule = DecryptRule.from_dict(raw)
                validate_rule(rule)
            except RuleError as exc:
                errors[str((raw or {}).get("id") or "?")] = str(exc)
                continue
            if rule.enabled:
                compiled.append(_CompiledRule(rule, compile_query(rule.match)))
        with self._lock:
            self._rules = compiled
            self._rule_errors = errors
            self._version += 1
        for flow in flows:
            self.submit(flow)

    def submit(self, flow: TrafficFlow) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, daemon=True, name="proxy-decrypt")
            self._thread.start()
        self._queue.put((self._version, flow))

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            version, flow = item
            if version != self._version:
                continue  # set_rules re-submitted the history with the new rules
            try:
                self.process(flow)
            except Exception:
                log.debug("proxy decrypt failed for flow %s", flow.id, exc_info=True)

    def drain(self, timeout: float = 5.0) -> None:
        """Block until queued work is done (tests and one-shot tools)."""
        deadline = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.05)

    def decode_flow(self, flow: TrafficFlow, rules: list[_CompiledRule] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            part: {"items": [], "texts": [], "ciphertext": []} for part in ("request", "response")
        }
        if flow.body_evicted:
            return result
        if self.auto_decode:
            for part in ("request", "response"):
                body = flow.request_body if part == "request" else flow.response_body
                ct = flow._first_request_content_type() if part == "request" else flow.content_type
                auto = auto_decode_body(body, ct, url=flow.url if part == "request" else "")
                for item in auto["items"]:
                    result[part]["items"].append({"source": "auto", **item})
                result[part]["texts"].extend(auto["texts"])
                result[part]["ciphertext"] = auto["ciphertext"]
        for compiled in self._rules if rules is None else rules:
            if not _rule_matches(compiled, flow):
                continue
            for part, items in _apply_rule(compiled.rule, flow, self.script_runner).items():
                result[part]["items"].extend(items)
                result[part]["texts"].extend(i["text"] for i in items if i.get("text"))
        for part in ("request", "response"):
            budget = _MAX_TEXTS_CHARS
            kept: list[str] = []
            for text in result[part]["texts"]:
                if budget <= 0:
                    break
                kept.append(text[:budget])
                budget -= len(text)
            result[part]["texts"] = kept
        return result

    def process(self, flow: TrafficFlow) -> None:
        before = flow.decoded_bytes()
        decoded = self.decode_flow(flow)
        has_content = any(decoded[p]["items"] for p in ("request", "response"))
        if not has_content and flow.decoded is None:
            return
        flow.decoded = decoded if has_content else None
        with self._lock:
            self._seq += 1
            flow.decode_seq = self._seq
        if self.on_decoded is not None:
            self.on_decoded(flow, flow.decoded_bytes() - before)
        if self.analyze is not None and flow.decrypted:
            req_text = "\n".join(decoded["request"]["texts"])
            resp_text = "\n".join(decoded["response"]["texts"])
            shadow = replace(
                flow,
                request_body=req_text.encode("utf-8") if req_text else flow.request_body,
                response_body=resp_text.encode("utf-8") if resp_text else flow.response_body,
                decoded=None,
            )
            self.analyze(shadow)

    def test_rule(self, raw_rule: dict[str, Any], flow: TrafficFlow) -> dict[str, Any]:
        """Dry-run one rule on one flow without saving it."""
        try:
            rule = DecryptRule.from_dict(raw_rule)
            validate_rule(rule)
        except RuleError as exc:
            return {"ok": False, "error": str(exc)}
        compiled = _CompiledRule(rule, compile_query(rule.match))
        items = _apply_rule(rule, flow, self.script_runner)
        targets = sum(len(v) for v in items.values())
        return {
            "ok": True,
            "matched": _rule_matches(compiled, flow),
            "targets": targets,
            "items": items,
            "hint": "" if targets else "没有在指定位置找到内容，请检查 apply_to 与 field",
        }

    def close(self) -> None:
        self._queue.put(None)
        self.script_runner.close()
