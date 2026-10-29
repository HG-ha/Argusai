"""Zero-config decoding of encoded bodies and fields."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os

from argusai.core.security.proxy_decode import auto_decode_body, decode_chain


def _ops(chain: dict) -> list[str]:
    return [layer["op"] for layer in chain["layers"]]


def test_base64_gzip_json_field_is_peeled() -> None:
    inner = json.dumps({"user": "demo", "balance": 9007199254740993}).encode()
    field = base64.b64encode(gzip.compress(inner)).decode()
    body = json.dumps({"code": 0, "data": field}).encode()
    result = auto_decode_body(body, "application/json")
    item = next(i for i in result["items"] if i["location"] == "data")
    assert _ops(item) == ["base64", "gzip", "utf8"]
    assert item["kind"] == "json"
    assert '"balance": 9007199254740993' in item["text"]
    assert item["text"] in result["texts"]


def test_jwt_hex_url_and_unicode_layers() -> None:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(b'{"sub":"42","role":"admin"}').rstrip(b"=").decode()
    jwt = f"{header}.{payload}.c2ln"
    assert _ops(decode_chain(jwt)) == ["jwt"]
    assert '"role": "admin"' in decode_chain(jwt)["text"]
    assert decode_chain(b"hello proxy world!".hex())["text"] == "hello proxy world!"
    assert decode_chain("%7B%22a%22%3A%22%E4%BD%A0%E5%A5%BD%22%7D")["text"] == '{"a":"你好"}'
    assert decode_chain("\\u767b\\u5f55\\u6210\\u529f")["text"] == "登录成功"


def test_unpadded_base64_and_form_fields() -> None:
    value = base64.b64encode(b"user=demo&role=admin").decode().rstrip("=")
    body = f"sign=abc&payload={value}".encode()
    result = auto_decode_body(body, "application/x-www-form-urlencoded")
    assert any(i["location"] == "form.payload" and i["text"] == "user=demo&role=admin" for i in result["items"])


def test_ciphertext_is_flagged_with_block_hint() -> None:
    blob = base64.b64encode(os.urandom(256)).decode()
    result = auto_decode_body(json.dumps({"data": blob}).encode(), "application/json")
    assert result["ciphertext"] and result["ciphertext"][0]["cipher_hint"] == "block16"
    assert result["texts"] == []
    salted = base64.b64encode(b"Salted__" + os.urandom(40)).decode()
    assert decode_chain(salted)["cipher_hint"] == "openssl-salted"


def test_noise_is_not_reported() -> None:
    md5 = hashlib.md5(b"x").hexdigest()
    sha = hashlib.sha256(b"x").hexdigest()
    body = json.dumps({"md5": md5, "sha": sha, "word": "abcdefghijklmnop", "id": "0123456789abcdef"}).encode()
    assert auto_decode_body(body, "application/json")["items"] == []
    page = b"<html><script>var s='a\\u0026b';</script>" + b"x" * 200 + b"</html>"
    assert auto_decode_body(page, "text/html")["items"] == []
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(512)
    assert auto_decode_body(png, "application/octet-stream")["ciphertext"] == []
    assert auto_decode_body(png, "image/png")["items"] == []


def test_query_parameters_are_decoded() -> None:
    token = base64.b64encode(b'{"uid":7,"exp":1893456000}').decode()
    result = auto_decode_body(b"", "", url=f"https://x.test/a?t={token}")
    assert any(i["location"] == "query.t" and '"uid":7' in i["text"] for i in result["items"])
