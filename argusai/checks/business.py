"""Business-logic checks.

Unlike the generic checks above, business-logic bugs are tightly coupled to
the target. We ship an IDOR probe template that works on REST endpoints of
the form ``/api/.../<id>`` where the ``id`` is numeric. For anything more
nuanced the user can plug in their own ``Check`` subclass and register it.
"""

from __future__ import annotations

import re
from typing import AsyncIterator
from urllib.parse import urljoin

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.target import Target


REST_ID_RE = re.compile(r"(/api/[A-Za-z0-9._/-]+/)(\d+)(/?.*)")


@register
class BusinessCheck(Check):
    id = "business"
    name = "Business logic (IDOR template)"
    description = "Probe REST endpoints for trivial IDOR (needs auth for real coverage)."
    order = 80

    async def run(
        self, target: Target, ctx: Context
    ) -> AsyncIterator[Finding]:
        # Nothing to do without a prior IDOR-candidate map. The recon check
        # collects hits, but by default knows only top-level paths; users
        # should add specific ``/api/foo/{id}`` entries to ``target.paths``.
        candidates = [p for p in target.paths if re.search(r"/\d+", p)]
        if not candidates:
            return

        for path in candidates:
            url = urljoin(target.origin + "/", path.lstrip("/"))
            m = REST_ID_RE.search(url)
            if not m:
                continue
            try:
                own = await ctx.get(url)
            except Exception:
                continue
            if own.status_code != 200:
                continue
            # Decrement / increment the id by 1 to look for neighbour access.
            neighbour = int(m.group(2)) + 1
            probe_url = m.group(1) + str(neighbour) + (m.group(3) or "")
            try:
                probe = await ctx.get(probe_url)
            except Exception:
                continue
            if probe.status_code == 200 and probe.text and probe.text != own.text:
                yield Finding(
                    id="business.idor-neighbour",
                    category="business",
                    severity=Severity.HIGH,
                    title=f"Possible IDOR at {probe_url}",
                    url=probe_url,
                    description=(
                        "An adjacent object id returned HTTP 200 with non-empty "
                        "content different from the owner's record. This may indicate "
                        "missing authorisation checks."
                    ),
                    evidence={
                        "own": url,
                        "neighbour": probe_url,
                        "own_len": len(own.content),
                        "neighbour_len": len(probe.content),
                    },
                    remediation=(
                        "Enforce authorisation server-side: verify that the authenticated "
                        "principal owns the requested object on every endpoint."
                    ),
                    cwe="CWE-639",
                )
