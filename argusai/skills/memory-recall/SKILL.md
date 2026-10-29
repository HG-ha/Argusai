---
name: memory-recall
description: Recall previous conversations, decisions, scan tasks, user preferences, and cross-session context. Use when the user refers to earlier work, says "之前", "刚才", "上次", "以前", "那个任务", "其他会话", or asks what was discussed before.
---

## Memory Recall

1. Decide the memory scope:
   - Same conversation or recent reference: call `search_memory`.
   - Older work, another chat/session, project history, or uncertain scope: call `search_all_memory`.
2. Generate your own search query from the user's intent. Prefer concrete terms:
   - Task IDs, URLs, domains, vulnerability names, filenames, feature names.
   - Chinese and English variants when useful, e.g. "窗口 缩放 resize", "XSS level1 过题".
3. If the first search is weak, refine and search again with a narrower or alternate query.
4. Ground the answer in retrieved hits. Mention which session or task the memory came from when helpful.
5. Do not invent remembered facts. If memory search has no useful hits, say so and ask for the missing detail.
6. For scan history, prefer retrieved task IDs and then call `get_task_detail`, `get_task_findings`, or `get_finding_detail` to verify current state.
7. Keep raw memory snippets concise in the final answer; summarize decisions and cite concrete IDs, URLs, or file names.
