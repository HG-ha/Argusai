"""HTML report — single file, self-contained, usable in pywebview too."""

from __future__ import annotations

import json
import time
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from argusai.core.runner import ScanResult
from argusai.report.markdown import LABELS


_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def render_html(result: ScanResult, out: Path, locale: str = "en") -> Path:
    env = _env()
    tpl = env.get_template("report.html.j2")
    sorted_findings = sorted(
        result.findings, key=lambda f: (-f.severity.rank, f.category, f.id)
    )
    labels = LABELS.get(locale, LABELS["en"])
    html = tpl.render(
        locale=locale,
        labels=labels,
        target=result.target,
        findings=sorted_findings,
        severity=result.by_severity(),
        total=len(result.findings),
        started=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result.started_at)),
        finished=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result.finished_at)),
        elapsed=f"{result.finished_at - result.started_at:.1f}",
        errors=result.errors,
        dumps=lambda v: json.dumps(v, ensure_ascii=False, indent=2),
    )
    out.write_text(html, encoding="utf-8")
    return out
