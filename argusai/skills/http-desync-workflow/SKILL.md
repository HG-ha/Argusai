---
name: http-desync-workflow
description: Use for HTTP request smuggling, cache poisoning and raw HTTP validation under explicit authorization.
---

## HTTP Desync Workflow

Protocol-layer tests are noisy and environment-sensitive. Treat results as suspected until manually confirmed on a controlled route.

1. Start with low-risk `cache_poison_probe` header reflection.
2. Use `http_desync_probe` only after explicit confirmation and within scope.
3. Use `raw_http_send` for a single crafted request when you need to reproduce parser behavior.
4. Attach the raw request and response excerpt to a case with `artifact_add`.
5. Record frontend/backend chain assumptions, status line, timing and response excerpts.
6. Do not use payloads that queue destructive follow-up requests.

Evidence should include raw request, response excerpt, proxy/cache context, and why the signal suggests CL.TE, TE.CL, H2 downgrade, or cache poisoning. Use `evidence_add` only for signals that are reproducible and scoped.

