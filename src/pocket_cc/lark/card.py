"""Lark interactive-card templates.

Pure functions that build the card dict structure Lark's `/im/v1/messages`
endpoint accepts (`msg_type=interactive`, `content` is the JSON of this dict).
No I/O — `LarkClient` is responsible for actually sending it.

We use the **legacy** card schema (no top-level `schema: "2.0"`) because:
  - Lark's PATCH endpoint accepts both, but legacy renders identically across
    older and newer Lark client versions.
  - All elements we need (markdown, action+button, hr, expandable note) work
    in the legacy schema without quirks.

Schema docs: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/im-v1/message-card/overview
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Literal

CardState = Literal["running", "done", "failed", "waiting"]

# state → (header template color, emoji prefix)
_STATE_STYLE: dict[CardState, tuple[str, str]] = {
    "running": ("blue", "⏳"),
    "done": ("green", "✅"),
    "failed": ("red", "❌"),
    "waiting": ("orange", "❓"),
}

ButtonStyle = Literal["default", "primary", "danger"]


@dataclass(frozen=True, slots=True)
class CardButton:
    """A clickable button rendered in a card's action row.

    The ``value`` dict is what comes back to us in
    `P2CardActionTrigger.event.action.value` when the user taps it — design
    your routing scheme around that field (e.g. `{"action": "cancel"}`).
    """

    text: str
    value: dict[str, Any]
    style: ButtonStyle = "default"


@dataclass(frozen=True, slots=True)
class ExpandableSection:
    """A collapsed section users can tap to expand. Useful for tool-call details."""

    label: str  # e.g. "Tool calls (4)"
    content: str  # markdown body


def build_status_card(
    *,
    title: str,
    body: str,
    state: CardState = "running",
    actions: list[CardButton] | None = None,
    detail: ExpandableSection | None = None,
) -> dict[str, Any]:
    """Build a status-style card with a colored header, body and optional buttons.

    Args:
        title: Header text (emoji prefix added automatically based on state).
        body: Main content. Markdown supported (bold, lists, code).
        state: Visual state — controls header color and emoji prefix.
        actions: Optional row of buttons (max 4 fits comfortably on mobile).
        detail: Optional expandable section for verbose information that
            shouldn't be in the always-visible body.

    Returns:
        A dict ready to be JSON-serialized into a Lark interactive message's
        ``content`` field.
    """
    color, emoji = _STATE_STYLE[state]
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": normalize_markdown_for_lark(body)},
    ]

    if detail is not None:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": f"▸ {detail.label}"}],
            }
        )
        elements.append({"tag": "markdown", "content": normalize_markdown_for_lark(detail.content)})

    if actions:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "action",
                "actions": [_render_button(b) for b in actions],
            }
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": f"{emoji} {title}"},
        },
        "elements": elements,
    }


def build_text_card(*, body: str) -> dict[str, Any]:
    """Build a minimal card with just markdown body — useful for one-off notices.

    Headerless, action-less. Use for things like 'session ended' confirmations.
    """
    return {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "markdown", "content": normalize_markdown_for_lark(body)}],
    }


# ----------------------------------------------------------- markdown normalize
# Lark's interactive-card `markdown` tag renders only a subset of GFM:
#   ✓ **bold** / *italic* / `code` / ```code block``` / [link](…) / - lists /
#     ~~strikethrough~~ / line breaks / <font color="…">
#   ✗ # / ## / ### headings (rendered as literal text)
#   ✗ GFM tables `| a | b |` (rendered as literal text)
# Claude Code's responses use both freely (especially when summarizing a
# project — headings + tables). Without normalization the user sees the
# raw markdown source, which is what triggered this fix. Anything the user
# really needs in original form is still available via the [📜 内容] button.

_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(\s*)#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_TABLE_ROW_RE: Final[re.Pattern[str]] = re.compile(r"^\s*\|.*\|\s*$")
# Separator row: `|---|---|` or `| :--- | ---: |` etc.
_TABLE_SEP_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$"
)


def normalize_markdown_for_lark(text: str) -> str:
    """Rewrite Claude/GFM-style markdown into Lark-renderable equivalents.

    Idempotent — calling twice yields the same result as once. Safe to apply
    to already-Lark-compatible text (no-op for content without headings or
    tables).
    """
    if not text:
        return text
    text = _HEADING_RE.sub(_heading_to_bold, text)
    text = _convert_tables(text)
    return text


def _heading_to_bold(match: re.Match[str]) -> str:
    """Replace `## content` with `**content**`, defensively unwrapping any
    bold/underline markers Claude already put around the heading text.

    Claude Code commonly writes `### **Section Title**` — naively re-bolding
    that yields `****Section Title****` which Lark renders as a literal
    `****` trailing the title. We strip whole-string `**…**` / `__…__`
    layers before re-wrapping so the final markup is always exactly one
    pair of `**`.
    """
    indent = match.group(1)
    content = match.group(2).strip()
    # Peel any number of whole-string bold/underline wrappers
    while len(content) >= 4 and (
        (content.startswith("**") and content.endswith("**"))
        or (content.startswith("__") and content.endswith("__"))
    ):
        content = content[2:-2].strip()
    if not content:
        return indent  # weird input like `### ****` → drop to just whitespace
    return f"{indent}**{content}**"


def _convert_tables(text: str) -> str:
    """Walk the text line-by-line; replace GFM tables with bulleted lists.

    A GFM table = header row + separator row (---) + zero or more data rows.
    We render each data row as a list item, with the header-named cells
    folded in as `**Header**: value` pairs. 2-column key/value tables get
    the cleanest output; wider tables fall back to ` · ` separated.

    Preserves a trailing newline if the input had one (splitlines + join
    would otherwise eat it).
    """
    if not text:
        return text
    has_trailing_newline = text.endswith("\n")
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        # Look for header + separator pattern starting at i.
        if i + 1 < n and _TABLE_ROW_RE.match(lines[i]) and _TABLE_SEP_RE.match(lines[i + 1]):
            header = _parse_row(lines[i])
            i += 2  # consume header + separator
            while i < n and _TABLE_ROW_RE.match(lines[i]):
                row = _parse_row(lines[i])
                if row:
                    out.append(_render_row_as_list_item(header, row))
                i += 1
            continue
        out.append(lines[i])
        i += 1
    result = "\n".join(out)
    return result + "\n" if has_trailing_newline else result


def _parse_row(line: str) -> list[str]:
    """Split `| a | b | c |` into ['a', 'b', 'c']. Empty trailing cells dropped."""
    stripped = line.strip()
    # Strip leading + trailing |
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = [c.strip() for c in stripped.split("|")]
    # Drop empty trailing cells (some tables pad with empty columns)
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _render_row_as_list_item(header: list[str], row: list[str]) -> str:
    """Render one table row as a markdown list item.

    Examples:
        header=['模块', '说明'], row=['ploto-bff', 'Backend for Frontend 层']
          → '- **模块**: ploto-bff · **说明**: Backend for Frontend 层'
        header=['x'], row=['hello']
          → '- **x**: hello'
        header=[], row=['hello']
          → '- hello'
    """
    if not row:
        return ""
    if not header:
        return "- " + " · ".join(row)
    pairs: list[str] = []
    for i, value in enumerate(row):
        if i < len(header) and header[i]:
            pairs.append(f"**{header[i]}**: {value}")
        else:
            pairs.append(value)
    return "- " + " · ".join(pairs)


# -------------------------------------------------------------------- helpers


def _render_button(button: CardButton) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": button.text},
        "type": button.style,
        "value": dict(button.value),  # defensive copy — Lark mutates this server-side
    }


# Re-export the default action set we'll need everywhere as a convenience.
# Keep this list short — every new button is one more route in the relay layer.
DEFAULT_RUNNING_ACTIONS: tuple[CardButton, ...] = (
    CardButton(text="⏹ 中断", value={"action": "cancel"}, style="danger"),
    # ⎋ Esc fires *two* Escape keys 100ms apart. Claude TUI requires double
    # Esc to fully abort a prompt + clear the input box; firing it from a
    # single Lark button click avoids the user having to double-tap (which
    # Lark rate-limits with "操作太频繁了" after the second click).
    CardButton(
        text="⎋ Esc",
        value={
            "action": "key_sequence",
            "keys": ["Escape", "Escape"],
            "delay_ms": 100,
        },
    ),
    CardButton(text="⇧⭾ Mode", value={"action": "key", "key": "BTab"}),
    CardButton(text="📜 内容", value={"action": "show_pane"}),
)
