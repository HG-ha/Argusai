---
name: upload-workflow
description: Use for file upload testing, MIME/extension bypass planning, SVG/HTML polyglot checks, and uploaded file access validation.
---

## Upload Workflow

1. Create or reuse a case for the target.
2. Discover forms with `upload_surface_discover`; attach the form/action summary with `artifact_add`.
3. Generate safe test files with `upload_payload_matrix`; do not create webshells.
4. Upload one marker file at a time through the authorized UI/API.
5. If the upload is API-driven, capture or reconstruct the request with `web_request_parse` and save it as an artifact.
6. Verify the returned URL with `upload_verify_access`.
7. Check storage location, content type, disposition, script execution, SVG/HTML execution, archive traversal and overwrite behavior.

Evidence should include form action, filename, MIME, marker, final URL, response content type and whether execution/readback occurred. Use `evidence_add` only after readback/execution behavior is observed or confidently ruled out.

