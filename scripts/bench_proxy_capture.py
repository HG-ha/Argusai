"""Offline load benchmark for the proxy capture hot path + live-push.

Answers the "does heavy capture wedge the backend or blow up memory?" question
without any network or GUI: it drives the real retention / passive-analysis /
per-flow UI-push code paths directly at high volume and reports throughput,
memory bounding, and how the live-push layer behaves when the UI bridge is slow.

Run: uv run --no-sync python scripts/bench_proxy_capture.py
"""

from __future__ import annotations

import threading
import time

from argusai.core.events import EventBus
from argusai.core.security import proxy_manager as pm
from argusai.core.security.proxy_analyzer import PassiveAnalyzer
from argusai.core.security.proxy_flow import ProxyConfig, TrafficFlow
from argusai.core.security.proxy_server import ProxyServer
from argusai.gui.live_push import LivePush


def make_flow(i: int) -> TrafficFlow:
    return TrafficFlow(
        id=i,
        timestamp=time.time(),
        method="GET",
        url=f"https://shop.example.test/api/catalog/item/{i}?q=search&token=abc{i}",
        host="shop.example.test",
        request_headers={"user-agent": "Mozilla/5.0", "cookie": "sid=deadbeef; a=b", "accept": "*/*"},
        request_body=b"",
        response_status=200,
        response_headers={"content-type": "application/json", "set-cookie": "x=1"},
        response_body=('{"ok":true,"id":%d,"name":"item","price":9.9}' % i).encode(),
        content_type="application/json",
    )


class _FakeWindow:
    """Stands in for the pywebview window; evaluate_js has ~1ms bridge latency."""

    def __init__(self) -> None:
        self.calls = 0

    def evaluate_js(self, _js: str) -> None:
        self.calls += 1
        time.sleep(0.001)


def _build_server(cap: int) -> tuple[ProxyServer, EventBus]:
    bus = EventBus()
    analyzer = PassiveAnalyzer(bus)
    server = ProxyServer(ProxyConfig(max_history=cap), analyzer, bus)
    return server, bus


def bench_capture(n: int, cap: int, analyze: bool, with_push: bool) -> dict:
    server, bus = _build_server(cap)
    win = _FakeWindow()
    live = LivePush()
    if with_push:
        live.bind(win)
        pm.set_ui_notify(lambda ch, payload=None: live.push(ch, payload))
        # Mirror the real wiring: ProxyManager._on_event pushes on each flow/finding.
        bus.subscribe(lambda evt: pm._notify_proxy_ui(evt.type) if evt.type in ("proxy.flow", "finding") else None)
    else:
        pm.set_ui_notify(None)

    # Warm vs late throughput: detect O(n^2) degradation as the buffer grows.
    marks: list[tuple[int, float]] = []
    t0 = time.perf_counter()
    for i in range(n):
        server._append_flow(make_flow(i), analyze=analyze)
        if i in (999, n - 1):
            marks.append((i, time.perf_counter()))
    total = time.perf_counter() - t0

    # Let the live-push worker drain whatever the bounded queue still holds.
    time.sleep(0.2)
    first_rate = 1000 / (marks[0][1] - t0) if marks else 0
    tail_count = n - 1 - 1000
    tail_rate = tail_count / (marks[-1][1] - marks[0][1]) if len(marks) > 1 and tail_count > 0 else 0

    pm.set_ui_notify(None)
    return {
        "flows": n,
        "cap": cap,
        "analyze": analyze,
        "with_push": with_push,
        "elapsed_s": round(total, 3),
        "flows_per_s": round(n / total),
        "first1k_per_s": round(first_rate),
        "tail_per_s": round(tail_rate),
        "retained_flows": len(server._flows),  # must stay == cap (bounded memory)
        "push_queue_left": live._queue.qsize(),  # must stay <= maxsize (coalesced)
        "push_delivered": win.calls,
    }


def main() -> None:
    print("=== proxy capture load benchmark (offline, no network) ===")
    scenarios = [
        dict(n=20000, cap=500, analyze=False, with_push=False),  # pure retention
        dict(n=20000, cap=500, analyze=False, with_push=True),   # + per-flow UI push
        dict(n=20000, cap=500, analyze=True, with_push=True),    # realistic: + passive analysis
        dict(n=20000, cap=5000, analyze=True, with_push=True),   # big buffer
    ]
    for s in scenarios:
        r = bench_capture(**s)
        print(
            f"n={r['flows']} cap={r['cap']} analyze={r['analyze']} push={r['with_push']}: "
            f"{r['flows_per_s']}/s (first1k {r['first1k_per_s']}/s, tail {r['tail_per_s']}/s) "
            f"retained={r['retained_flows']} pushQueueLeft={r['push_queue_left']} delivered={r['push_delivered']}"
        )
    print("notes: retained must equal cap (memory bounded); tail/s ~= first1k/s means no O(n^2) slowdown;")
    print("       delivered << n with a small pushQueueLeft means a slow UI bridge coalesces, never stalls capture.")


if __name__ == "__main__":
    main()
