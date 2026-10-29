---
name: ui-block-protocol
description: Use ArgusAI UI blocks instead of duplicated Markdown for structured results and whenever a blocking question needs user input. Covers tables, cards, search results, findings, options, workflow choices, knowledge results, UI-first user decisions, and fenced mind maps for trees.
trigger-keywords:
  - "\\b(show_data_table|show_structured_card|show_file_resource_view)\\b"
  - "知识库.*(表格|列表|卡片)|表格.*展示|结构化.*卡片"
  - "\\b(data table|structured card)\\b"
  - "思维导图|mind ?map|markmap"
trigger-tools:
  - show_data_table
  - show_structured_card
  - show_file_resource_view
---

## UI Block Protocol

ArgusAI supports structured assistant UI blocks. Treat them as the primary user interface for interactive or structured output, not as decoration after prose.

This follows Loom22's principle: tools return machine-readable data, the runtime turns it into typed UI blocks, and user clicks go through stable action handlers. Do not make the user parse raw JSON or duplicated Markdown tables when a block can carry the same data.

### Core Rule

When a UI block is available or generated, the visible prose must not repeat the same payload.

Good pattern:

1. Call the block tool with the structured data.
2. Write at most 1-3 short sentences:
   - what was found,
   - what it means,
   - what the user can do next.
3. If a next step needs a choice, use `stop_for_user_decision` instead of a prose-only option list.

Bad pattern:

- Call `show_data_table`, then write the same rows again as a Markdown table.
- Call `show_structured_card`, then write the same metrics/steps/list again in prose.
- Call `show_file_resource_view`, then paste full file contents into prose.
- Call `kb_search`, then leave the user with only prose when a clickable table would be clearer.
- Call `stop_for_user_decision`, then also write "请选择 A/B/C" as a normal paragraph.
- Invent inline markup such as `<next_actions>` / `<option>…</option>` (or any other pseudo-widget tag) to fake clickable choices — the renderer does not parse it and it shows as raw text. Use `stop_for_user_decision`.
- Paste raw tool JSON into the answer.

### Search Results

These tools return data to the model; they do **not** automatically render a user-facing table:

- `kb_search`
- `search_memory`
- `search_all_memory`
- `skill_search`
- `codebase_search`

After using any of those tools:

- call `show_data_table` if the result set should be visible or clickable;
- do not write a Markdown table with the same rows;
- write only a short summary and the next useful action.

### `show_data_table`

Use `show_data_table` for model-authored structured data. Good examples:

- selected knowledge base hits after `kb_search`;
- memory / skill / code search results worth showing to the user;
- candidate URLs, endpoints, files, parameters, findings, tasks, or artifacts;
- options or candidates assembled from multiple tool outputs;
- any list where each row benefits from one-click actions. Labels are not fixed: use concise, task-specific labels such as `查看`, `打开`, `读取`, `使用`, `对比`, `生成计划`, `标为证据`, or another action that fits the row.

Keep columns compact:

- 2-5 visible columns is usually enough.
- Put long bodies, snippets, or raw evidence behind action buttons instead of in the table.
- Use `row.actions` for multiple row buttons, up to 4 concise actions. Keep `row.action` only for a single legacy action.
- Use stable `href` routes when the UI can open an entity directly, e.g. `#/knowledge/<id>` or `#/task/<id>`.
- Use `value` when clicking should send a follow-up instruction back to the assistant. The value may be custom, but it must be a short, concrete instruction the assistant can act on, not hidden state, raw JSON, HTML, JavaScript, or a vague label.

After the table, summarize instead of duplicating:

```text
找到 9 条知识库条目，全部为 confirmed。你可以点“查看”打开条目，或让我基于其中一条生成测试计划。
```

### `show_structured_card`

Use `show_structured_card` for model-authored structured information that is not naturally a row table:

- overview cards for a scan, plan, finding, endpoint, or artifact;
- grouped key/value facts such as target, auth state, confidence, evidence path;
- compact metrics such as counts, risk totals, coverage, or progress;
- ordered steps, timelines, remediation plans, or next actions;
- badges/tags/status chips;
- action groups where buttons should open an entity or submit a concrete instruction.

Allowed section types:

