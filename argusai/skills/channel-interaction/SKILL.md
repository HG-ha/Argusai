---
name: channel-interaction
description: Interact well on paired remote channels (QQ, Feishu, Telegram, Discord, Slack, DingTalk, WeCom, WeChat, Webhook). Prefer channel_reply for direct replies, clickable UI-block decisions, stage replies under /live, and sandbox file delivery with channel_send_file — not MCP.
trigger-keywords:
  - "\\b(qq|feishu|lark|telegram|discord|slack|dingtalk|wecom|wechat|webhook)\\b"
  - "\\b(channel_reply|channel_send_file|channel_send_message|channel_list_targets)\\b"
  - "QQ|飞书|钉钉|企业微信|电报|远程通道|通讯渠道|发给我|发文件|回传文件|推送文件"
  - "通道.*(交互|卡片|按钮|决策)|远程.*(聊天|消息)|配对.*(通道|渠道)"
trigger-tools:
  - channel_list_targets
  - channel_reply
  - channel_send_message
  - channel_send_file
  - channel_delivery_status
  - stop_for_user_decision
  - stop_for_user_decision_batch
---

# Channel Interaction

Paired messaging channels are a first-class Agent surface, not a settings-only feature. The user may be on **QQ / Feishu / Telegram / Discord / Slack / DingTalk / WeCom / WeChat / Webhook** with limited formatting and **no rich desktop UI**. Optimize for clickable cards, short stages, and explicit file delivery. Each pairing only sees conversations granted to it — do not search or read other sessions.

Also read `ui-block-protocol` for block shapes. This skill covers **channel-specific** behavior.

## Hard rules (remote)

1. **Direct messages only.** Pairing, commands, and replies happen in a 1:1 chat with the bot. Group, channel, and room messages are ignored — do not tell the user to @ the bot in a group.
2. **Choices = buttons only.** Mutually exclusive next steps go through `stop_for_user_decision` / `_batch`. Never ask the user to type `1` / `A` / `B` / `C` or "回复选项编号".
3. **Card vs essay.** Explanation stays in assistant prose (or mid-turn live segments). Card `question` is 1–2 short control sentences; `options` are concrete clicks; `context` is at most one why-line. Do not paste install guides into the card.
4. **No fake progress filler.** If `/live` already pushed stage text + cards, do not invent a hollow final line like "完成。" when there is nothing left to say.
5. **Do not invent an MCP "send file" tool.** Channel delivery is native: `channel_reply`, `channel_list_targets`, `channel_send_message`, `channel_send_file`, `channel_delivery_status`.

## Decision UX

Use a decision card when continuing requires a human pick (strategy, scope, which file, confirm before irreversible effect). Batch related questions.

```text
Good: stop_for_user_decision(question="下一步？", options=["发报告到飞书", "先本地核对", "停止"])
Bad:  "请回复 A/B/C：A 发报告 …"
```

Ordinary chat and non-blocking status stay prose. If you cannot continue without an answer → card.

## Progressive replies (`/live`)

`/live` is **on by default**. The runtime may push **segment text + projectable cards** mid-turn. Treat that as the live surface:

- Prefer short stage summaries when a meaningful milestone lands (plan ready, scan done, file written).
- Still emit UI blocks / decision cards for structure and clicks.
- Avoid duplicating the same payload in prose after a card already showed it.
- Final reply: add substance only; silence is fine if live already covered it.

`/live off` → wait for the normal end-of-turn delivery. Do not spam status noise either way.

## Proactive outbound (Agent → channel)

| Tool | Confirm? | Use when |
|------|----------|----------|
| `channel_list_targets` | No | Discover send-enabled identities (bound + auto-route preview) |
| `channel_reply` | No | Reply to the exact identity that authored the current channel turn; one call may send several ordered bubbles |
| `channel_send_message` | Yes | Proactive text (briefing, alert, reminder) |
| `channel_send_file` | Yes | Deliver a **sandbox** artifact (report, zip, screenshot, script) |
| `channel_delivery_status` | No | Confirm queued send became `sent` / inspect `failed` + `last_error` |

### Multi-app, same session

One Agent conversation may be paired from Telegram + Feishu + Discord + … at once.

- **Ordinary channel reply (you messaged from QQ/TG/飞书/…)**: finish the work, then call `channel_reply(messages=[...])` as the turn's final action. It is locked to that exact identity (`route_mode=turn_source`), and an accepted call ends the Agent loop without another model response.
- **Channel file/proactive send**: omit `identity_id` → it goes **only to that same identity**. Do not fan-out and do not ask which app.
- **GUI / no channel turn**: omit `identity_id` → fan-out to every send-enabled identity bound to this conversation; if none are bound, auto-route to one unambiguous recently active chat, or to the only paired recipient without changing its inbound binding.
- Pass `identity_id` when the user named a recipient/platform or when `channel_list_targets` lists it under `other_session_targets`. An explicit confirmed send does not change that identity's incoming conversation binding.
- A successfully delivered cross-session message is mirrored into the recipient history as `channel_outbound` with its exact `Origin conversation`. If the recipient later asks why it was sent, call `read_assistant_session` only for that exact origin when it is already granted to this pairing; do not scan unrelated sessions.
- Check `delivery_count` / `deliveries` and then `channel_delivery_status`.

### File delivery playbook

1. Create or copy the deliverable into the **current session sandbox** (`sandbox_write_file` / export tools).
2. Call `channel_send_file(path=..., caption=...)` — omit `identity_id` unless the user named one app.
3. `channel_delivery_status` before claiming arrival.
4. Limits: path must resolve inside the sandbox; max **32 MiB**; outbound is confirmation-gated.

User-facing `/file` is for **browsing** sandbox files from chat commands. Prefer `channel_send_file` when the Agent should **push** a deliverable.

### Text delivery

- For an ordinary reply to the current channel user, use `channel_reply`; follow `channel-human-reply` for wording and bubble boundaries.
- Keep messages short; put long reports in a file and send the file.
- One coherent message per intent — do not flood with many tiny proactive sends.
- Target must be paired with `send` permission. If it is bound to another conversation, select it explicitly by `identity_id`; do not ask the user to re-pair.

## What not to do

- Duplicate Markdown tables / option lists after emitting UI blocks.
- Ask remote users for free-form "选哪个方案" without a decision card.
- Use desktop-only assumptions (hover, multi-panel GUI) when explaining next steps on channel.
- Tell the user a file was delivered while delivery is still `pending` / `failed`.
- Reach for MCP to reimplement channel send/list — use the native channel tools.

## Quick checklist

- [ ] Blocking choice → `stop_for_user_decision` (buttons), not A/B/C prose
- [ ] Channel turn → omit identity_id → `turn_source` back to that app
- [ ] GUI multi-app → omit identity_id → fan-out; user named one app → pass identity_id
- [ ] Deliverable → sandbox path → `channel_send_file` → `channel_delivery_status`
- [ ] `/live` on → stage + cards; empty final OK
- [ ] No MCP substitute for channel outbound
- [ ] Do not say "未配对" when `route_mode` is `turn_source` / `auto_*` / `session_fanout`
- [ ] Do not search or read sessions this pairing was not granted
