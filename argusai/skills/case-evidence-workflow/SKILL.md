---
name: case-evidence-workflow
description: Use for multi-step security analysis, reverse engineering, audits, exploit verification, reports, or any task where evidence must survive context compaction. Establishes Case, Artifact, Evidence, and stable-reference habits across Web, network, mobile, and binary domains.
---

## Case Evidence Workflow

Use todos for progress. Use cases for durable evidence.

### When to create a case

Create or reuse a case for any task that has more than a quick one-shot answer:

- Web audit, vulnerability verification, request replay, OOB testing
- JS/WASM reverse engineering or signed request reproduction
- Network/service triage, pcap review, system evidence collection
- APK/IPA or EXE/DLL/ELF reverse engineering
- Repository / source-code audit or white-box testing
- Anything that will become a report, PoC, or later follow-up

Use `case_create` at the start unless the user clearly points to an existing case.

### Reference model

- `todo`: execution state for this turn or task
- `case`: durable investigation container
- `artifact`: raw material such as request, response, trace, pcap, APK, EXE, decompiled code, source snippet, screenshot, log
- `evidence`: conclusion-supporting record that references artifacts
- `finding`: reportable security issue or lead

Never paste large raw material into chat. Store it with `artifact_add` and keep only `case_id`, `artifact_ref`, `evidence_ref`, `job_id`, `oracle_id`, or `task_id` in context.

### Default flow

1. Create or select a case with `case_create` / `case_list` / `case_get`.
2. Attach important inputs with `artifact_add`: raw requests, responses, scan snippets, traces, hook hits, decompiled snippets, command outputs.
3. Run domain tools to verify or disprove the hypothesis.
4. Attach decisive results with `evidence_add`, referencing the relevant artifacts.
5. Use todos to track progress, but never treat completed todos as evidence.
6. Final answers should cite the decisive evidence refs and summarize only what matters.

### Token discipline

- Store large content as artifacts, not tool output.
- Read artifacts with `artifact_read(max_chars=...)` only when needed.
- Prefer small summaries in `evidence_add(summary=...)`.
- When context is compacted, stable refs are the recovery path.

### Domains

Use `domain_inventory` when choosing a workflow or when the target is not Web.

- `web`: URL/API/browser/proxy/JS/OOB/request replay
- `network`: host/CIDR/service/pcap/protocol triage
- `mobile`: APK/IPA/static analysis/frida/API extraction
- `binary`: PE/ELF/EXE/DLL/static and behavior triage
- `common` + `target_type='repository'`: source-code audit. Use `codebase_scope_set` first, read code through `codebase_search` / `codebase_read`, and store decisive snippets with `codebase_evidence_add`.
