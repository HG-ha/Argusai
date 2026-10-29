"""Runner: drives a set of checks against one or more targets."""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from argusai.core.check import Check, Registry
from argusai.core.context import Context, ContextSettings
from argusai.core.events import EventBus
from argusai.core.finding import Finding, Severity
from argusai.core.target import Target


@dataclass
class ScanResult:
    target: Target
    findings: list[Finding] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    errors: list[dict] = field(default_factory=list)
    run_dir: Path | None = None

    def by_severity(self) -> dict[str, int]:
        c: dict[str, int] = {s.value: 0 for s in Severity}
        for f in self.findings:
            c[f.severity.value] += 1
        return c


class Runner:
    """Orchestrates checks across targets.

    Typical usage::

        bus = EventBus()
        bus.subscribe(print_event)
        runner = Runner(bus=bus, work_dir=Path("runs/2026"))
        result = await runner.run(target, checks=["recon", "headers"])
    """

    def __init__(
        self,
        bus: EventBus | None = None,
        work_dir: Path | str = "runs",
        settings: ContextSettings | None = None,
    ) -> None:
        self.bus = bus or EventBus()
        self.work_dir = Path(work_dir)
        self.settings = settings or ContextSettings()

    async def run(
        self,
        target: Target,
        checks: Iterable[str] | None = None,
        run_dir: Path | str | None = None,
    ) -> ScanResult:
        # Ensure built-in checks are registered by importing the package.
        import argusai.checks  # noqa: F401

        check_types = Registry.resolve(checks)
        if run_dir is None:
            run_dir = (
                self.work_dir / time.strftime("%Y%m%d-%H%M%S") / _slug(target.name)
            )
        else:
            run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        result = ScanResult(target=target, run_dir=run_dir)

        self.bus.emit(
            "run.start",
            target=target.url,
            target_name=target.name,
            checks=[c.id for c in check_types],
            run_dir=str(run_dir),
        )

        findings_path = run_dir / "findings.jsonl"
        with findings_path.open("w", encoding="utf-8") as fp:
            async with Context(bus=self.bus, work_dir=run_dir, settings=self.settings) as ctx:
                await self._maybe_auth(target, ctx)
                for check_cls in check_types:
                    check = check_cls()
                    self.bus.emit("check.start", check=check.id, name=check.name)
                    t0 = time.time()
                    count = 0
                    try:
                        async for f in check.run(target, ctx):
                            f.target = target.name or target.origin
                            f.check = check.id
                            _annotate_finding_for_agent(f)
                            result.findings.append(f)
                            # Accumulate in shared dict for downstream checks (e.g. ai_poc)
                            ctx.shared.setdefault("_all_findings", []).append(f)
                            fp.write(json.dumps(f.to_dict(), ensure_ascii=False) + "\n")
                            fp.flush()
                            self.bus.emit("finding", finding=f.to_dict())
                            count += 1
                    except Exception as e:
                        tb = traceback.format_exc()
                        result.errors.append(
                            {"check": check.id, "error": str(e), "trace": tb}
                        )
                        self.bus.emit(
                            "check.error", check=check.id, error=str(e), trace=tb
                        )
                    self.bus.emit(
                        "check.done",
                        check=check.id,
                        count=count,
                        elapsed=round(time.time() - t0, 3),
                    )

        result.finished_at = time.time()
        summary = result.by_severity()
        self.bus.emit(
            "run.done",
            target=target.url,
            total=len(result.findings),
            severity=summary,
            elapsed=round(result.finished_at - result.started_at, 3),
            run_dir=str(run_dir),
        )

        # write summary.json for the report layer
        (run_dir / "summary.json").write_text(
            json.dumps(
                {
                    "target": {
                        "url": target.url,
                        "name": target.name,
                        "scope": target.scope,
                    },
                    "checks": [c.id for c in check_types],
                    "severity": summary,
                    "total": len(result.findings),
                    "started_at": result.started_at,
                    "finished_at": result.finished_at,
                    "errors": result.errors,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return result

    async def _maybe_auth(self, target: Target, ctx: Context) -> None:
        """Apply authentication from the target config."""
        if not target.auth or target.auth.mode == "none":
            return
        if target.auth.mode == "static":
            for k, v in target.auth.headers.items():
                ctx.http.headers[k] = v
            for k, v in target.auth.cookies.items():
                ctx.http.cookies.set(k, v)
            ctx.log("applied static auth headers/cookies")
        elif target.auth.mode == "login_form":
            await self._auth_login_form(target, ctx)
        elif target.auth.mode == "script":
            await self._auth_script(target, ctx)

    async def _auth_login_form(self, target: Target, ctx: Context) -> None:
        """Use Playwright to fill a login form and extract the session."""
        auth = target.auth
        if not auth or not auth.login_url:
            ctx.log("login_form auth: no login_url configured, skipping")
            return

        ctx.log(f"login_form auth: navigating to {auth.login_url}")
        browser_ctx = await ctx.browser_context()
        page = await browser_ctx.new_page()
        try:
            await page.goto(auth.login_url, wait_until="domcontentloaded")

            username_sel = auth.selectors.get("username", "#username")
            password_sel = auth.selectors.get("password", "#password")
            submit_sel = auth.selectors.get("submit", "button[type=submit]")

            if auth.username:
                await page.fill(username_sel, auth.username)
            if auth.password:
                await page.fill(password_sel, auth.password)
            await page.click(submit_sel)
            await page.wait_for_load_state("networkidle")

            # Extract cookies from Playwright and apply to httpx client
            cookies = await browser_ctx.cookies()
            for c in cookies:
                ctx.http.cookies.set(c["name"], c["value"], domain=c.get("domain"))

            # Extract any auth headers the app sets (e.g. Bearer token in localStorage)
            extra_headers = await page.evaluate("""() => {
              const h = {};
              const token = localStorage.getItem('token') || localStorage.getItem('access_token')
                || localStorage.getItem('authToken') || sessionStorage.getItem('token');
              if (token) h['Authorization'] = 'Bearer ' + token;
              return h;
            }""")
            for k, v in extra_headers.items():
                ctx.http.headers[k] = v

            # Copy custom user-supplied headers too (they take precedence)
            for k, v in auth.headers.items():
                ctx.http.headers[k] = v
            for k, v in auth.cookies.items():
                ctx.http.cookies.set(k, v)

            ctx.log("login_form auth: session extracted")
        except Exception as e:
            ctx.log(f"login_form auth failed: {e}", level="warn")
        finally:
            await page.close()

    async def _auth_script(self, target: Target, ctx: Context) -> None:
        """Import and run a user-supplied auth script."""
        auth = target.auth
        if not auth or not auth.script:
            ctx.log("script auth: no script configured, skipping")
            return

        module_name, _, callable_name = auth.script.partition(":")
        if not module_name or not callable_name:
            ctx.log("script auth: invalid format, expected module:callable", level="warn")
            return

        ctx.log(f"script auth: importing {module_name}:{callable_name}")
        try:
            import importlib
            mod = importlib.import_module(module_name)
            fn = getattr(mod, callable_name)
            result = await fn(ctx)
            if isinstance(result, dict):
                for k, v in result.get("headers", {}).items():
                    ctx.http.headers[k] = v
                for k, v in result.get("cookies", {}).items():
                    ctx.http.cookies.set(k, v)
            ctx.log("script auth: session applied")
        except Exception as e:
            ctx.log(f"script auth failed: {e}", level="warn")


def _slug(s: str) -> str:
    import re

    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", s).strip("-")
    return s or "target"


def _annotate_finding_for_agent(f: Finding) -> None:
    """Attach non-breaking workflow hints for AI follow-up."""
    discovery_checks = {
        "recon", "js_reverse", "fuzzer", "upload", "identity",
        "http_desync", "headers", "tls", "secrets",
    }
    f.domain = f.domain or "web"
    f.kind = "discovery" if f.check in discovery_checks or f.severity == Severity.INFO else (f.kind or "verification")
    f.confidence = f.confidence or str((f.evidence or {}).get("confidence") or ("suspected" if f.kind == "discovery" else "probable"))

    tools: list[str] = []
    actions: list[str] = []
    fid = f.id or ""
    category = f.category or ""
    if f.url:
        tools.extend(["web_request_parse", "web_request_replay", "web_response_diff"])
        actions.append("Attach this URL/request to a case before deeper validation.")
    if f.check == "js_reverse" or "js_reverse" in fid:
        tools.extend(["reverse_job_create", "reverse_recipe_apply", "js_signature_oracle_plan"])
        actions.append("Capture runtime signing or crypto oracle records in a ReverseJob.")
    if "upload" in fid or f.check == "upload":
        tools.extend(["upload_payload_matrix", "upload_verify_access"])
        actions.append("Verify upload handling with safe marker files and access checks.")
    if "ssrf" in fid or category == "ssrf":
        tools.extend(["oob_payload_generate", "web_request_mutate", "oob_check_events"])
        actions.append("Use OOB correlation for blind SSRF-style validation when in scope.")
    if "idor" in fid or f.check in {"access_control", "business"}:
        tools.extend(["session_vault_list", "auth_matrix_from_vault", "idor_compare_request"])
        actions.append("Compare named sessions or adjacent object ids before declaring access control impact.")
    if "sqli" in fid or category in {"injection", "sqli"}:
        tools.extend(["sqli_probe_param", "sqlmap_plan_scan", "sqlmap_run_scan"])
        actions.append("Confirm SQLi with safe probes or managed sqlmap policy before reporting exploitation.")
    if "cors" in fid or "csrf" in fid or f.check == "identity":
        tools.extend(["cors_matrix_probe", "cors_exploit_verify", "csrf_poc_generate", "oauth_flow_analyze"])
        actions.append("Use identity-specific tools; CORS needs browser fetch verification before confirmed exploitability.")

    seen = set(f.recommended_tools)
    f.recommended_tools.extend([t for t in tools if not (t in seen or seen.add(t))])
    seen_actions = set(f.next_actions)
    f.next_actions.extend([a for a in actions if not (a in seen_actions or seen_actions.add(a))])
