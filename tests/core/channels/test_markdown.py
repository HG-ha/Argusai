from __future__ import annotations

import re

from argusai.core.channels.cards import (
    feishu_card_elements,
    render_channel_reply,
)
from argusai.core.channels.markdown import (
    DINGTALK,
    DISCORD,
    FEISHU,
    PLAIN,
    RAW,
    SLACK,
    TELEGRAM_HTML,
    WECOM,
    dialect_for,
    feishu_markdown_parts,
    render_markdown,
    render_markdown_chunks,
)
from argusai.core.channels.models import ChannelReply

SAMPLE = """## 扫描结果

发现 **2 个高危**，详见 [报告](https://example.com/r?a=1&b=2)，字段 `id` *未过滤*。

- 注入点：/api/user
- 反射型 XSS

```bash
echo "1 < 2"
```

> 先修注入。

| 编号 | 风险 |
| --- | --- |
| 1 | 高危 |
"""


def test_provider_dialect_mapping() -> None:
    assert dialect_for("telegram") == TELEGRAM_HTML
    assert dialect_for("slack") == SLACK
    assert dialect_for("feishu") == FEISHU
    assert dialect_for("wecom") == WECOM
    assert dialect_for("dingtalk") == DINGTALK
    assert dialect_for("discord") == DISCORD
    assert dialect_for("webhook") == RAW


def test_telegram_html_escapes_and_wraps_every_construct() -> None:
    out = render_markdown(SAMPLE, TELEGRAM_HTML)
    assert "<b>扫描结果</b>" in out
    assert "<b>2 个高危</b>" in out
    assert '<a href="https://example.com/r?a=1&amp;b=2">报告</a>' in out
    assert "<code>id</code>" in out
    assert "<i>未过滤</i>" in out
    assert '<pre><code class="language-bash">echo "1 &lt; 2"</code></pre>' in out
    assert "<blockquote>先修注入。</blockquote>" in out
    assert "•" in out
    # Raw angle brackets must never reach Telegram outside of our own tags.
    assert "1 < 2" not in out
    assert re.search(r"<(?!/?(b|i|s|a|code|pre|blockquote)\b)", out) is None


def test_identifiers_keep_their_underscores() -> None:
    source = "调用 channel_send_file 和 stat_text_file，_强调_ 仍然生效。"
    for dialect in (TELEGRAM_HTML, SLACK, FEISHU, DISCORD, PLAIN):
        out = render_markdown(source, dialect)
        assert "channel_send_file" in out
        assert "stat_text_file" in out
    assert "<i>强调</i>" in render_markdown(source, TELEGRAM_HTML)


def test_slack_uses_its_own_dialect_not_commonmark() -> None:
    out = render_markdown(SAMPLE, SLACK)
    assert "*扫描结果*" in out
    assert "**" not in out
    assert "<https://example.com/r?a=1&amp;b=2|报告>" in out
    assert "[报告](" not in out
    assert "_未过滤_" in out
    assert "```" in out
    assert "&gt; 先修注入。" in out


def test_feishu_keeps_headings_quotes_lists_and_code() -> None:
    out = render_markdown(SAMPLE, FEISHU)
    assert out.startswith("## 扫描结果")
    assert "\n- 注入点" in out
    assert "```bash" in out
    assert "> 先修注入。" in out


def test_wecom_keeps_v1_subset_and_strips_the_rest() -> None:
    out = render_markdown(SAMPLE, WECOM)
    assert "## 扫描结果" in out
    assert "**2 个高危**" in out
    assert "`id`" in out
    assert "> 先修注入。" in out
    # v1 app messages render neither italics, fences, nor lists.
    assert "*未过滤*" not in out
    assert "未过滤" in out
    assert "```" not in out
    assert "• 注入点：/api/user" in out


def test_dingtalk_drops_fences_and_inline_code_only() -> None:
    out = render_markdown(SAMPLE, DINGTALK)
    assert "## 扫描结果" in out
    assert "- 注入点：/api/user" in out
    assert "```" not in out
    assert "`id`" not in out
    assert "字段 id *未过滤*" in out


def test_discord_passes_through_but_renders_tables_as_code() -> None:
    out = render_markdown(SAMPLE, DISCORD)
    assert "## 扫描结果" in out
    assert "**2 个高危**" in out
    assert "```bash" in out
    assert "编号 | 风险" in out


