"""Local + MinerU document parsing tool."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from argusai.core.settings import AppSettings, _parse
from argusai.core.tools import document_parse
from argusai.gui.settings import SettingsMixin, _merge_document_parse_settings

# Minimal PDF with extractable Helvetica text (pypdf-friendly).
_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj
2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj
3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources<< /Font<< /F1 5 0 R >> >> >>endobj
4 0 obj<< /Length 51 >>stream
BT /F1 12 Tf 72 72 Td (Hello Argus PDF) Tj ET
endstream
endobj
5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000266 00000 n 
0000000367 00000 n 
trailer<< /Size 6 /Root 1 0 R >>
startxref
444
%%EOF
"""


class _FakeSettingsMgr:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    def get(self) -> AppSettings:
        return self._settings


class _FakeApi:
    def __init__(self, settings: AppSettings) -> None:
        self.settings_mgr = _FakeSettingsMgr(settings)


def _write_docx(path: Path, text: str = "Hello Argus DOCX") -> None:
    from docx import Document

    doc = Document()
    doc.add_paragraph(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "sandbox"
    root.mkdir()
    monkeypatch.setattr(document_parse, "_session_sandbox_root", lambda api, kwargs: root)
    return root


def test_parse_pdf_and_docx_local(sandbox: Path, tmp_path: Path) -> None:
    settings = AppSettings(data_root=str(tmp_path / "data"))
    api = _FakeApi(settings)
    pdf = sandbox / "_attachments" / "a.pdf"
    docx = sandbox / "_attachments" / "b.docx"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(_MINIMAL_PDF)
    _write_docx(docx)

    pdf_r = document_parse.parse_document(api, path="_attachments/a.pdf", engine="local")
    assert pdf_r["ok"] is True
    assert pdf_r["engine"] == "local"
    assert "Hello Argus PDF" in pdf_r["markdown"]

    docx_r = document_parse.parse_document(api, path="_attachments/b.docx", engine="local")
    assert docx_r["ok"] is True
    assert "Hello Argus DOCX" in docx_r["markdown"]


def test_local_rejects_png_without_mineru(sandbox: Path, tmp_path: Path) -> None:
    settings = AppSettings(data_root=str(tmp_path / "data"))
    api = _FakeApi(settings)
    png = sandbox / "_attachments" / "scan.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    r = document_parse.parse_document(api, path="_attachments/scan.png", engine="auto")
    assert r["ok"] is False
    assert "MinerU" in r["error"]


def test_mineru_agent_upload_flow(
    sandbox: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings(
        data_root=str(tmp_path / "data"),
        document_parse_mineru_enabled=True,
        document_parse_mineru_base_url="https://mineru.test",
    )
    api = _FakeApi(settings)
    pdf = sandbox / "_attachments" / "scan.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(_MINIMAL_PDF)

    poll_hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/api/v1/agent/parse/file"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"task_id": "t1", "file_url": "https://oss.test/u"}},
            )
        if request.method == "PUT" and url.startswith("https://oss.test/u"):
            return httpx.Response(200)
        if request.method == "GET" and url.endswith("/api/v1/agent/parse/t1"):
            poll_hits["n"] += 1
            if poll_hits["n"] == 1:
                return httpx.Response(200, json={"code": 0, "data": {"state": "running"}})
            return httpx.Response(
                200,
                json={"code": 0, "data": {"state": "done", "markdown_url": "https://cdn.test/out.md"}},
            )
        if request.method == "GET" and url.startswith("https://cdn.test/out.md"):
            return httpx.Response(200, text="# Cloud MD\n\nparsed")
        return httpx.Response(404, text=f"unexpected {request.method} {url}")

    def fake_client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False, **{
            k: v for k, v in kwargs.items() if k != "transport"
        })

    monkeypatch.setattr(
        "argusai.core.infra.httpx_client.direct_async_client",
        fake_client,
    )

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(document_parse, "_async_sleep", _fast_sleep)

    r = document_parse.parse_document(api, path="_attachments/scan.pdf", engine="mineru")
    assert r["ok"] is True
    assert r["engine"] == "mineru"
    assert "Cloud MD" in r["markdown"]


def test_settings_roundtrip_document_parse() -> None:
    raw = _parse(
        {
            "document_parse": {
                "max_output_chars": 12345,
                "mineru": {
                    "enabled": True,
                    "api_key": "tok-abc",
                    "base_url": "https://mineru.example",
                    "model_version": "pipeline",
                    "timeout_seconds": 120,
                },
            }
        }
    ).to_dict()
    assert raw["document_parse"]["mineru"]["enabled"] is True
    assert raw["document_parse"]["mineru"]["api_key"] == "tok-abc"
    assert raw["document_parse"]["max_output_chars"] == 12345


def test_merge_keeps_blank_mineru_api_key() -> None:
    current = {
        "max_output_chars": 200000,
        "mineru": {"enabled": True, "api_key": "secret", "base_url": "https://mineru.net"},
    }
    merged = _merge_document_parse_settings(
        current,
        {"mineru": {"enabled": True, "api_key": "", "base_url": "https://mineru.net"}},
    )
    assert merged["mineru"]["api_key"] == "secret"

    cleared = _merge_document_parse_settings(
        current,
        {"mineru": {"clear_api_key": True, "api_key": ""}},
    )
    assert cleared["mineru"]["api_key"] == ""


class _SettingsHarness(SettingsMixin):
    def __init__(self, path: Path) -> None:
        from argusai.core.settings import SettingsManager

        self.settings_mgr = SettingsManager(path)


def test_gui_save_settings_document_parse(tmp_path: Path) -> None:
    api = _SettingsHarness(tmp_path / "settings.json")
    r = api.save_settings(
        {
            "document_parse": {
                "max_output_chars": 50000,
                "mineru": {
                    "enabled": True,
                    "api_key": "tok-1",
                    "base_url": "https://mineru.net",
                    "model_version": "vlm",
                    "timeout_seconds": 180,
                },
            }
        }
    )
    assert r["ok"] is True
    s = api.settings_mgr.get()
    assert s.document_parse_mineru_enabled is True
    assert s.document_parse_mineru_api_key == "tok-1"
    assert s.document_parse_max_output_chars == 50000

    r2 = api.save_settings({"document_parse": {"mineru": {"enabled": True, "api_key": ""}}})
    assert r2["ok"] is True
    assert api.settings_mgr.get().document_parse_mineru_api_key == "tok-1"
