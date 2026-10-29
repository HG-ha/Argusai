---
name: poc-page-workflow
description: Create safe local HTML proof-of-concept pages for authorized web security tests, one-click attack demos, XSS labs, opener/name payloads, and browser interaction PoCs. Do not use for CORS exploitability verification.
---

# Local PoC Page Workflow

Use this skill when the user asks for a one-click attack page, wants an HTML PoC opened automatically, or complains that they must manually create an HTML file.

Do **not** use this workflow for CORS verification. A `file://` PoC has `Origin: null`, so it only tests null-origin handling and can miss or misstate real `http(s)` attacker origins. For CORS, use `identity-workflow`: `cors_matrix_probe` for candidates, then `cors_exploit_verify` for real browser `fetch(..., credentials: "include")` verification.

Principles:

1. Only create PoC pages for authorized targets or local lab exercises.
2. Prefer a minimal, understandable HTML page over a large exploit framework.
3. Do not persist PoC files inside the project repository. Use `create_poc_page`; it writes under the PoC page directory configured in Settings.
4. If the PoC opens a remote target, show the target URL clearly in the page.
5. If the PoC has side effects beyond navigation/popups, ask for confirmation and keep the action explicit.
6. Do not auto-bypass captchas or perform credential attacks. For captcha workflows, create analysis/demo pages only.
7. For serious findings, attach the generated PoC path/URI and expected observation to the active case with `artifact_add` or `evidence_add`.

Recommended tool use:

- Use `create_poc_page` when you have an HTML body or full HTML document and the user wants a runnable demo.
- Never use `create_poc_page` / `open_poc_page` to decide whether a CORS issue is exploitable. Generate CORS HTML only as an explanatory artifact after `cors_exploit_verify` has already produced decisive evidence.
- Set `wrap=true` for snippets such as a link, form, script, or button.
- Set `open_browser=true` only when the user explicitly wants the page opened. This is a confirmed tool action.
- Use `open_poc_page` to reopen a previously generated page by the returned path or file URI.
- For XSS opener/name labs, generate a page with a clear button/link that sets `window.name`, calls `window.open`, or submits a form as needed.

Output style:

- After creating a page, always summarize the `path` and `directory` returned by the tool, what the page does, and what the user should observe.
- Do not ask the user to copy/save HTML manually if `create_poc_page` can handle it.
- Include the payload in prose only if it is short and useful for understanding.

