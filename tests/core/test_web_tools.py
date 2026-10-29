import asyncio
import sys
from types import ModuleType

from argusai.core.security import web_tools
from argusai.core.security.web_tools import (
    _parse_baidu_results,
    prefer_cn_search,
    web_search,
)

_HIT = {"heading": "Example", "link": "https://example.com", "description": "Fallback worked"}
_BAIDU_HTML = """
<html><body>
<div class="result c-container" mu="https://example.com/cn">
  <h3><a href="https://www.baidu.com/link?url=abc">百度标题</a></h3>
  <div class="c-abstract">百度摘要</div>
</div>
</body></html>
"""


def _install_fake_ddgs(monkeypatch, handler) -> None:
    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def text(self, query: str, **kwargs):
            return handler(query, **kwargs)

    fake_module = ModuleType("ddgs")
    fake_module.DDGS = FakeDDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake_module)


def test_web_search_prefers_bing_then_auto(monkeypatch) -> None:
    calls: list[str] = []

    def handler(query: str, **kwargs):
        backend = kwargs.get("backend")
        calls.append(backend)
        if backend == "bing":
            raise KeyError("text")
        return [_HIT]

    monkeypatch.setattr(web_tools, "_system_locale_tag", lambda: "en_us")
    _install_fake_ddgs(monkeypatch, handler)

    result = asyncio.run(web_search("latest news", max_results=3, region="us-en"))

    assert result["ok"] is True
    assert calls[:2] == ["bing", "auto"]
    assert result["backend"] == "auto"
    assert result["results"] == [
        {"title": "Example", "url": "https://example.com", "snippet": "Fallback worked"}
    ]


def test_web_search_reports_all_backend_errors(monkeypatch) -> None:
    def handler(query: str, **kwargs):
        raise RuntimeError(f"{kwargs.get('backend')} unavailable")

    monkeypatch.setattr(web_tools, "_system_locale_tag", lambda: "en_us")
    _install_fake_ddgs(monkeypatch, handler)

    result = asyncio.run(web_search("latest news", max_results=3, region="us-en"))

    assert result["ok"] is False
    assert "bing unavailable" in result["error"]
    assert "auto unavailable" in result["error"]
    assert "duckduckgo unavailable" in result["error"]
    assert result["results"] == []


def test_web_search_uses_baidu_after_bing_in_china(monkeypatch) -> None:
    calls: list[str] = []

    def handler(query: str, **kwargs):
        calls.append(kwargs.get("backend"))
        return []

    async def fake_baidu(query: str, max_results: int):
        return [{"title": "百度标题", "url": "https://example.com/cn", "snippet": "百度摘要"}]

    monkeypatch.setattr(web_tools, "_system_locale_tag", lambda: "zh_cn")
    monkeypatch.setattr(web_tools, "_baidu_html_search", fake_baidu)
    _install_fake_ddgs(monkeypatch, handler)

    result = asyncio.run(web_search("最新新闻", max_results=3, region="cn-zh"))

    assert result["ok"] is True
    assert result["backend"] == "baidu"
    assert result["region"] == "cn-zh"
    assert calls == ["bing"]
    assert result["results"][0]["url"] == "https://example.com/cn"


def test_web_search_upgrades_worldwide_region_on_chinese_locale(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def handler(query: str, **kwargs):
        seen["region"] = kwargs.get("region")
        seen["backend"] = kwargs.get("backend")
        return [_HIT]

    monkeypatch.setattr(web_tools, "_system_locale_tag", lambda: "zh_cn")
    _install_fake_ddgs(monkeypatch, handler)

    result = asyncio.run(web_search("新闻", max_results=3, region="wt-wt"))

    assert result["ok"] is True
    assert seen["region"] == "cn-zh"
    assert seen["backend"] == "bing"
    assert result["backend"] == "bing"


def test_explicit_western_region_skips_baidu_even_on_chinese_locale(monkeypatch) -> None:
    baidu_calls: list[str] = []

    def handler(query: str, **kwargs):
        return [_HIT]

    async def fake_baidu(query: str, max_results: int):
        baidu_calls.append(query)
        raise AssertionError("baidu should not run for us-en")

    monkeypatch.setattr(web_tools, "_system_locale_tag", lambda: "zh_cn")
    monkeypatch.setattr(web_tools, "_baidu_html_search", fake_baidu)
    _install_fake_ddgs(monkeypatch, handler)

    result = asyncio.run(web_search("news", max_results=3, region="us-en"))

    assert result["ok"] is True
    assert result["backend"] == "bing"
    assert baidu_calls == []
    assert prefer_cn_search("us-en") is False


def test_parse_baidu_results_prefers_mu_url() -> None:
    rows = _parse_baidu_results(_BAIDU_HTML, 5)
    assert rows == [
        {"title": "百度标题", "url": "https://example.com/cn", "snippet": "百度摘要"}
    ]
