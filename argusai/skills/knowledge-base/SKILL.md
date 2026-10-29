---
name: knowledge-base
description: Distill, retrieve, and revise your own long-term knowledge across assistant sessions. Use proactively at the start of any non-trivial task (search for prior solutions) and at the end (sediment what worked). Also use when the user says "记一下", "沉淀", "记到知识库", "下次别忘了", "remember this", "save this", or references your past notes.
trigger-keywords:
  - "\\b(kb_search|kb_save|kb_update|knowledge base)\\b"
  - "记一下|沉淀|知识库|remember this|save this|past notes"
---

## Knowledge Base — your own cross-session notebook

The knowledge base (KB) is the user's per-machine store of concrete, reusable web-pentest insights you've curated. It is **separate** from:

- **conversation memory** (`search_memory` / `search_all_memory`) — verbatim chat history, noisy, single-session-rooted;
- **skill docs** (`skills/*/SKILL.md`) — generic methodology written by humans, doesn't change;
- **task reports** (`get_task_detail` / `report.html`) — per-scan products, not reusable knowledge.
- **cases/artifacts/evidence** (`case_*`, `artifact_*`, `evidence_add`) — durable proof for one investigation, not reusable methodology.

KB entries are **markdown files with stable ids** (e.g. `kb-20260513-A7Xq`). They survive across sessions and across reboots. The user can browse / edit / delete them in the 知识库 UI page.

### When to use

#### At the START of a non-trivial task

1. Identify the situation in 1-3 keywords (e.g. "oracle sqli oob", "csrf hidden token", "websocket origin bypass").
2. Call `kb_search(query=..., tags=[...])` early — before firing payloads — to find anything you (or a past session) already wrote about this.
3. If a hit looks relevant, `kb_read(id=...)` for the full body. Apply the recorded payload / approach directly when it fits; cite the kb id in your reasoning so the user knows you're drawing on past work.

If KB index hints appear in the system prompt under "Knowledge Base — your past notes", they are the top recent / relevant entries. Treat them like a quick-reference list, but always `kb_read` the full body before relying on details.

#### At the END of a task / mid-task when you hit a real insight

Sediment when **all** of these are true:

- You learned something **concrete** (a payload, a one-liner, a workflow, a target-specific quirk);
- It's **not obvious** from the skill docs already in your context;
- You can imagine your future self saving 10+ minutes by reading it next time.

Sediment via `kb_save`. Required fields:

- `title`: short and concrete (e.g. "PortSwigger Oracle SQLi via EXTRACTVALUE + XMLTYPE OOB"). Avoid generic titles like "SQL injection notes".
- `body`: a markdown body. Recommended sections:
    - `## Context` — what target / target class, what failed first;
    - `## Solution` — copy-paste-ready payload / command / snippet;
    - `## Why it works` — one or two sentences of mechanism;
    - `## Known limitations` (optional) — when this doesn't apply.
- `tags`: lowercase keywords for tag-filtered search (e.g. `["sqli", "oracle", "xxe", "oob", "portswigger"]`).
- `trigger_terms`: phrases that should auto-surface this entry (synonyms, alternative names, target identifiers).
- `applies_to`: 1-3 plain Chinese / English sentences describing scenarios (e.g. `["Oracle 盲注，需外带", "UTL_HTTP 被禁用"]`).
- `confidence`: `confirmed` (you saw it work end-to-end) / `probable` (worked in similar context) / `hypothesis` (untested) / `experimental` (might work, mark for revisit).
- `reason`: one line — why you're saving (e.g. "解出 PortSwigger Lab 7").

**ALWAYS `kb_search` first with the same tags before `kb_save`.** If a near-match exists:

- Minor refinement (typo, extra caveat, better payload variant) → use `kb_update(id=<existing>, body=..., reason="...")`.
- Structural rewrite (different scope, different conclusion) → write the new entry first with `kb_save`, then call `kb_supersede(old_id=..., new_id=..., reason="...")` to link them. The old entry stays readable but is hidden from default search.

