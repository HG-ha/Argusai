---
name: access-control-workflow
description: Use for IDOR, horizontal/vertical privilege checks, multi-account comparisons, and business state-machine testing.
---

## Access Control Workflow

Prefer two real roles/accounts when available. Without credentials, only report IDOR leads as suspected.

1. Create or reuse a case.
2. Save named roles with `session_vault_save` (`user_a`, `user_b`, `admin`, `anonymous`) when credentials or headers are available.
3. Inventory session headers/cookies with `auth_session_inventory` or `session_vault_list`.
4. Identify object identifiers in paths, query strings, JSON bodies and filenames.
5. Parse and save representative requests with `web_request_parse` and `artifact_add`.
6. Compare original vs alternate object with `idor_compare_request`.
7. Compare multiple URLs and roles with `auth_matrix_from_vault` or `role_matrix_probe`.
8. For workflows, model steps with `business_state_probe` and test replay, skip, repeat and object swap cases.

Evidence requires unauthorized access impact, not just predictable IDs. Include account/role labels, original object, alternate object, status/length differences, and replay commands. Attach baseline and unauthorized responses as artifacts, then add `evidence_add(kind='finding'|'http')`.

