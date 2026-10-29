"""Markdown report."""

from __future__ import annotations

import json
import time
from pathlib import Path

from argusai.core.finding import Severity
from argusai.core.runner import ScanResult


LABELS = {
    "en": {
        "title": "Security audit",
        "target": "Target",
        "scope": "Scope",
        "default": "(default)",
        "finished": "Finished",
        "in": "in",
        "seconds": "s",
        "severity_mix": "Severity mix",
        "check": "Check",
        "evidence": "Evidence",
        "remediation": "Remediation",
        "scan_errors": "Scan errors",
        "severity": {
            "critical": "CRITICAL",
            "high": "HIGH",
            "medium": "MEDIUM",
            "low": "LOW",
            "info": "INFO",
        },
    },
    "zh": {
        "title": "安全审计报告",
        "target": "目标",
        "scope": "作用域",
        "default": "（默认）",
        "finished": "完成时间",
        "in": "耗时",
        "seconds": "秒",
        "severity_mix": "严重性分布",
        "check": "检测项",
        "evidence": "证据",
        "remediation": "修复建议",
        "scan_errors": "扫描错误",
        "severity": {
            "critical": "严重",
            "high": "高危",
            "medium": "中危",
            "low": "低危",
            "info": "信息",
        },
    },
}

SEV_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "info": "⚪",
}


def _labels(locale: str) -> dict:
    return LABELS.get(locale, LABELS["en"])


def render_markdown(result: ScanResult, out: Path, locale: str = "en") -> Path:
    labels = _labels(locale)
    lines: list[str] = []
    t = result.target
    lines.append(f"# {labels['title']} — {t.name}")
    lines.append("")
    lines.append(f"- {labels['target']}: <{t.url}>")
    lines.append(f"- {labels['scope']}: {', '.join(t.scope) or labels['default']}")
    lines.append(
        f"- {labels['finished']}: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(result.finished_at))} "
        f"({labels['in']} {result.finished_at - result.started_at:.1f}{labels['seconds']})"
    )
    counts = result.by_severity()
    lines.append(
        f"- {labels['severity_mix']}: "
        + " · ".join(f"{SEV_EMOJI[s]}{labels['severity'][s]}:{counts[s]}" for s in counts)
    )
    lines.append("")

    sorted_findings = sorted(
        result.findings,
        key=lambda f: (-f.severity.rank, f.category, f.id),
    )

    for sev in ("critical", "high", "medium", "low", "info"):
        bucket = [f for f in sorted_findings if f.severity.value == sev]
        if not bucket:
            continue
        lines.append(f"## {SEV_EMOJI[sev]} {labels['severity'][sev]} ({len(bucket)})")
        lines.append("")
        for f in bucket:
            lines.append(f"### {f.title}")
            lines.append("")
            lines.append(f"- **ID**: `{f.id}`  ·  **{labels['check']}**: `{f.check}`")
            if f.url:
                lines.append(f"- **URL**: {f.url}")
            if f.cwe:
                lines.append(f"- **CWE**: {f.cwe}")
            if f.description:
                lines.append("")
                lines.append(f.description)
            if f.evidence:
                lines.append("")
                lines.append(f"<details><summary>{labels['evidence']}</summary>")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(f.evidence, ensure_ascii=False, indent=2))
                lines.append("```")
                lines.append("")
                lines.append("</details>")
            if f.remediation:
                lines.append("")
                lines.append(f"**{labels['remediation']}**: {f.remediation}")
            lines.append("")
    if result.errors:
        lines.append(f"## {labels['scan_errors']}")
        for e in result.errors:
            lines.append(f"- `{e['check']}`: {e['error']}")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out
