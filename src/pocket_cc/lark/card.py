"""Lark interactive-card templates (Schema 2.0 / cardkit).

Pure functions that build card dicts. No I/O — :class:`LarkClient` is what
actually delivers them.

* :func:`build_status_card_v2` / :func:`build_text_card_v2` /
  :func:`build_restart_notice_card_v2` produce the
  ``schema/config/header/body`` shape consumed by
  ``POST /cardkit/v1/cards`` and the streaming PUT endpoints. Stable
  ``element_id`` constants (:data:`ELEMENT_ID_BODY`,
  :data:`ELEMENT_ID_DETAIL_*`, :data:`ELEMENT_ID_ACTIONS`) let the
  streaming layer target the body markdown by name without parsing the
  JSON back.

* :func:`normalize_markdown_for_lark` rewrites Claude/GFM markdown into
  the subset Lark's v2 ``markdown`` element actually renders — same idea
  as before the cardkit migration. v2 supports headings natively in
  theory, but GFM pipe-tables are still silently dropped by Lark's
  renderer, so we keep this normalization step (downgrades tables to
  bullet lists / fixed-width code blocks). Callers in
  :mod:`pocket_cc.relay.card_renderer` apply it to body / detail content
  before handing them to the v2 builders.

Cardkit Schema 2.0 docs: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/feishu-cards/card-overview
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Final, Literal

CardState = Literal["running", "done", "failed", "waiting", "cancelled"]

# state → (header template color, emoji prefix)
_STATE_STYLE: dict[CardState, tuple[str, str]] = {
    "running": ("blue", "⏳"),
    "done": ("green", "✅"),
    "failed": ("red", "❌"),
    "waiting": ("orange", "❓"),
    "cancelled": ("grey", "⏹"),
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


# ----------------------------------------------------------- markdown normalize
# Lark's Schema 2.0 ``markdown`` element renders most of GFM, but **GFM
# pipe-tables are silently dropped** (confirmed on a real device after the
# cardkit migration — same downgrade behavior as legacy). Heading support
# in v2 has not been verified yet; we keep converting ``## heading`` →
# ``**heading**`` conservatively so the migration didn't change the visual.
# A future Phase 4-B can drop the heading branch after a real-device
# A/B test.
#
# What v2 markdown supports natively (no normalization needed):
#   ✓ **bold** / *italic* / `code` / ```code block``` / [link](…) / - lists /
#     ~~strikethrough~~ / line breaks / <font color="…">
# What we still rewrite here:
#   • # / ## / ### headings → **bold** (conservative, pending verification)
#   • GFM tables `| a | b |` → bullet list or fixed-width code block
# Claude Code's responses use both freely (especially when summarizing a
# project — headings + tables). Without normalization the user sees the
# raw markdown source. Anything the user really needs in original form is
# still available via the [📜 内容] button.

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
            data_rows: list[list[str]] = []
            while i < n and _TABLE_ROW_RE.match(lines[i]):
                row = _parse_row(lines[i])
                if row:
                    data_rows.append(row)
                i += 1
            # Decide rendering mode for the *whole* table — mixing code-block
            # and bullet rows in one table would look incoherent.
            if _should_use_code_block_table(header, data_rows):
                out.append(_render_as_code_block_table(header, data_rows))
            else:
                for row in data_rows:
                    out.append(_render_row_as_list_item(header, row))
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


# ---- code-block table rendering --------------------------------------------
# When a table's cells are short and free of inline markdown, rendering it as
# a fenced ``` block lets Lark's monospace font preserve column alignment —
# users *see* a table instead of a bullet list. Falls back to bullets when:
#   - only one column (the bullet form is already cleaner)
#   - any cell contains inline markdown (bold / code / link) that would print
#     as literal text inside a code fence — a regression vs the bullet form
#   - any cell exceeds ``_CODE_BLOCK_CELL_WIDTH_LIMIT`` display chars (long
#     prose would force horizontal scroll on mobile, worse than wrapping
#     bullets)

_CODE_BLOCK_CELL_WIDTH_LIMIT: Final[int] = 24

# Inline markdown markers whose literals would survive a code fence and look
# wrong: ``**bold**``, ``__under__``, `` `code` ``, ``[text](url)``.
_INLINE_MD_IN_CELL_RE: Final[re.Pattern[str]] = re.compile(r"\*\*|__|`|\[.+?\]\(")


def _display_width(s: str) -> int:
    """Monospace display width: CJK / fullwidth = 2 cells, other = 1.

    Uses :func:`unicodedata.east_asian_width` — ``W`` (Wide) / ``F`` (Fullwidth)
    get 2, everything else 1. Good enough for the alignment we need; doesn't
    handle combining marks (rare in Claude output) or emoji (variable across
    fonts but ASCII-width is usually close enough).
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _should_use_code_block_table(header: list[str], rows: list[list[str]]) -> bool:
    if len(header) < 2:
        return False
    all_cells = [*header, *(c for r in rows for c in r)]
    if any(_INLINE_MD_IN_CELL_RE.search(c) for c in all_cells):
        return False
    return not any(_display_width(c) > _CODE_BLOCK_CELL_WIDTH_LIMIT for c in all_cells)


