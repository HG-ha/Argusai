"""Upload surface checks."""

from __future__ import annotations

from typing import AsyncIterator
from urllib.parse import urljoin

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.target import Target
from argusai.core.security.upload_tools import upload_surface_discover_from_html


@register
class UploadCheck(Check):
    id = "upload"
    name = "Upload Surface"
    description = "Discover file upload forms and provide safe payload matrix guidance."
    order = 83

    async def run(self, target: Target, ctx: Context) -> AsyncIterator[Finding]:
        urls = [target.url]
        urls.extend(urljoin(target.origin + "/", p.lstrip("/")) for p in target.paths[:20])
        seen = set()
        for url in urls:
            if url in seen or not target.in_scope(url):
                continue
            seen.add(url)
            try:
                r = await ctx.get(url)
            except Exception:
                continue
            found = upload_surface_discover_from_html(r.text, str(r.url))
            if not found.get("surfaces"):
                continue
            yield Finding(
                id="upload.surface",
                category="upload",
                severity=Severity.INFO,
                title="File upload surface discovered",
                url=str(r.url),
                description="A file input form was discovered. Use the upload payload matrix to test MIME, extension and content handling safely.",
                evidence=found,
                remediation="Validate extension, MIME and magic bytes server-side; store uploads outside executable web roots; serve with safe Content-Type and Content-Disposition.",
                cwe="CWE-434",
            )

