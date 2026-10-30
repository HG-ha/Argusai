---
name: user-notification
description: Notify the user about meaningful completion, failure, or attention-required events through a local desktop notification or a paired communication channel. Use when the user asks to be notified, alerted, reminded when work finishes, or sent a result through QQ, Feishu, Telegram, Discord, Slack, DingTalk, WeCom, WeChat, or Webhook.
inject-priority: 8
trigger-keywords:
  - "\\b(notify me|notification|alert me|remind me when|send me an alert)\\b"
  - "通知我|提醒我|桌面通知|系统通知|消息推送|结果推送|完成后.*(通知|提醒|告诉)|失败时.*通知"
  - "(QQ|飞书|钉钉|企业微信|微信|电报|通讯渠道).*(通知|提醒|推送)"
trigger-tools:
  - desktop_notify
  - channel_list_targets
  - channel_send_message
  - channel_delivery_status
---

# User Notification

Use notifications as the final delivery step for meaningful completion, failure,
or attention-required events only when the current conversation came from the
GUI and ArgusAI is hidden or unfocused. Do not notify for routine progress.

## Choose the route

1. Check the turn source first. If it came from QQ, Feishu, Telegram, Discord,
   Slack, DingTalk, WeCom, Webhook, or another communication channel, do not
   call any notification tool. The normal final reply is already the notification.
2. If the turn came from the GUI but ArgusAI is still focused and visible, do
   not notify; the user can see the result in the current conversation.
3. For a background/unfocused GUI turn with no named destination, use
   `desktop_notify`.
4. For a named or paired communication channel from a background GUI turn,
   call `channel_list_targets`,
   then `channel_send_message`. Pass `identity_id` when the user named one
   recipient/platform or the target is bound to another conversation.
5. Use both desktop and channel only when the user asks for both.

For recurring or detached work, use the `assistant-cron` workflow and its
notification settings instead of assuming this turn remains alive.

## Desktop notification

- Call `desktop_notify(title=..., message=..., level=...)`.
- The tool returns `sent=false` for channel-origin turns and while ArgusAI is
  focused; treat that as intentional suppression, not a failure.
- Keep title short and message actionable.
- If notifications are disabled or unavailable, report that once; do not retry
  repeatedly or replace it with desktop clicks.

## Communication-channel notification

1. Resolve the recipient with `channel_list_targets`.
2. Send one concise message with `channel_send_message`.
3. Check `channel_delivery_status` before claiming delivery.
4. Do not rebind a recipient merely to send one notification.

Channel sends remain confirmation-gated. Never use desktop automation or an MCP
substitute to send through a paired channel.

## Content

- Include outcome, the minimum useful result, and the next action when needed.
- For failure, include the concrete blocker and recovery step.
- Never put API keys, cookies, credentials, private tokens, or large raw logs in
  a notification. Refer to the protected artifact or current conversation.
- Deduplicate: one notification per completed event unless the user requests
  escalation or retries.