def _render_as_code_block_table(header: list[str], rows: list[list[str]]) -> str:
    """Render a header + rows as a fenced code block with pipe-aligned columns.

    Example output (back-tick-fenced)::

        模块            | 说明
        ------------- | ----------------------
        ploto-bff     | Backend for Frontend 层
        ploto-monitor | 监控指标功能
    """
    n_cols = len(header)
    col_widths = [_display_width(h) for h in header]
    for row in rows:
        for i in range(min(n_cols, len(row))):
            col_widths[i] = max(col_widths[i], _display_width(row[i]))

    def _pad(cell: str, width: int) -> str:
        # Pad right with spaces, accounting for east-asian width.
        return cell + " " * (width - _display_width(cell))

    sep_cells = ["-" * w for w in col_widths]
    body_lines = [" | ".join(_pad(h, w) for h, w in zip(header, col_widths, strict=True))]
    body_lines.append(" | ".join(sep_cells))
    for row in rows:
        padded = list(row) + [""] * max(0, n_cols - len(row))
        body_lines.append(
            " | ".join(
                _pad(c, w) for c, w in zip(padded[:n_cols], col_widths, strict=True)
            )
        )
    return "```\n" + "\n".join(body_lines) + "\n```"


# -------------------------------------------------------------------- helpers


def build_running_actions(mode_suffix: str = "") -> tuple[CardButton, ...]:
    """Return the running-state action row, with an optional Mode-button suffix.

    The Mode button cycles Claude's permission mode via Shift-Tab (BTab).
    Without feedback, users in Lark have no way to tell which mode Claude
    is in — passing the current mode label here surfaces it as
    "⇧⭾ Mode · 自动接受" / "…计划" / etc. The relay's card renderer
    builds this per-card using the latest transcript-derived mode.

    Other buttons are static — neither their behavior nor their label
    depends on per-turn state, so keeping them defined inline here makes
    the action row trivial to inspect.
    """
    mode_text = "⇧⭾ Mode" if not mode_suffix else f"⇧⭾ Mode · {mode_suffix}"
    return (
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
        CardButton(text=mode_text, value={"action": "key", "key": "BTab"}),
        CardButton(text="📜 内容", value={"action": "show_pane"}),
    )


# Generic action set used by callers that don't care about the current
# permission mode (e.g. quick utility cards). The relay layer prefers
# `build_running_actions(mode_label(...))` so the Mode button reflects state.
DEFAULT_RUNNING_ACTIONS: tuple[CardButton, ...] = build_running_actions()


# =========================================================================
# Schema 2.0 (cardkit) builders — Phase 2 of the cardkit migration.
# Coexist with the legacy builders above; not yet wired up to the renderer.
# =========================================================================


# Stable element ids assigned by the v2 builders. The streaming PUT endpoint
# (``stream_element_content``) targets ``ELEMENT_ID_BODY`` exclusively to
# append assistant text without re-sending the whole card; whole-card state
# transitions (running → done, waiting card swap) go through
# ``update_card_entity`` instead. Keep these names stable: Phase 3's
# CardStream will hardcode them.
ELEMENT_ID_BODY: Final[str] = "body"
ELEMENT_ID_DETAIL_DIVIDER: Final[str] = "detail_divider"
ELEMENT_ID_DETAIL_LABEL: Final[str] = "detail_label"
ELEMENT_ID_DETAIL_CONTENT: Final[str] = "detail_content"
ELEMENT_ID_ACTIONS_DIVIDER: Final[str] = "actions_divider"
ELEMENT_ID_ACTIONS: Final[str] = "actions"


