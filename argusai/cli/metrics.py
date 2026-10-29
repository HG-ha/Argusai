"""Host metrics for the ops MONITOR rail (stdlib / ctypes only — no psutil)."""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from argusai.core.token_usage_display import context_usage_parts, fmt_tokens, token_usage_parts

_SPARK = "▁▂▃▄▅▆▇█"
_HISTORY = 16


class _CpuTracker:
    """Delta-based CPU% + short sparkline history."""

    def __init__(self) -> None:
        self._prev: tuple[int, int] | None = None  # (idle, total)
        self._prev_ts = 0.0
        self._pct: float | None = None
        self._hist: deque[float] = deque(maxlen=_HISTORY)

    def update(self) -> float | None:
        snap = _cpu_times()
        now = time.monotonic()
        if snap is None:
            return self._pct
        idle, total = snap
        if self._prev is not None and total > self._prev[1] and now > self._prev_ts:
            d_total = total - self._prev[1]
            d_idle = idle - self._prev[0]
            if d_total > 0:
                busy = max(0.0, min(1.0, 1.0 - (d_idle / d_total)))
                self._pct = round(busy * 100.0, 1)
                self._hist.append(self._pct)
        self._prev = (idle, total)
        self._prev_ts = now
        return self._pct

    @property
    def pct(self) -> float | None:
        return self._pct

    @property
    def history(self) -> list[float]:
        return list(self._hist)


_CPU = _CpuTracker()


def sample_host_metrics(*, data_root: str | Path | None = None) -> dict[str, Any]:
    """Best-effort host sample. Never raises to callers."""
    out: dict[str, Any] = {
        "ok": True,
        "cpu_pct": None,
        "cpu_hist": [],
        "cpu_count": os.cpu_count() or None,
        "proc_rss_mb": None,
        "proc_pid": os.getpid(),
        "mem_used_mb": None,
        "mem_avail_mb": None,
        "mem_total_mb": None,
        "mem_pct": None,
        "swap_used_mb": None,
        "swap_total_mb": None,
        "disk_used_gb": None,
        "disk_free_gb": None,
        "disk_total_gb": None,
        "disk_pct": None,
        "disk_label": "",
        "uptime_s": None,
        "load_1": None,
        "load_5": None,
        "load_15": None,
    }
    try:
        out["cpu_pct"] = _CPU.update()
        out["cpu_hist"] = _CPU.history
    except Exception:
        pass
    try:
        out["proc_rss_mb"] = _process_rss_mb()
    except Exception:
        pass
    try:
        used, avail, total = _system_memory_detail_mb()
        out["mem_used_mb"] = used
        out["mem_avail_mb"] = avail
        out["mem_total_mb"] = total
        if used is not None and total:
            out["mem_pct"] = round(100.0 * float(used) / float(total), 1)
    except Exception:
        pass
    try:
        su, st = _swap_mb()
        out["swap_used_mb"] = su
        out["swap_total_mb"] = st
    except Exception:
        pass
    try:
        root = Path(data_root) if data_root else Path.cwd()
        root = root if root.exists() else Path.cwd()
        usage = shutil.disk_usage(str(root))
        used_g = usage.used / (1024**3)
        total_g = usage.total / (1024**3)
        free_g = usage.free / (1024**3)
        out["disk_used_gb"] = round(used_g, 1)
        out["disk_free_gb"] = round(free_g, 1)
        out["disk_total_gb"] = round(total_g, 1)
        out["disk_pct"] = round(100.0 * usage.used / usage.total, 1) if usage.total else None
        out["disk_label"] = _disk_label(root)
    except Exception:
        pass
    try:
        out["uptime_s"] = _uptime_s()
    except Exception:
        pass
    try:
        loads = _loadavg()
        if loads:
            out["load_1"], out["load_5"], out["load_15"] = loads
    except Exception:
        pass
    return out