def test_plain_strips_markers_and_raw_passes_through() -> None:
    plain = render_markdown(SAMPLE, PLAIN)
    assert "**" not in plain
    assert "扫描结果" in plain
    assert "报告 (https://example.com/r?a=1&b=2)" in plain
    assert render_markdown(SAMPLE, RAW) == SAMPLE


def test_tables_are_aligned_for_surfaces_without_table_support() -> None:
    table = "| a | bbbb |\n| --- | --- |\n| cc | d |"
    out = render_markdown(table, WECOM)
    assert "a  | bbbb" in out
    assert "cc | d" in out


def test_feishu_markdown_parts_lift_pipe_tables() -> None:
    parts = feishu_markdown_parts(SAMPLE)
    kinds = [part["kind"] for part in parts]
    assert "table" in kinds
    table = next(part for part in parts if part["kind"] == "table")
    assert table["rows"][0] == ["编号", "风险"]
    assert table["rows"][1] == ["1", "高危"]
    markdown = "\n".join(part["content"] for part in parts if part["kind"] == "markdown")
    assert "## 扫描结果" in markdown
    assert "| 编号 |" not in markdown


def test_chunks_never_split_inside_a_tag_or_a_fence() -> None:
    chunks = render_markdown_chunks(SAMPLE, TELEGRAM_HTML, 120)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("<pre>") == chunk.count("</pre>")
        assert chunk.count("<b>") == chunk.count("</b>")
        assert not chunk.endswith("<")
        assert len(chunk) <= 120 or "\n" not in chunk
    joined = "".join(chunks)
    assert "扫描结果" in joined and "先修注入。" in joined


def test_oversized_code_block_is_split_into_whole_lines() -> None:
    body = "\n".join(f"line-{index}" for index in range(40))
    chunks = render_markdown_chunks(f"```\n{body}\n```", DISCORD, 120)
    assert len(chunks) > 1
    assert all(len(chunk) <= 120 for chunk in chunks)


def test_reply_payloads_carry_the_provider_flavour() -> None:
    reply = ChannelReply(text=SAMPLE, external_chat_id="chat-1")

    telegram = render_channel_reply("telegram", reply)
    assert telegram["parse_mode"] == "HTML"
    assert telegram["text_chunks"]
    assert "<b>扫描结果</b>" in telegram["text"]

    discord = render_channel_reply("discord", reply)
    assert discord["content_chunks"]
    assert "## 扫描结果" in discord["content"]

    slack = render_channel_reply("slack", reply)
    assert slack["blocks"][0]["text"]["type"] == "mrkdwn"
    assert "*扫描结果*" in slack["blocks"][0]["text"]["text"]
    slack_table = next(block for block in slack["blocks"] if block.get("type") == "table")
    assert slack_table["rows"][0][0]["text"] == "编号"
    assert slack_table["rows"][1][1]["text"] == "高危"

    feishu = render_channel_reply("feishu", reply)
    assert feishu["content"]["schema"] == "2.0"
    elements = feishu_card_elements(feishu["content"])
    markdown = next(item["content"] for item in elements if item.get("tag") == "markdown")
    assert "## 扫描结果" in markdown
    table = next(item for item in elements if item.get("tag") == "table")
    assert table["columns"][0]["display_name"] == "编号"
    assert table["rows"][0]["c1"] == "高危"

    wecom = render_channel_reply("wecom", reply)
    assert wecom["msgtype"] == "markdown"
    assert "## 扫描结果" in wecom["markdown"]["content"]

    dingtalk = render_channel_reply("dingtalk", reply)
    assert "## 扫描结果" in dingtalk["actionCard"]["text"]

    webhook = render_channel_reply("webhook", reply)
    assert webhook["text"] == SAMPLE.strip()


def test_wecom_falls_back_to_plain_text_inside_button_cards() -> None:
    reply = ChannelReply(
        text="**决定**",
        external_chat_id="chat-1",
        blocks=({
            "type": "user_decision",
            "title": "继续？",
            "actions": [{"label": "是", "token": "ca_token1234567890"}],
        },),
    )
    payload = render_channel_reply("wecom", reply)
    assert payload["msgtype"] == "template_card"
    assert "**" not in payload["template_card"]["sub_title_text"]
    assert "决定" in payload["template_card"]["sub_title_text"]


def test_wecom_markdown_content_stays_within_the_byte_budget() -> None:
    reply = ChannelReply(text="中" * 3000, external_chat_id="chat-1")
    payload = render_channel_reply("wecom", reply)
    assert len(payload["markdown"]["content"].encode("utf-8")) <= 2000
