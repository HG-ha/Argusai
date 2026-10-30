---
name: channel-human-reply
description: Write natural direct-channel replies that follow the global user-habits profile, preserve technical facts, and use channel_reply to send one response as a few ordered message bubbles.
trigger-keywords:
  - "\\b(channel_reply|human reply|natural reply|multi[- ]bubble)\\b"
  - "拟人回复|拟人化|去AI味|多段回复|多句话|消息气泡|用户习惯"
  - "\\b(qq|feishu|telegram|discord|slack|dingtalk|wecom|wechat)\\b.*\\b(reply|message|chat)\\b"
trigger-tools:
  - channel_reply
---

# Channel human reply

Use this skill for a direct message that arrived through a paired communication channel. It controls wording and bubble boundaries. It does not change facts, permissions, evidence, or the safety model.

## Priority order

1. Apply the globally injected **用户习惯 / User habits** profile first. `speaking_style`, `output_format`, `avoid`, and `notes` override the generic style below. The same profile governs GUI chats and the floating ball; do not create a separate channel persona.
2. Preserve the user's requested register and the task's natural register. Incident response, commands, findings, hashes, URLs, dates, and risk statements stay exact.
3. Use the generic clean-chat rules below only where the profile is silent.

## Compose the reply

- Start with the useful answer. Skip ceremonial openings, praise, and restating the question.
- Say each point once. Remove empty transitions, repeated conclusions, forced three-part symmetry, and phrases such as “值得注意的是”“综上所述”“希望这能帮到你”.
- Keep specific facts. Never invent names, numbers, dates, citations, actions, feelings, typos, hesitation, or personal experience to sound human.
- Match the situation. Casual chat may be brief. A security conclusion remains precise. Code, commands, paths, identifiers, and quoted text remain verbatim.
- Use plain wording where it does not reduce precision. Do not trade a necessary technical term for a casual synonym.
- A natural reply may be one bubble. Do not split every sentence.

## Bubble boundaries

Use `channel_reply(messages=[...], pause_ms=...)` only for the current channel-authored turn. The Agent owns the boundaries: the runtime never splits or rewrites a bubble.

- For ordinary conversation containing several distinct thoughts, prefer 2–4 short bubbles so the exchange reads like a normal chat. One cohesive thought may stay in one bubble; use more than four only when separate steps genuinely need separate messages. The tool caps the call at 8.
- Split by meaning and conversational rhythm, never mechanically at every sentence or punctuation mark.
- When you intend several chat messages, call the tool once and put each exact message in its own `messages` item. Never put the whole multi-message reply into one array item and expect the runtime to split it.
- Each bubble should carry one complete thought. Good boundaries are: answer → evidence, result → next action, or short acknowledgement → requested detail.
- Keep related bullets, a code block, a URL with its label, and a command with its explanation in the same bubble.
- Use a short pause, normally 250–500 ms. Do not imitate typing mistakes or add filler merely to justify another bubble.
- If the answer is intentionally one cohesive paragraph or structured block, return that single reply normally; do not manufacture extra bubbles.
- Treat `channel_reply` as the final action of the turn. Finish all research and tool work first, then call it without surrounding assistant prose. Once the reply is accepted, the Agent loop terminates immediately; it does not run another model pass to manufacture a delivery receipt or summary.

Example:

```json
{
  "messages": [
    "已经定位到了：QQ 渠道没有进入当前公开历史，现有版本确实不会显示。",
    "我会恢复官方 SDK 接入和多段回复链路，完成后再跑渠道与会话隔离测试。"
  ],
  "pause_ms": 350
}
```

## Use the other channel paths when needed

- A blocking choice still uses `stop_for_user_decision` so the channel gets buttons or one-time `/act` commands.
- A proactive alert or message that did not originate from the current channel turn uses confirmation-gated `channel_send_message`.
- A file uses `channel_send_file`, followed by `channel_delivery_status` before claiming it arrived.
- `/live` stage updates may show real progress during a long run. Do not turn ordinary conversation into progress spam.

This guidance was informed by the MIT-licensed `LifelongLazyLearner/qu-ai-wei` project (<https://github.com/LifelongLazyLearner/qu-ai-wei>) and adapted for ArgusAI's channel, safety, and global user-habits contracts.