def _disk_label(root: Path) -> str:
    try:
        resolved = root.resolve()
    except Exception:
        resolved = root
    s = str(resolved)
    if sys.platform == "win32" and len(s) >= 2 and s[1] == ":":
        return s[:2].upper()
    # Keep last path segment for unix data roots.
    name = resolved.name or s
    return name[:12] if name else "/"


def _cpu_times() -> tuple[int, int] | None:
    """Return (idle_ticks, total_ticks) or None."""
    if sys.platform == "win32":
        return _win_cpu_times()
    try:
        line = Path("/proc/stat").read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        parts = line.split()
        if parts[0] != "cpu":
            return None
        nums = [int(x) for x in parts[1:]]
        if len(nums) < 4:
            return None
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
        total = sum(nums)
        return idle, total
    except Exception:
        return None


def _win_cpu_times() -> tuple[int, int] | None:
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    def _u64(ft: FILETIME) -> int:
        return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)

    idle = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    kernel32 = ctypes.WinDLL("kernel32")
    GetSystemTimes = kernel32.GetSystemTimes
    GetSystemTimes.argtypes = [
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    GetSystemTimes.restype = wintypes.BOOL
    if not GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    idle_t = _u64(idle)
    # On Windows, kernel time includes idle time.
    kernel_t = _u64(kernel)
    user_t = _u64(user)
    total = kernel_t + user_t
    return idle_t, total


def _process_rss_mb() -> float | None:
    if sys.platform == "win32":
        return _win_process_rss_mb()
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8", errors="ignore")
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                kb = int(line.split()[1])
                return round(kb / 1024, 1)
    except Exception:
        pass
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return round(rss / (1024 * 1024), 1)
        return round(rss / 1024, 1)
    except Exception:
        return None


def _win_process_rss_mb() -> float | None:
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    psapi = ctypes.WinDLL("psapi")
    kernel32 = ctypes.WinDLL("kernel32")
    GetCurrentProcess = kernel32.GetCurrentProcess
    GetCurrentProcess.restype = wintypes.HANDLE
    GetProcessMemoryInfo = psapi.GetProcessMemoryInfo
    GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    if not GetProcessMemoryInfo(GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return round(int(counters.WorkingSetSize) / (1024 * 1024), 1)


def _system_memory_detail_mb() -> tuple[float | None, float | None, float | None]:
    """(used, available, total) in MB."""
    if sys.platform == "win32":
        return _win_system_memory_detail_mb()
    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore")
        total_kb = avail_kb = None
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
        if total_kb is None:
            return None, None, None
        total = round(total_kb / 1024, 1)
        if avail_kb is None:
            return None, None, total
        avail = round(avail_kb / 1024, 1)
        used = round((total_kb - avail_kb) / 1024, 1)
        return used, avail, total
    except Exception:
        return None, None, None


def _win_system_memory_detail_mb() -> tuple[float | None, float | None, float | None]:
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    kernel32 = ctypes.WinDLL("kernel32")
    GlobalMemoryStatusEx = kernel32.GlobalMemoryStatusEx
    GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
    GlobalMemoryStatusEx.restype = wintypes.BOOL
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not GlobalMemoryStatusEx(ctypes.byref(status)):
        return None, None, None
    total = round(int(status.ullTotalPhys) / (1024 * 1024), 1)
    avail = round(int(status.ullAvailPhys) / (1024 * 1024), 1)
    used = round(total - avail, 1)
    return used, avail, total


def _system_memory_mb() -> tuple[float | None, float | None]:
    """Back-compat: (used, total)."""
    used, _avail, total = _system_memory_detail_mb()
    return used, total


def _swap_mb() -> tuple[float | None, float | None]:
    if sys.platform == "win32":
        return _win_swap_mb()
    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore")
        total_kb = free_kb = None
        for line in meminfo.splitlines():
            if line.startswith("SwapTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("SwapFree:"):
                free_kb = int(line.split()[1])
        if total_kb is None or total_kb == 0:
            return None, None
        total = round(total_kb / 1024, 1)
        free = round((free_kb or 0) / 1024, 1)
        return round(total - free, 1), total
    except Exception:
        return None, None


def _win_swap_mb() -> tuple[float | None, float | None]:
    """Pagefile used/total via MEMORYSTATUSEX."""
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    kernel32 = ctypes.WinDLL("kernel32")
    GlobalMemoryStatusEx = kernel32.GlobalMemoryStatusEx
    GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
    GlobalMemoryStatusEx.restype = wintypes.BOOL
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not GlobalMemoryStatusEx(ctypes.byref(status)):
        return None, None
    total = int(status.ullTotalPageFile) / (1024 * 1024)
    avail = int(status.ullAvailPageFile) / (1024 * 1024)
    # Pagefile counters include physical RAM on Windows; show commit charge style.
    if total <= 0:
        return None, None
    used = max(0.0, total - avail)
    return round(used, 1), round(total, 1)


def _uptime_s() -> float | None:
    if sys.platform == "win32":
        import ctypes

        GetTickCount64 = ctypes.windll.kernel32.GetTickCount64
        GetTickCount64.restype = ctypes.c_ulonglong
        return float(GetTickCount64()) / 1000.0
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except Exception:
        return None


def _loadavg() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None


def meter_bar(ratio: float | None, *, width: int = 10) -> str:
    """btop-style block meter."""
    if ratio is None:
        return "░" * width
    clamped = max(0.0, min(1.0, float(ratio)))
    filled = int(round(clamped * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def sparkline(values: list[float] | None, *, width: int = 12, scale: float = 100.0) -> str:
    if not values:
        return "·" * width
    recent = values[-width:]
    if len(recent) < width:
        recent = [0.0] * (width - len(recent)) + recent
    chars: list[str] = []
    for v in recent:
        idx = int(round(max(0.0, min(1.0, float(v) / scale)) * (len(_SPARK) - 1)))
        chars.append(_SPARK[idx])
    return "".join(chars)


def _fmt_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    mins, _ = divmod(s, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{mins:02d}m"
    return f"{mins}m"


def _fmt_gib_from_mb(mb: float | None) -> str:
    if mb is None:
        return "n/a"
    if mb >= 1024:
        return f"{mb / 1024:.1f}G"
    return f"{mb:.0f}M"


def _fmt_gb(gb: float | None) -> str:
    if gb is None:
        return "n/a"
    if gb >= 100:
        return f"{gb:.0f}G"
    return f"{gb:.1f}G"


def _metric_detail_row(label: str, value: str, *, value_width: int = 16) -> str:
    """Align secondary metric labels and values to stable columns."""
    return f"     {label:<5}{value:>{value_width}}"


def format_token_usage_lines(
    payload: dict[str, Any],
    *,
    bar_width: int = 8,
) -> list[str]:
    """Compact session Token/cache block for the MONITOR rail."""
    parts = token_usage_parts(payload)
    context = context_usage_parts(payload)
    if parts is None and context is None:
        return []

    value_width = bar_width + 6

    def token_row(label: str, value: str) -> str:
        return f"{label:<6}{value:>{value_width}}"

    lines: list[str] = []
    if context is not None:
        pct_text = f"{context['percent']:.0f}%"
        lines.append(
            f"{'CTX':<6}{meter_bar(context['percent'] / 100.0, width=bar_width)} {pct_text:>5}"
        )
    if parts is None:
        return lines

    hit_rate = parts["hit_rate"]
    ratio = hit_rate / 100.0 if hit_rate is not None else None
    rate_text = f"{hit_rate:.1f}%" if hit_rate is not None else "n/a"
    lines.extend(
        [
            token_row("IN", fmt_tokens(int(parts["input_tokens"]))),
            token_row("OUT", fmt_tokens(int(parts["output_tokens"]))),
            f"{'HIT':<6}{meter_bar(ratio, width=bar_width)} {rate_text:>5}",
            token_row(
                "H/M",
                f"{fmt_tokens(int(parts['hit_tokens']))} / {fmt_tokens(int(parts['miss_tokens']))}",
            ),
        ]
    )
    if int(parts["write_tokens"]):
        lines.append(token_row("WRITE", fmt_tokens(int(parts["write_tokens"]))))
    lines.append(token_row("CALLS", str(int(parts["calls"]))))
    return lines


def format_metrics_lines(sample: dict[str, Any], *, bar_width: int = 10) -> list[str]:
    """Compact btop-like SYS block for the right rail (~32 cols)."""
    cpu = sample.get("cpu_pct")
    hist = list(sample.get("cpu_hist") or [])
    cores = sample.get("cpu_count")
    mu, ma, mt = sample.get("mem_used_mb"), sample.get("mem_avail_mb"), sample.get("mem_total_mb")
    mem_pct = sample.get("mem_pct")
    if mem_pct is None and mu is not None and mt:
        mem_pct = round(100.0 * float(mu) / float(mt), 1)
    su, st = sample.get("swap_used_mb"), sample.get("swap_total_mb")
    du, df, dt = sample.get("disk_used_gb"), sample.get("disk_free_gb"), sample.get("disk_total_gb")
    disk_pct = sample.get("disk_pct")
    if disk_pct is None and du is not None and dt:
        disk_pct = round(100.0 * float(du) / float(dt), 1)
    label = str(sample.get("disk_label") or "disk")
    proc = sample.get("proc_rss_mb")
    pid = sample.get("proc_pid")
    up = _fmt_uptime(sample.get("uptime_s"))
    load1 = sample.get("load_1")

    cpu_ratio = (float(cpu) / 100.0) if cpu is not None else None
    mem_ratio = (float(mem_pct) / 100.0) if mem_pct is not None else None
    disk_ratio = (float(disk_pct) / 100.0) if disk_pct is not None else None
    swap_ratio = (float(su) / float(st)) if su is not None and st else None
    rss_ratio = (float(proc) / float(mt)) if proc is not None and mt else None

    cpu_txt = f"{cpu:.0f}%" if cpu is not None else "…"
    core_txt = f"{cores}c" if cores else ""
    mem_txt = f"{mem_pct:.0f}%" if mem_pct is not None else "n/a"
    disk_txt = f"{disk_pct:.0f}%" if disk_pct is not None else "n/a"
    app_txt = f"{proc:.0f}M" if proc is not None else "n/a"

    lines = [
        f"CPU  {meter_bar(cpu_ratio, width=bar_width)} {cpu_txt:>4} {core_txt}".rstrip(),
        f"     {sparkline(hist, width=max(10, bar_width + 2))}",
        f"MEM  {meter_bar(mem_ratio, width=bar_width)} {mem_txt:>4}",
        _metric_detail_row(
            "used",
            f"{_fmt_gib_from_mb(mu)} / {_fmt_gib_from_mb(mt)}",
        ),
        _metric_detail_row("free", _fmt_gib_from_mb(ma)),
    ]
    if su is not None and st and float(st) > 0:
        sw_pct = f"{100.0 * float(su) / float(st):.0f}%"
        lines.append(f"SWP  {meter_bar(swap_ratio, width=bar_width)} {sw_pct:>4}")
        lines.append(
            _metric_detail_row(
                "used",
                f"{_fmt_gib_from_mb(su)} / {_fmt_gib_from_mb(st)}",
            )
        )
    lines.extend(
        [
            f"DSK  {meter_bar(disk_ratio, width=bar_width)} {disk_txt:>4}",
            _metric_detail_row(label, f"{_fmt_gb(du)} / {_fmt_gb(dt)}"),
            _metric_detail_row("free", _fmt_gb(df)),
            f"APP  {meter_bar(rss_ratio, width=bar_width)} {app_txt:>4}",
            _metric_detail_row("pid", str(pid)),
        ]
    )
    host_bits = [f"up {up}"]
    if load1 is not None:
        host_bits.append(f"ld {load1:.2f}")
    lines.append(f"{'HOST':<5}{' · '.join(host_bits):>21}")
    return lines
