---
name: identity-workflow
description: Use for CORS, CSRF, OAuth/OIDC/SAML/JWT analysis and PoC generation.
---

## Identity Workflow

1. Create or reuse a case. Attach relevant auth requests, redirects, tokens, or forms as artifacts.
2. For CORS, run `cors_matrix_probe` first to identify header candidates, then run `cors_exploit_verify` against a sensitive endpoint before calling it exploitable. Header reflection alone is only a lead.
3. Prefer `cors_exploit_verify` with an attached real browser session when the app needs login. If no attached session is available, pass a scoped `cookie` value and state that SameSite behavior is simulated from `cookie_same_site`.
4. Confirmed CORS evidence requires `readable=true`, credentials available, and a sensitive response signal. If the browser blocks the fetch, report the header issue as not exploitable by browser CORS.
5. Never use `create_poc_page`, `open_poc_page`, or a `file://` HTML page to verify CORS exploitability. Local files have `Origin: null`; they are only useful for a null-origin edge case after the main `http(s)` attacker-origin check.
6. For CSRF, identify state-changing actions, missing/unvalidated token, ambient credentials, and SameSite behavior. Generate PoC with `csrf_poc_generate`.
7. For OAuth/OIDC, analyze authorization URLs with `oauth_flow_analyze`: redirect URI, state, nonce, PKCE and response type.
8. For JWT, use `jwt_attack_surface`; decodability alone is not a vulnerability.
9. For SAML, use `saml_surface_analyze`; verify signature/audience/recipient handling with real flows before claiming impact.
10. For multi-role checks, save named sessions with `session_vault_save` and use `auth_matrix_from_vault`.

Evidence must connect protocol weakness to data access, account takeover, CSRF state change, or authorization bypass. Use `evidence_add` with artifact refs for redirect chains, requests, responses, token analysis, or PoC output.