- `text`: short paragraph, no Markdown table.
- `kv`: key/value facts using `items: [{ "label": "...", "value": "..." }]`.
- `metrics`: compact numeric/status facts using the same item shape plus optional `note`.
- `list`: concise bullets using `items: [{ "label": "..." }]`.
- `steps`: ordered steps using `items: [{ "title": "...", "body": "...", "status": "..." }]`.
- `badges`: chips using `items: [{ "label": "...", "variant": "info|success|warning|danger|neutral" }]`.
- `actions`: buttons using `actions: [{ "label": "...", "href": "#/knowledge/<id>" }]` or `{ "label": "...", "value": "..." }`.

Use `data_table` when the user needs to compare many same-shaped rows. Use `structured_card` when the user needs a compact, typed summary. Do not invent nested cards, HTML, CSS, JavaScript, or arbitrary JSON widgets.

### `show_file_resource_view`

Use `show_file_resource_view` when the user should browse or preview generated files:

- sandbox scripts and snippets;
- PoC files, reports, notes, logs, JSON, HTML, JS, Python, shell scripts;
- reverse-engineering outputs or exported workspace artifacts;
- any small set of files where the user may click one item and inspect it.

The block should contain metadata, not full contents:

- prefer sandbox-relative `relative_path`;
- include `name`, `kind`, `size`, `mime` when known;
- set `previewable=false` for directories, binary files, archives, or anything that should not be shown inline;
- use item `actions` for explicit follow-ups such as `运行这个脚本`, `基于此文件生成报告`, or an internal `href`.

The UI reads text previews on demand from the current session sandbox. Do not paste large files into the conversation just because they are previewable.

### `stop_for_user_decision`

Use `stop_for_user_decision` when you need a human decision:

- choose a scope, target, strategy, report style, or next task;
- provide credentials, URL, cookie, OTP, file path, or other input;
- approve a workflow branch that cannot be inferred safely.

This is a hard UI-first rule:

- one blocking question → `stop_for_user_decision`;
- several related blocking questions → `stop_for_user_decision_batch`;
- never leave a required choice only in prose, a Markdown list, or a final
  sentence such as "要我先交付哪个？";
- on Feishu / Telegram / other remote channels, the only clickable choices are
  the card buttons — never ask the user to type `1` / `A` / `B` / `C`;
- explanatory prose may precede the tool call, but must not duplicate the
  question or options after the card is emitted;
- ordinary non-blocking conversation may remain prose, but if the Agent cannot
  continue without the answer, a `user_decision` block is required.

Good options are short and action-oriented:

```text
["读取第 1 条", "基于全部生成测试计划", "重新搜索", "停止"]
```

If typing is useful, set `input_placeholder`. Do not require typed input unless the next step cannot proceed without it.

### Output Shape

For structured results, prefer this shape:

```text
[block: data_table]

短摘要：找到了什么，以及最有用的下一步。
```

For structured summaries, prefer this shape:

```text
[block: structured_card]

一句话说明这张卡的含义或下一步。
```

For decision points, prefer this shape:

```text
[block: user_decision]
```

No extra option list is needed because the card itself is the UI.

### Current Supported Blocks

- `data_table`: structured rows with optional row actions.
- `structured_card`: generic protocolized card with whitelisted sections (`text`, `kv`, `metrics`, `list`, `steps`, `badges`, `actions`).
- `file_resource_view`: file/resource browser with click-to-preview sandbox files.
- `user_decision`: hard stop asking the user to choose or provide input.
- `todo_list`: progress and control flow.
- `kb_updated`: knowledge base update notification.
- task, finding, command, sandbox, replay, evidence, and reverse result cards generated by the runtime from tool traces.

If no existing block fits, keep prose concise and use a normal summary. Do not invent HTML, JavaScript, or unsupported block types in text.

### Mind maps

The GUI chat renders a fenced `mindmap` or mermaid `mindmap` as a zoomable diagram. Use that for a hierarchy, attack/call/auth chain, or decision tree. Emit one fence, keep node labels short, and do not reprint the same tree as a long bullet list. Other mermaid diagram types stay as code. Rows still go to `show_data_table`.

```mindmap
root((认证链路))
  入口
    登录
    SSO
  票据
    Cookie
    JWT
```
