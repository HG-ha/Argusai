"""Proxy traffic history: newest-first reads, incremental sync, clear, retention."""

from __future__ import annotations

import time
from pathlib import Path

from argusai.core.security.proxy import ProxyManager, ProxyServer
from argusai.core.security.proxy_analyzer import PassiveAnalyzer
from argusai.core.security.proxy_flow import ProxyConfig, TrafficFlow


def _manager(tmp_path: Path, cap: int = 5) -> tuple[ProxyManager, ProxyServer]:
    mgr = ProxyManager(ca_dir=tmp_path / "ca")
    analyzer = PassiveAnalyzer(mgr.bus)
    server = ProxyServer(ProxyConfig(max_history=cap), analyzer, mgr.bus)
    mgr._analyzer = analyzer
    mgr._server = server
    return mgr, server


def _capture(server: ProxyServer, path: str, *, headers: dict[str, str] | None = None) -> TrafficFlow:
    flow = TrafficFlow(
        id=server._next_flow_id(),
        timestamp=time.time(),
        method="GET",
        url=f"https://app.example.test{path}",
        host="app.example.test",
        response_status=200,
        response_headers=headers if headers is not None else {"content-type": "text/plain"},
        response_body=b"ok",
    )
    server._append_flow(flow)
    return flow


def test_get_traffic_returns_newest_flows(tmp_path: Path) -> None:
    mgr, server = _manager(tmp_path, cap=50)
    for i in range(10):
        _capture(server, f"/p{i}")
    rows = mgr.get_traffic(limit=3)
    assert [r["url"].rsplit("/", 1)[-1] for r in rows] == ["p7", "p8", "p9"]


def test_traffic_after_is_incremental_and_reports_trimming(tmp_path: Path) -> None:
    mgr, server = _manager(tmp_path, cap=5)
    for i in range(3):
        _capture(server, f"/a{i}")
    first = mgr.get_traffic_after(0, 500, None)
    assert first["reset"] is True
    assert [f["id"] for f in first["flows"]] == [1, 2, 3]

    for i in range(4):
        _capture(server, f"/b{i}")
    delta = mgr.get_traffic_after(3, 500, first["generation"])
    assert delta["reset"] is False
    assert [f["id"] for f in delta["flows"]] == [4, 5, 6, 7]
    # Cap 5: ids 1 and 2 were evicted; the reader trims below first_id.
    assert delta["first_id"] == 3
    assert delta["total"] == 5


def test_clear_bumps_generation_and_drops_findings(tmp_path: Path) -> None:
    mgr, server = _manager(tmp_path)
    flow = _capture(server, "/login", headers={"content-type": "text/html"})
    assert mgr.get_findings(), "missing security headers should be flagged"
    assert mgr.get_findings()[0]["metadata"]["flow_id"] == flow.id

    generation = mgr.get_traffic_after(0, 500, None)["generation"]
    result = mgr.clear_traffic()
    assert result["generation"] == generation + 1
    assert mgr.get_findings() == []
    after = mgr.get_traffic_after(flow.id, 500, generation)
    assert after["reset"] is True
    assert after["flows"] == []

    # Dedup is reset too, so the same page is reported again after a clear.
    _capture(server, "/login", headers={"content-type": "text/html"})
    assert mgr.get_findings()


def test_findings_are_not_duplicated_across_restarts(tmp_path: Path) -> None:
    mgr, server = _manager(tmp_path)
    _capture(server, "/x", headers={})
    count = len(mgr.get_findings())
    mgr._analyzer.reset_dedup()  # what a fresh analyzer after restart looks like
    _capture(server, "/x", headers={})
    assert len(mgr.get_findings()) == count


def test_history_survives_stop_and_continues_ids(tmp_path: Path) -> None:
    mgr, server = _manager(tmp_path)
    _capture(server, "/before")
    mgr._retain_history(server)
    mgr._server = None

    assert [r["url"] for r in mgr.get_traffic()] == ["https://app.example.test/before"]
    assert mgr.get_flow_detail(1)["ok"] is True
    assert mgr.get_status()["flow_count"] == 1

    restarted = ProxyServer(ProxyConfig(max_history=5), PassiveAnalyzer(mgr.bus), mgr.bus)
    restarted.adopt_history(mgr._retained_flows, mgr._last_flow_id)
    mgr._server = restarted
    assert _capture(restarted, "/after").id == 2
    assert [f["id"] for f in mgr.get_traffic_after(0, 500, None)["flows"]] == [1, 2]


