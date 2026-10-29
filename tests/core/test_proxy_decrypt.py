"""Decryption rules: validation, field targeting, pipelines, scripts, service."""

from __future__ import annotations

import base64
import gzip
import json
import shutil
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from argusai.core.security.crypto_reproduce import _do_aes, _do_sm4
from argusai.core.security.proxy_decrypt import (
    DecryptRule,
    NodeScriptRunner,
    ProxyDecryptService,
    RuleError,
    extract_targets,
    redact_rule,
    run_pipeline,
    validate_rule,
)
from argusai.core.security.proxy_flow import TrafficFlow
from argusai.core.security.proxy_query import compile_query, run_query

KEY = "0123456789abcdef"
IV = "fedcba9876543210"


def _aes_b64(plain: bytes) -> str:
    return base64.b64encode(_do_aes("encrypt", KEY.encode(), IV.encode(), "cbc", plain)).decode()


def _flow(fid: int, url: str, resp: bytes, *, method: str = "POST", req: bytes = b"") -> TrafficFlow:
    return TrafficFlow(
        id=fid,
        timestamp=time.time(),
        method=method,
        url=url,
        host=url.split("/")[2],
        response_status=200,
        request_body=req,
        response_body=resp,
        content_type="application/json",
        response_headers={"content-type": "application/json"},
        raw_response_header_items=[("Content-Type", "application/json")],
    )


AES_RULE = {
    "name": "app aes",
    "match": "host:api.app.test",
    "apply_to": "response",
    "field": "data",
    "steps": [
        {"op": "decode", "codec": "base64"},
        {"op": "sym", "algo": "aes-cbc", "key": KEY, "key_format": "utf8", "iv": IV, "iv_format": "utf8"},
        {"op": "decompress", "algo": "gzip"},
    ],
}


def test_validation_messages_are_actionable() -> None:
    bad_key = json.loads(json.dumps(AES_RULE))
    bad_key["steps"][1]["key"] = "short"
    with pytest.raises(RuleError, match="16/24/32 字节"):
        validate_rule(DecryptRule.from_dict(bad_key))
    with pytest.raises(RuleError, match="匹配表达式"):
        validate_rule(DecryptRule.from_dict({**AES_RULE, "match": "hots:x"}))
    with pytest.raises(RuleError, match="需要 iv"):
        validate_rule(DecryptRule.from_dict({**AES_RULE, "steps": [{"op": "sym", "algo": "aes-cbc", "key": KEY, "key_format": "utf8"}]}))
    validate_rule(DecryptRule.from_dict(AES_RULE))


def test_field_targets() -> None:
    flow = _flow(1, "https://api.app.test/a?q=dGVzdA%3D%3D", b'{"data":"X","list":[{"enc":"A"},{"enc":"B"}]}',
                 req=b"p=abc&x=1")
    assert extract_targets(flow, "response", "data") == [("json.data", b"X")]
    assert [v for _, v in extract_targets(flow, "response", "json.list[*].enc")] == [b"A", b"B"]
    assert extract_targets(flow, "request", "form.p") == [("form.p", b"abc")]
    assert extract_targets(flow, "request", "query.q") == [("query.q", b"dGVzdA==")]
    assert extract_targets(flow, "response", "") == [("<body>", flow.response_body)]


def test_service_decrypts_and_feeds_query_engine() -> None:
    plain = json.dumps({"orderId": "9007199254740993", "token": "eyJ.x"}).encode()
    body = json.dumps({"code": 0, "data": _aes_b64(gzip.compress(plain))}).encode()
    flow = _flow(7, "https://api.app.test/order", body)
    other = _flow(8, "https://cdn.other.test/x", body)
    service = ProxyDecryptService()
    service.set_rules([AES_RULE])
    for f in (flow, other):
        service.process(f)
    item = next(i for i in flow.decoded["response"]["items"] if i["source"] == "rule")
    assert item["ok"] and item["kind"] == "json" and "9007199254740993" in item["text"]
    assert run_query(compile_query("dec.resp:orderId"), [flow, other]).ids == [7]
    assert run_query(compile_query("has:decrypted"), [flow, other]).ids == [7]
    assert flow.to_dict()["decrypted"] is True


def test_sm4_iv_prefix_and_rsa_blocks() -> None:
    key = b"k" * 16
    iv = b"i" * 16
    ct = iv + _do_sm4("encrypt", key, iv, "cbc", b"sm4 plaintext")
    steps = [{"op": "sym", "algo": "sm4-cbc", "key": key.hex(), "key_format": "hex", "iv": "@prefix"}]
    assert run_pipeline(ct, steps).output == b"sm4 plaintext"

    private = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    blob = b"".join(private.public_key().encrypt(chunk, oaep) for chunk in (b"part-one|", b"part-two"))
    result = run_pipeline(blob, [{"op": "rsa", "private_key": pem, "padding": "oaep-sha256"}])
    assert result.ok and result.output == b"part-one|part-two"


def test_failures_report_the_step() -> None:
    result = run_pipeline(b"!!!", [{"op": "decode", "codec": "base64"}])
    assert not result.ok and result.failed_step == 1 and "base64" in result.error


def test_redact_hides_keys() -> None:
    masked = redact_rule(AES_RULE)
    assert KEY not in json.dumps(masked) and IV not in json.dumps(masked)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_script_step_runs_in_node_worker() -> None:
    runner = NodeScriptRunner()
    try:
        code = """
        function decrypt(input, ctx) {
          const key = Buffer.from('0123456789abcdef');
          const d = crypto.createDecipheriv('aes-128-cbc', key, Buffer.from('fedcba9876543210'));
          return Buffer.concat([d.update(input), d.final()]).toString('utf8') + '|' + ctx.method;
        }"""
        ct = base64.b64decode(_aes_b64(b"from node"))
        steps = [{"op": "script", "code": code}]
        assert run_pipeline(ct, steps, script_runner=runner, ctx={"method": "POST"}).output == b"from node|POST"
        hang = "function decrypt(){ while(true){} }"
        failed = run_pipeline(b"x", [{"op": "script", "code": hang}], script_runner=runner)
        assert not failed.ok and "timed out" in failed.error.lower()
        # The worker survives a timed-out script.
        assert run_pipeline(ct, steps, script_runner=runner, ctx={"method": "GET"}).ok
    finally:
        runner.close()