After `kb_save` / `kb_update`, the tool returns `near_matches` — if any of them are actually the same insight you just wrote, fix it on the NEXT turn with `kb_supersede`. Don't let parallel duplicates accumulate.

### What NOT to sediment

- Textbook XSS / SQLi syntax — already in `web-attack-knowledge` skill.
- "I ran nmap and found port 80 open" — too plain, not insight.
- Raw evidence that belongs in a case artifact — requests, responses, OOB events, hook hits, pcap slices, APK/EXE analysis dumps.
- Generic platitudes ("inputs should be validated") — not actionable.
- Verbose narrative of a single user request — those live in conversation memory.
- Negative results ("I tried X and it didn't work") **unless** the negative itself is valuable signal — e.g. "Lab 14 explicitly blocks `<script>`-tag XSS even with case variation, must use `<svg onload>`".

### Notifying the user

After any `kb_save` / `kb_update` / `kb_supersede` / `kb_delete`, the UI automatically renders a "知识库已更新" notification card with the entry id and title — you don't have to repeat the same info.

But DO mention the action briefly in your final summary, like:

> 我已经把这个 Oracle OOB exfil 思路沉淀到知识库（`kb-20260513-A7Xq`）。下次遇到类似 lab 直接复用。

This single line is enough — the user can click the notification card to open the entry. Don't paste the full body.

### Common operations cheatsheet

```text
# Starting a SQLi task → search first
kb_search(query="oracle sqli oob", tags=["sqli","oracle"], limit=8)

# Found a hit → read it
kb_read(id="kb-20260513-A7Xq")

# Solved something new
kb_save(
    title="PortSwigger Oracle SQLi via EXTRACTVALUE + XMLTYPE OOB",
    body="## Context\n...\n## Solution\n```\n...\n```\n## Why it works\n...",
    tags=["sqli","oracle","xxe","oob","portswigger"],
    trigger_terms=["EXTRACTVALUE","XMLTYPE","UTL_HTTP","oast.fun"],
    applies_to=["Oracle 盲注，需外带", "UTL_HTTP 被禁用"],
    confidence="confirmed",
    related=["task:<task_id>"],
    reason="解出 PortSwigger Lab 7",
)

# Refining an existing entry
kb_update(
    id="kb-20260513-A7Xq",
    body="<full new body>",  # body is full-replace, not patch
    reason="补充 PostgreSQL 变体",
)

# Structural rewrite — write new, then link
new = kb_save(title="...", body="...", tags=[...], ...)
kb_supersede(old_id="kb-20260513-A7Xq", new_id=new["entry"]["id"], reason="重写为通用方法论")

# Cleanup
kb_delete(id="kb-20260513-A7Xq", reason="过期，已被 kb-... 取代")

# Periodic housekeeping — advisory only
# Returns trash entries older than 30 days. You do NOT have a tool to
# hard-delete them; if the user wants to free disk, tell them to open the
# 知识库 page, tick "Include trashed", and click "清空回收站".
kb_suggest_cleanup(min_age_days=30, max_results=20)
```

### Trash & cleanup policy

`kb_delete` is **soft** — entries go to `_trash/` and remain restorable from
the UI. The user (NOT you) controls hard deletion:

- A startup auto-purge runs once per app launch and hard-deletes trash
  entries older than `kb_trash_retention_days` (default 30, configurable
  in Settings; `0` = never auto-purge).
- The user can click "清空回收站" (Empty trash) in the Knowledge Base
  page when the trash view is enabled.
- You have `kb_suggest_cleanup` to **propose** candidates if the user asks
  "is anything piling up?" or you notice trash growing across sessions —
  but you must defer the actual purge to the user.

### Quality bar reminder

Every entry should pass the **"would I want this verbatim in my notes next year"** test. If you wouldn't bookmark it yourself, don't save it.
