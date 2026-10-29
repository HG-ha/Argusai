---
name: findings-review
description: Review scan findings and present them clearly. Use when the user asks about results, vulnerabilities, findings, or what a scan discovered.
---

## Findings Review

1. Call `get_task_detail` for severity overview. Report: "X findings: N critical, M high, P medium, Q low, R info".
2. Call `analyze_findings` for structured breakdown. Present critical/high first.
3. For a specific finding, call `get_finding_detail`. Explain: what it is → why it matters → how to fix. Reference CWE.
4. Use finding metadata when present:
   - `kind=discovery` means a lead; recommend follow-up tools before claiming exploitability.
   - `recommended_tools` and `next_actions` are the preferred continuation path.
   - `artifact_refs` / `evidence_ref` are stronger than prose summaries.
5. For follow-up verification, create or reuse a case and attach the relevant finding/detail as an artifact.
6. Identify patterns:
   - Many header findings → server hardening needed
   - Many secret findings → code review for hardcoded credentials
   - Many recon findings → reduce exposed paths
7. Recommend: fix criticals first, re-scan after fixes, use proxy for runtime coverage.
8. Use the user's language. Mention actual finding titles, URLs, severity.