def test_live_query_filters_delta_and_reports_decryption(tmp_path: Path) -> None:
    import base64

    mgr, server = _manager(tmp_path, cap=50)
    mgr.decryptor.auto_decode = True
    _capture(server, "/plain")
    encoded = base64.b64encode(b'{"secret":"value","n":1}').decode()
    flow = TrafficFlow(
        id=server._next_flow_id(),
        timestamp=time.time(),
        method="GET",
        url="https://app.example.test/enc",
        host="app.example.test",
        response_status=200,
        content_type="application/json",
        response_headers={"content-type": "application/json"},
        response_body=f'{{"data":"{encoded}"}}'.encode(),
    )
    server._append_flow(flow)
    mgr.decryptor.process(flow)  # what the background worker does

    first = mgr.get_traffic_after(0, 500, None, query="dec:secret", decode_seq=None)
    assert first["query_matches"] == [flow.id]
    assert first["decrypted_ids"] == [flow.id]
    again = mgr.get_traffic_after(flow.id, 500, first["generation"], query="dec:secret",
                                  decode_seq=first["decode_seq"])
    assert again["query_matches"] == [] and "decrypted_ids" not in again
    bad = mgr.get_traffic_after(0, 500, None, query="hots:x")
    assert "未知字段" in bad["query_error"]
    assert mgr.query_traffic("path:/enc", limit=5)["flows"][0]["id"] == flow.id


def test_live_query_rechecks_flows_decrypted_after_sync(tmp_path: Path) -> None:
    import base64

    mgr, server = _manager(tmp_path, cap=50)
    mgr.decryptor.auto_decode = True
    encoded = base64.b64encode(b'{"token":"abc"}').decode()
    late = TrafficFlow(
        id=server._next_flow_id(),
        timestamp=time.time(),
        method="GET",
        url="https://app.example.test/late",
        host="app.example.test",
        response_status=200,
        content_type="application/json",
        response_headers={"content-type": "application/json"},
        response_body=f'{{"data":"{encoded}"}}'.encode(),
    )
    server._append_flow(late)
    first = mgr.get_traffic_after(0, 500, None, query="dec:token", decode_seq=None)
    assert first["query_matches"] == []

    mgr.decryptor.process(late)  # plaintext shows up after the reader synced
    newer = _capture(server, "/newer")
    nxt = mgr.get_traffic_after(late.id, 500, first["generation"], query="dec:token",
                                decode_seq=first["decode_seq"])
    assert nxt["query_rechecked"] == [late.id]
    assert nxt["query_matches"] == [late.id]
    assert nxt["decrypted_ids"] == [late.id]
    assert [f["id"] for f in nxt["flows"]] == [newer.id]


def test_decrypted_plaintext_feeds_passive_analysis(tmp_path: Path) -> None:
    import base64

    mgr, server = _manager(tmp_path, cap=50)
    mgr.decryptor.auto_decode = True
    hidden = base64.b64encode(b'{"err":"You have an error in your SQL syntax near 1"}').decode()
    flow = TrafficFlow(
        id=server._next_flow_id(),
        timestamp=time.time(),
        method="GET",
        url="https://app.example.test/api/item?id=1",
        host="app.example.test",
        response_status=200,
        content_type="application/json",
        response_headers={"content-type": "application/json"},
        response_body=f'{{"data":"{hidden}"}}'.encode(),
    )
    server._append_flow(flow)
    assert not PassiveAnalyzer(mgr.bus)._check_sql_errors(flow, flow.response_body.decode())

    mgr.decryptor.process(flow)

    sql = [f for f in mgr._findings if f.id == "proxy.sql-error"]
    assert len(sql) == 1
    assert sql[0].metadata["source"] == "decrypted"
    assert sql[0].metadata["flow_id"] == flow.id


def test_flow_lookup_without_any_history_reports_not_running(tmp_path: Path) -> None:
    mgr = ProxyManager(ca_dir=tmp_path / "ca")
    assert mgr.get_flow_detail(1) == {"ok": False, "error": "proxy not running"}
    assert mgr.get_traffic_after(0, 500, None)["flows"] == []
