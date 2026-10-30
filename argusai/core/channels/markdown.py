"""Translate assistant Markdown into each provider's own flavour.

Assistant output is CommonMark-ish. Every chat platform speaks a different
subset, so a single string cannot satisfy all of them:

* Telegram renders nothing without ``parse_mode``; HTML is the only mode that
  survives arbitrary assistant text once escaped.
* Slack uses its own ``mrkdwn`` dialect (``*bold*``, ``<url|label>``) and has no
  headings or tables.
* Feishu card JSON 2.0 markdown supports headings, quotes, lists, and code, but
  still not pipe tables — those become native ``table`` components.
* WeCom *app* messages accept only the v1 subset: headings, bold, links, inline
  code, and quotes (``markdown_v2`` exists for group webhooks only).
* DingTalk action cards render headings, emphasis, links, and lists, but no
  code fences or tables.
* Discord already renders CommonMark natively; only tables need help.
* QQ Bot Markdown preserves headings, emphasis, links, quotes and fenced code;
  pipe tables become fenced text. The transport falls back to plain text when
  the bot has not been granted QQ's separately reviewed Markdown permission.

Anything a target cannot render is degraded into readable plain text instead of
leaking raw syntax characters at the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

TELEGRAM_HTML = "telegram_html"
SLACK = "slack"
FEISHU = "feishu"
WECOM = "wecom"
DINGTALK = "dingtalk"
DISCORD = "discord"
QQ = "qq"
PLAIN = "plain"
RAW = "raw"

_FENCE_RE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*(?P<lang>[A-Za-z0-9_+.-]*)\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}(?P<level>#{1,6})\s+(?P<body>.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(?P<body>.*)$")
_LIST_RE = re.compile(r"^(?P<indent>\s*)(?P<marker>[-*+]|\d{1,3}[.)])\s+(?P<body>.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$")

# Underscore emphasis needs word boundaries on both sides, or identifiers like
# ``channel_send_file`` turn into "channel<i>send</i>file".
_INLINE_RE = re.compile(
    r"(?P<code_fence>`+)(?P<code>.+?)(?P=code_fence)"
    r"|!?\[(?P<link_text>[^\]]*)\]\((?P<link_url>[^()\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|\*\*(?P<strong>.+?)\*\*"
    r"|(?<![0-9A-Za-z_])__(?P<strong_underscore>.+?)__(?![0-9A-Za-z_])"
    r"|~~(?P<strike>.+?)~~"
    r"|\*(?P<em>[^\s*](?:[^*]*[^\s*])?)\*"
    r"|(?<![0-9A-Za-z_])_(?P<em_underscore>[^\s_](?:[^_]*[^\s_])?)_(?![0-9A-Za-z_])",
    re.DOTALL,
)


def _display_width(value: str) -> int:
    return sum(2 if ord(char) > 0x2E80 else 1 for char in value)


def _pad(value: str, width: int) -> str:
    return value + " " * max(0, width - _display_width(value))


@dataclass(frozen=True)
class Dialect:
    """How one provider wants each Markdown construct expressed."""

    name: str
    escape: Callable[[str], str] = lambda value: value
    bold: Callable[[str], str] = lambda value: value
    italic: Callable[[str], str] = lambda value: value
    strike: Callable[[str], str] = lambda value: value
    code: Callable[[str], str] = lambda value: value
    link: Callable[[str, str], str] = lambda text, url: f"{text} ({url})"
    heading: Callable[[int, str], str] = lambda _level, value: value
    quote: Callable[[str], str] = lambda value: value
    code_block: Callable[[str, str], str] = lambda body, _lang: body
    bullet: str = "• "
    hr: str = "──────────"
    tables_as_code: bool = True
    # Telegram forbids splitting inside a tag, so chunking happens per block.
    chunk_limit: int = 0
    parse_mode: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _slack_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


DIALECTS: dict[str, Dialect] = {
    PLAIN: Dialect(name=PLAIN),
    TELEGRAM_HTML: Dialect(
        name=TELEGRAM_HTML,
        escape=_html_escape,
        bold=lambda value: f"<b>{value}</b>",
        italic=lambda value: f"<i>{value}</i>",
        strike=lambda value: f"<s>{value}</s>",
        code=lambda value: f"<code>{value}</code>",
        link=lambda text, url: f'<a href="{url}">{text}</a>',
        heading=lambda _level, value: f"<b>{value}</b>",
        quote=lambda value: f"<blockquote>{value}</blockquote>",
        code_block=lambda body, lang: (
            f'<pre><code class="language-{lang}">{body}</code></pre>'
            if lang
            else f"<pre>{body}</pre>"
        ),
        chunk_limit=3500,
        parse_mode="HTML",
    ),
    SLACK: Dialect(
        name=SLACK,
        escape=_slack_escape,
        bold=lambda value: f"*{value}*",
        italic=lambda value: f"_{value}_",
        strike=lambda value: f"~{value}~",
        code=lambda value: f"`{value}`",
        link=lambda text, url: f"<{url}|{text}>" if text else f"<{url}>",
        heading=lambda _level, value: f"*{value}*",
        quote=lambda value: f"&gt; {value}",
        code_block=lambda body, _lang: f"```\n{body}\n```",
        chunk_limit=2900,
    ),
    FEISHU: Dialect(
        name=FEISHU,
        bold=lambda value: f"**{value}**",
        italic=lambda value: f"*{value}*",
        strike=lambda value: f"~~{value}~~",
        code=lambda value: f"`{value}`",
        link=lambda text, url: f"[{text}]({url})" if text else url,
        heading=lambda level, value: f"{'#' * min(max(level, 1), 3)} {value}",
        quote=lambda value: f"> {value}",
        code_block=lambda body, lang: f"```{lang}\n{body}\n```",
        bullet="- ",
        chunk_limit=3800,
    ),
    WECOM: Dialect(
        name=WECOM,
        bold=lambda value: f"**{value}**",
        code=lambda value: f"`{value}`",
        link=lambda text, url: f"[{text}]({url})" if text else url,
        heading=lambda level, value: f"{'#' * level} {value}",
        quote=lambda value: f"> {value}",
        # App messages have no fenced code; keep the payload readable instead.
        code_block=lambda body, _lang: body,
        tables_as_code=False,
        chunk_limit=1800,
    ),
    DINGTALK: Dialect(
        name=DINGTALK,
        bold=lambda value: f"**{value}**",
        italic=lambda value: f"*{value}*",
        link=lambda text, url: f"[{text}]({url})" if text else url,
        heading=lambda level, value: f"{'#' * min(level, 6)} {value}",
        quote=lambda value: f"> {value}",
        code_block=lambda body, _lang: body,
        bullet="- ",
        tables_as_code=False,
        chunk_limit=4000,
    ),
    DISCORD: Dialect(
        name=DISCORD,
        bold=lambda value: f"**{value}**",
        italic=lambda value: f"*{value}*",
        strike=lambda value: f"~~{value}~~",
        code=lambda value: f"`{value}`",
        link=lambda text, url: f"[{text}]({url})" if text else url,
        heading=lambda level, value: f"{'#' * min(level, 3)} {value}",
        quote=lambda value: f"> {value}",
        code_block=lambda body, lang: f"```{lang}\n{body}\n```",
        bullet="- ",
        chunk_limit=1900,
    ),
    # QQ Bot Markdown requires separate platform approval. Keep the native
    # syntax here; the adapter owns the permission-aware plain-text fallback.
    # A conservative 3,800-char chunk leaves room below the 4,000-char limit.
    QQ: Dialect(
        name=QQ,
        bold=lambda value: f"**{value}**",
        italic=lambda value: f"*{value}*",
        strike=lambda value: f"~~{value}~~",
        code=lambda value: f"`{value}`",
        link=lambda text, url: f"[{text}]({url})" if text else url,
        heading=lambda level, value: f"{'#' * min(max(level, 1), 3)} {value}",
        quote=lambda value: f"> {value}",
        code_block=lambda body, lang: f"```{lang}\n{body}\n```",
        bullet="- ",
        chunk_limit=3800,
        tables_as_code=True,
    ),
}


def _blocks(text: str) -> list[dict[str, Any]]:
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE_RE.match(line)
        if fence is not None:
            marker = fence.group("fence")[0]
            body: list[str] = []
            index += 1
            while index < len(lines):
                closing = _FENCE_RE.match(lines[index])
                if closing is not None and closing.group("fence")[0] == marker:
                    index += 1
                    break
                body.append(lines[index])
                index += 1
            blocks.append({
                "type": "code",
                "lang": fence.group("lang") or "",
                "lines": body,
            })
            continue
        if not line.strip():
            index += 1
            continue
        if _HR_RE.match(line):
            blocks.append({"type": "hr"})
            index += 1
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None:
            blocks.append({
                "type": "heading",
                "level": len(heading.group("level")),
                "text": heading.group("body"),
            })
            index += 1
            continue
        if (
            _TABLE_ROW_RE.match(line)
            and index + 1 < len(lines)
            and _TABLE_SEP_RE.match(lines[index + 1])
        ):
            rows: list[list[str]] = []
            while index < len(lines) and _TABLE_ROW_RE.match(lines[index]):
                if not _TABLE_SEP_RE.match(lines[index]):
                    rows.append([
                        cell.strip()
                        for cell in lines[index].strip().strip("|").split("|")
                    ])
                index += 1
            blocks.append({"type": "table", "rows": rows})
            continue
        quote = _QUOTE_RE.match(line)
        if quote is not None:
            body = []
            while index < len(lines):
                match = _QUOTE_RE.match(lines[index])
                if match is None:
                    break
                body.append(match.group("body"))
                index += 1
            blocks.append({"type": "quote", "lines": body})
            continue
        item = _LIST_RE.match(line)
        if item is not None:
            items: list[dict[str, Any]] = []
            while index < len(lines):
                match = _LIST_RE.match(lines[index])
                if match is None:
                    if lines[index].strip() and items:
                        items[-1]["text"] += " " + lines[index].strip()
                        index += 1
                        continue
                    break
                marker = match.group("marker")
                items.append({
                    "indent": len(match.group("indent")) // 2,
                    "ordered": marker[0].isdigit(),
                    "marker": marker,
                    "text": match.group("body"),
                })
                index += 1
            blocks.append({"type": "list", "items": items})
            continue
        paragraph: list[str] = []
        while index < len(lines):
            current = lines[index]
            if (
                not current.strip()
                or _HR_RE.match(current)
                or _HEADING_RE.match(current)
                or _FENCE_RE.match(current)
                or _QUOTE_RE.match(current)
                or _LIST_RE.match(current)
                or _TABLE_ROW_RE.match(current)
            ):
                break
            paragraph.append(current.strip())
            index += 1
        if paragraph:
            blocks.append({"type": "paragraph", "lines": paragraph})
    return blocks


def render_inline(text: str, dialect: Dialect) -> str:
    """Render one inline span, escaping every literal fragment for the target."""
    out: list[str] = []
    position = 0
    source = str(text or "")
    for match in _INLINE_RE.finditer(source):
        out.append(dialect.escape(source[position:match.start()]))
        position = match.end()
        if match.group("code") is not None:
            out.append(dialect.code(dialect.escape(match.group("code"))))
        elif match.group("link_url") is not None:
            label = render_inline(match.group("link_text") or "", dialect)
            url = dialect.escape(match.group("link_url"))
            out.append(dialect.link(label, url))
        elif (strong := match.group("strong") or match.group("strong_underscore")) is not None:
            out.append(dialect.bold(render_inline(strong, dialect)))
        elif match.group("strike") is not None:
            out.append(dialect.strike(render_inline(match.group("strike"), dialect)))
        elif (em := match.group("em") or match.group("em_underscore")) is not None:
            out.append(dialect.italic(render_inline(em, dialect)))
    out.append(dialect.escape(source[position:]))
    return "".join(out)


def _table_lines(rows: list[list[str]], dialect: Dialect) -> list[str]:
    if not rows:
        return []
    plain = DIALECTS[PLAIN]
    width = max(len(row) for row in rows)
    cells = [
        [render_inline(row[column] if column < len(row) else "", plain) for column in range(width)]
        for row in rows
    ]
    widths = [
        max(_display_width(row[column]) for row in cells)
        for column in range(width)
    ]
    lines = [
        " | ".join(_pad(row[column], widths[column]) for column in range(width)).rstrip()
        for row in cells
    ]
    if len(lines) > 1:
        lines.insert(1, "-" * min(_display_width(lines[0]), 80))
    return lines


def _render_block(block: dict[str, Any], dialect: Dialect) -> str:
    kind = str(block.get("type") or "")
    if kind == "code":
        body = "\n".join(block.get("lines") or [])
        if dialect.name == TELEGRAM_HTML:
            body = dialect.escape(body)
        return dialect.code_block(body, str(block.get("lang") or ""))
    if kind == "heading":
        return dialect.heading(
            int(block.get("level") or 1),
            render_inline(str(block.get("text") or ""), dialect),
        )
    if kind == "hr":
        return dialect.hr
    if kind == "quote":
        rendered = [
            render_inline(line, dialect)
            for line in block.get("lines") or []
        ]
        if dialect.name == TELEGRAM_HTML:
            return dialect.quote("\n".join(rendered))
        return "\n".join(dialect.quote(line) for line in rendered)
    if kind == "list":
        lines = []
        for item in block.get("items") or []:
            indent = "  " * int(item.get("indent") or 0)
            marker = (
                f"{item.get('marker')} "
                if item.get("ordered")
                else dialect.bullet
            )
            lines.append(
                f"{indent}{marker}{render_inline(str(item.get('text') or ''), dialect)}"
            )
        return "\n".join(lines)
    if kind == "table":
        lines = _table_lines(list(block.get("rows") or []), dialect)
        if not lines:
            return ""
        if dialect.tables_as_code:
            body = "\n".join(lines)
            if dialect.name == TELEGRAM_HTML:
                body = dialect.escape(body)
            return dialect.code_block(body, "")
        return "\n".join(dialect.escape(line) for line in lines)
    return "\n".join(
        render_inline(line, dialect)
        for line in block.get("lines") or []
    )


def render_markdown(text: str, dialect_name: str) -> str:
    """Render assistant Markdown into ``dialect_name``.

    ``RAW`` passes the source through untouched; ``PLAIN`` strips every marker
    into readable text for surfaces that render nothing at all.
    """
    if dialect_name == RAW or dialect_name not in DIALECTS:
        return str(text or "")
    dialect = DIALECTS[dialect_name]
    rendered = [
        chunk for chunk in (
            _render_block(block, dialect) for block in _blocks(text)
        ) if chunk
    ]
    return "\n\n".join(rendered)


def markdown_table_parts(text: str, dialect_name: str = FEISHU) -> list[dict[str, Any]]:
    """Split assistant Markdown into dialect text blobs and raw table rows.

    Providers that have a native table component (Feishu cards, Slack Block Kit)
    lift the ``table`` parts; everyone else can still render the markdown parts.
    """
    dialect = DIALECTS.get(dialect_name) or DIALECTS[PLAIN]
    parts: list[dict[str, Any]] = []
    markdown_chunks: list[str] = []

    def flush() -> None:
        content = "\n\n".join(chunk for chunk in markdown_chunks if chunk).strip()
        markdown_chunks.clear()
        if content:
            parts.append({"kind": "markdown", "content": content})

    for block in _blocks(text):
        if str(block.get("type") or "") == "table":
            rows = [list(row) for row in list(block.get("rows") or []) if row]
            if rows:
                flush()
                parts.append({"kind": "table", "rows": rows})
                continue
        rendered = _render_block(block, dialect)
        if rendered.strip():
            markdown_chunks.append(rendered)
    flush()
    return parts


def feishu_markdown_parts(text: str) -> list[dict[str, Any]]:
    """Feishu card JSON 2.0 still cannot render pipe tables inside markdown."""
    return markdown_table_parts(text, FEISHU)


def _split_long(value: str, limit: int) -> list[str]:
    parts: list[str] = []
    remainder = value
    while len(remainder) > limit:
        cut = remainder.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(remainder[:cut])
        remainder = remainder[cut:].lstrip("\n")
    if remainder:
        parts.append(remainder)
    return parts


def render_markdown_chunks(
    text: str,
    dialect_name: str,
    limit: int = 0,
) -> list[str]:
    """Render into messages of at most ``limit`` characters.

    Chunking happens between rendered blocks so a message never ends inside an
    HTML tag or an unbalanced code fence, which providers reject outright.
    """
    dialect = DIALECTS.get(dialect_name)
    size = limit or (dialect.chunk_limit if dialect else 0) or 4000
    if dialect is None or dialect_name == RAW:
        return _split_long(str(text or ""), size) or [""]
    rendered = [
        chunk for chunk in (
            _render_block(block, dialect) for block in _blocks(text)
        ) if chunk
    ]
    chunks: list[str] = []
    current = ""
    for piece in rendered:
        for part in ([piece] if len(piece) <= size else _split_long(piece, size)):
            if not current:
                current = part
            elif len(current) + 2 + len(part) <= size:
                current = f"{current}\n\n{part}"
            else:
                chunks.append(current)
                current = part
    if current:
        chunks.append(current)
    return chunks or [""]


def dialect_for(provider: str) -> str:
    """Map a channel provider onto its Markdown dialect."""
    return {
        "telegram": TELEGRAM_HTML,
        "slack": SLACK,
        "feishu": FEISHU,
        "wecom": WECOM,
        "dingtalk": DINGTALK,
        "discord": DISCORD,
    }.get(str(provider or "").lower(), RAW)