def build_status_card_v2(
    *,
    title: str,
    body: str,
    state: CardState = "running",
    actions: list[CardButton] | None = None,
    detail: ExpandableSection | None = None,
) -> dict[str, Any]:
    """Build the cardkit Schema 2.0 equivalent of :func:`build_status_card`.

    Differences from the legacy builder a caller must know about:

    * Top-level ``schema: "2.0"`` declares the new format.
    * ``config.streaming_mode`` is ``True`` for non-terminal states (running /
      waiting) so the cardkit streaming PUT endpoint accepts updates;
      terminal states (done / failed / cancelled) emit ``False`` so the IM
      list-preview / forwarding behavior settles.
    * ``config.summary.content`` carries the chat-list preview string —
      derived from ``title`` so the preview reads as "{emoji} {title}"
      without separate plumbing.
    * Body elements live under ``body.elements`` (not top-level ``elements``)
      and the body markdown carries :data:`ELEMENT_ID_BODY` so streaming
      updates can target it by name.
    * The v1 ``action`` container is gone in v2 — buttons go directly into
      ``body.elements``; a multi-button row uses a ``column_set`` with one
      ``column`` per button.
    * Headings (``# / ##``) and other GFM features the legacy ``markdown``
      tag dropped are rendered natively in v2 — callers can stop
      pre-normalizing body content for those once Phase 4 lands.
    """
    color, emoji = _STATE_STYLE[state]
    is_streaming = state in ("running", "waiting")

    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "element_id": ELEMENT_ID_BODY, "content": body},
    ]

    if detail is not None:
        elements.append({"tag": "hr", "element_id": ELEMENT_ID_DETAIL_DIVIDER})
        # v2 dropped the ``note`` tag — flatten to a grey markdown line so the
        # visual weight matches what legacy emitted.
        elements.append(
            {
                "tag": "markdown",
                "element_id": ELEMENT_ID_DETAIL_LABEL,
                "content": f"<font color='grey'>▸ {detail.label}</font>",
            }
        )
        elements.append(
            {
                "tag": "markdown",
                "element_id": ELEMENT_ID_DETAIL_CONTENT,
                "content": detail.content,
            }
        )

    if actions:
        elements.append({"tag": "hr", "element_id": ELEMENT_ID_ACTIONS_DIVIDER})
        elements.append(_render_button_row_v2(actions))

    summary_text = f"{emoji} {title}"
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": is_streaming,
            # ``summary.content`` is the chat-list / notification preview.
            # Empty string is valid; we feed the title-with-emoji so previews
            # are scannable without opening the card.
            "summary": {"content": summary_text},
            # ``wide_screen_mode`` migrated to ``enable_forward`` / similar
            # in v2; the legacy key is no-op here and we omit it.
        },
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": summary_text},
        },
        "body": {"elements": elements},
    }


def build_text_card_v2(*, body: str) -> dict[str, Any]:
    """v2 equivalent of :func:`build_text_card` — headerless, action-less.

    No streaming_mode (these cards are one-shot notices), no summary header
    overlay. Useful for "session ended" / orphan-restart notice cases.
    """
    return {
        "schema": "2.0",
        "config": {},
        "body": {
            "elements": [
                {"tag": "markdown", "element_id": ELEMENT_ID_BODY, "content": body},
            ],
        },
    }


def build_restart_notice_card_v2() -> dict[str, Any]:
    """v2 equivalent of :func:`build_restart_notice_card`.

    Same intent as legacy: visually close out a card the previous (dead)
    process left in a ⏳ state by flipping it to ⏹ cancelled with a notice.
    """
    return build_status_card_v2(
        title="pocket-cc 已重启",
        body="⚠️ 上一轮的状态未能保留——可发送新消息重新开始。",
        state="cancelled",
    )


def _render_button_row_v2(actions: list[CardButton]) -> dict[str, Any]:
    """Pack N buttons into a v2 ``column_set`` (the v2 multi-button idiom).

    Each column contains one button. The cardkit renderer lays them out
    side-by-side; on mobile they wrap as needed.
    """
    columns = [
        {"tag": "column", "elements": [_render_button_v2(b)]} for b in actions
    ]
    return {
        "tag": "column_set",
        "element_id": ELEMENT_ID_ACTIONS,
        "columns": columns,
    }


def _render_button_v2(button: CardButton) -> dict[str, Any]:
    """v2 button element — shape mirrors legacy ``_render_button``.

    The cardkit callback action picks up ``value`` verbatim, so the
    ``card.action.trigger_v1`` event routing in :mod:`event_loop` keeps
    working unchanged. ``type`` is the style key (default / primary /
    danger), same as legacy.
    """
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": button.text},
        "type": button.style,
        "value": dict(button.value),  # defensive copy — server may mutate
    }
