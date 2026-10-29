"""Reconnaissance: discover paths, fingerprint the stack, collect JS bundles."""

from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator
from urllib.parse import urljoin

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.target import Target

COMMON_PATHS = [
    "/",
    "/robots.txt",
    "/sitemap.xml",
    "/humans.txt",
    "/.env",
    "/.env.local",
    "/.env.production",
    "/.git/config",
    "/.git/HEAD",
    "/.DS_Store",
    "/package.json",
    "/composer.json",
    "/config.json",
    "/manifest.json",
    "/asset-manifest.json",
    "/swagger.json",
    "/openapi.json",
    "/api-docs",
    "/swagger-ui.html",
    "/actuator",
    "/actuator/health",
    "/actuator/env",
    "/actuator/heapdump",
    "/graphql",
    "/graphiql",
    "/server-status",
    "/phpinfo.php",
    "/wp-admin",
    "/wp-login.php",
    "/admin",
    "/login",
    "/debug",
    "/health",
    "/ping",
]

FINGERPRINT_HEADERS = [
    "server",
    "x-powered-by",
    "x-aspnet-version",
    "x-generator",
    "x-runtime",
    "x-drupal-cache",
    "via",
]

SENSITIVE_BODY_SIGNATURES = {
    "/.env": [re.compile(rb"(?m)^[A-Z_][A-Z0-9_]*=")],
    "/.git/config": [re.compile(rb"\[core\]")],
    "/.git/HEAD": [re.compile(rb"^ref: refs/")],
    "/package.json": [re.compile(rb'"dependencies"\s*:')],
    "/actuator": [re.compile(rb'"_links"\s*:')],
    "/swagger.json": [re.compile(rb'"swagger"|"openapi"')],
    "/openapi.json": [re.compile(rb'"openapi"')],
}


@register
class ReconCheck(Check):
    id = "recon"
    name = "Reconnaissance"
    description = "Fingerprint the stack and find exposed sensitive paths."
    order = 10

    async def run(
        self, target: Target, ctx: Context
    ) -> AsyncIterator[Finding]:
        ctx.log(f"recon: probing {target.origin}")

        # 1. Base request to capture fingerprint headers
        try:
            resp = await ctx.get(target.url)
        except Exception as e:
            yield Finding(
                id="recon.base-unreachable",
                category="recon",
                severity=Severity.INFO,
                title="Target base URL unreachable",
                url=target.url,
                description=str(e),
            )
            return

        fp: dict[str, str] = {}
        for h in FINGERPRINT_HEADERS:
            if h in resp.headers:
                fp[h] = resp.headers[h]
        if fp:
            yield Finding(
                id="recon.fingerprint",
                category="recon",
                severity=Severity.INFO,
                title="Server fingerprint headers present",
                url=str(resp.url),
                description=(
                    "The server advertises stack details through response headers. "
                    "While rarely a vulnerability on its own, this assists attackers "
                    "in mapping known CVEs to your deployment."
                ),
                evidence={"headers": fp, "status": resp.status_code},
                remediation=(
                    "Strip or replace Server / X-Powered-By headers at the edge "
                    "(nginx: server_tokens off; node: app.disable('x-powered-by'))."
                ),
                cwe="CWE-200",
            )

        # Record base HTML so later checks (secrets, xss) can reuse it.
        ctx.shared.setdefault("recon", {})["base_html"] = resp.text
        ctx.shared["recon"]["base_url"] = str(resp.url)
        ctx.shared["recon"]["fingerprints"] = fp

        # 2. Probe common paths concurrently; ctx.get already has a semaphore.
        paths_to_probe = COMMON_PATHS + list(target.paths)

        async def _probe(path: str):
            url = urljoin(target.origin + "/", path.lstrip("/"))
            try:
                r = await ctx.get(url)
            except Exception:
                return None
            return path, url, r

        probes = await asyncio.gather(*[_probe(p) for p in paths_to_probe])
        hits: list[dict] = []
        for item in probes:
            if item is None:
                continue
            path, url, r = item
            hits.append(
                {
                    "path": path,
                    "status": r.status_code,
                    "length": len(r.content),
                    "content_type": r.headers.get("content-type", ""),
                }
            )
            for sig in SENSITIVE_BODY_SIGNATURES.get(path, []):
                if sig.search(r.content):
                    yield Finding(
                        id=f"recon.exposed{path.replace('/', '-')}",
                        category="recon",
                        severity=Severity.HIGH,
                        title=f"Sensitive file exposed: {path}",
                        url=url,
                        description=(
                            "A sensitive file is being served by the application. "
                            "This can leak secrets, source code, or infrastructure metadata."
                        ),
                        evidence={"snippet": r.text[:400]},
                        remediation=(
                            "Block this path at the reverse proxy or remove the file "
                            "from the deployment artifact."
                        ),
                        cwe="CWE-538",
                    )
                    break
        ctx.shared["recon"]["paths"] = hits

        # 3. Pull JS bundles referenced by the base HTML for downstream checks.
        scripts = re.findall(r'<script[^>]+src="([^"]+)"', resp.text)
        script_urls = []
        for src in scripts[:60]:
            full = urljoin(str(resp.url), src)
            if target.in_scope(full):
                script_urls.append(full)

        async def _fetch_script(u: str):
            try:
                jr = await ctx.get(u)
                if jr.status_code == 200 and jr.text:
                    return u, jr.text
            except Exception:
                pass
            return None

        fetched = await asyncio.gather(*[_fetch_script(u) for u in script_urls])
        collected: dict[str, str] = {u: t for u, t in filter(None, fetched)}
        ctx.shared["recon"]["scripts"] = collected
        if collected:
            ctx.log(f"recon: collected {len(collected)} JS bundles")
