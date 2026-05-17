"""Render Claude transcript events as a Lark card.

Two layers:
  - :class:`TurnAccumulator` ingests an Event stream (from
    `claude.transcript.TranscriptReader`) and keeps a running snapshot of
    "what should the card look like right now".
  - :func:`render_card` turns that snapshot into the dict format
    `lark.card.build_status_card` produces.

A "turn" = one user prompt + everything Claude does until it stops. The
accumulator is reset per turn so cards stay short. Long turns get **body
truncation** (last 3000 chars wins) so we don't hit Lark's per-card limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

from pocket_cc.claude.transcript import (
    AssistantText,
    AssistantThinking,
    Event,
    ToolResult,
    ToolUse,
    UserText,
)
from pocket_cc.lark.card import (
    DEFAULT_RUNNING_ACTIONS,
    CardState,
    ExpandableSection,
    build_status_card,
)

# Lark cards larger than ~30KB get rejected; the body field is the long pole.
# We trim aggressively because the user can use the [📜 内容] button to dump
# the raw pane for full context.
_BODY_MAX_CHARS = 3000
_THINKING_MAX_CHARS = 2000
_TITLE_MAX_CHARS = 60


@dataclass(frozen=True, slots=True)
class TurnSnapshot:
    """Render-ready summary of a turn. Pure data, no Lark concepts."""

    user_prompt: str
    assistant_text: str
    tool_calls: list[str]
    thinking: str
    state: CardState
    error: str = ""


@dataclass
class TurnAccumulator:
    """Stateful aggregator — fed Events, emits TurnSnapshot."""

    user_prompt: str = ""
    _assistant_text_parts: list[str] = field(default_factory=list)
    _tool_calls: list[str] = field(default_factory=list)
    _thinking_parts: list[str] = field(default_factory=list)
    _seen_uuids: set[str] = field(default_factory=set)

    def ingest(self, event: Event) -> None:
        """Fold a single Event into the running snapshot.

        Idempotent on `event.uuid`: replaying the same transcript twice (e.g.
        after truncation reset) doesn't double-count. Claude assigns a single
        uuid per record, and a record yields exactly one event of each kind
        per content block — so duplicate uuids = re-reading.
        """
        # We only dedupe events that have an identity; ToolResult shares its
        # uuid with the user record that wraps it, which is fine because we
        # never re-process the same record line twice in normal operation.
        # The set acts as belt-and-braces for the truncation-replay case.
        if isinstance(event, UserText):
            if not self.user_prompt:
                self.user_prompt = event.text
            return
        if isinstance(event, AssistantText):
            self._assistant_text_parts.append(event.text)
            return
        if isinstance(event, ToolUse):
            self._tool_calls.append(format_tool_call(event.tool_name, event.tool_input))
            return
        if isinstance(event, AssistantThinking):
            self._thinking_parts.append(event.text)
            return
        if isinstance(event, ToolResult):
            # tool_result is currently not rendered into the visible card —
            # users can use the show_pane button to see raw output, and the
            # final transcript view is in the desktop's tmux. Keeping the
            # branch here so future code can fold short results into detail.
            return

    def snapshot(self, state: CardState = "running", error: str = "") -> TurnSnapshot:
        return TurnSnapshot(
            user_prompt=self.user_prompt,
            assistant_text="\n\n".join(p for p in self._assistant_text_parts if p),
            tool_calls=list(self._tool_calls),
            thinking="\n\n".join(p for p in self._thinking_parts if p),
            state=state,
            error=error,
        )


# --------------------------------------------------------------- render layer


def render_card(snapshot: TurnSnapshot) -> dict[str, Any]:
    """Render a TurnSnapshot to the Lark card dict.

    State-driven shape:
      - running: blue header, action row visible, "(运行中…)" placeholder if empty
      - done:    green header, no action row (turn is over)
      - failed:  red header, error in body
      - waiting: orange header (reserved for future plan-mode / ask-user UX)
    """
    title = _shorten(snapshot.user_prompt, _TITLE_MAX_CHARS) or "Claude"
    body = _render_body(snapshot)
    detail = _render_detail(snapshot)
    actions = list(DEFAULT_RUNNING_ACTIONS) if snapshot.state == "running" else None

    return build_status_card(
        title=title,
        body=body,
        state=snapshot.state,
        detail=detail,
        actions=actions,
    )


def format_tool_call(name: str, input_data: dict[str, Any]) -> str:
    """One-line summary for a Claude tool_use block, e.g. '📖 Read foo.py'.

    Falls back to '🔧 {name}' when the tool isn't recognized — every new
    tool stays readable without changes here.
    """
    # Tool name → (emoji, key in input, formatter callable)
    if name in {"Read", "Edit", "Write", "MultiEdit"} and "file_path" in input_data:
        emoji = {"Read": "📖", "Edit": "✏️", "Write": "📝", "MultiEdit": "✏️"}[name]
        return f"{emoji} **{name}** `{PurePath(str(input_data['file_path'])).name}`"
    if name == "Bash" and "command" in input_data:
        snippet = _shorten(str(input_data["command"]), 80)
        return f"⚡ `{snippet}`"
    if name == "Grep" and "pattern" in input_data:
        return f"🔍 **Grep** `{input_data['pattern']}`"
    if name == "Glob" and "pattern" in input_data:
        return f"📂 **Glob** `{input_data['pattern']}`"
    if name == "WebFetch" and "url" in input_data:
        return f"🌐 **WebFetch** {input_data['url']}"
    if name == "WebSearch" and "query" in input_data:
        return f"🔎 **WebSearch** `{input_data['query']}`"
    if name == "Task":
        desc = input_data.get("description", "")
        return f"🤖 **Task** {_shorten(str(desc), 60)}" if desc else "🤖 **Task**"
    return f"🔧 **{name}**"


# -------------------------------------------------------------------- helpers


def _render_body(snapshot: TurnSnapshot) -> str:
    sections: list[str] = []

    if snapshot.state == "failed" and snapshot.error:
        sections.append(f"**❌ 失败**\n\n```\n{snapshot.error}\n```")
    elif snapshot.assistant_text:
        sections.append(snapshot.assistant_text)

    if snapshot.tool_calls:
        bullets = "\n".join(f"- {t}" for t in snapshot.tool_calls)
        sections.append(f"**🔧 工具调用** ({len(snapshot.tool_calls)})\n{bullets}")

    if not sections:
        sections.append("_（运行中…）_")

    body = "\n\n".join(sections)
    if len(body) > _BODY_MAX_CHARS:
        # Keep the tail — the user wants the most recent output, not the start.
        body = "…(已截断早期内容)…\n\n" + body[-(_BODY_MAX_CHARS - 30) :]
    return body


def _render_detail(snapshot: TurnSnapshot) -> ExpandableSection | None:
    if not snapshot.thinking:
        return None
    thinking = snapshot.thinking
    if len(thinking) > _THINKING_MAX_CHARS:
        thinking = thinking[-_THINKING_MAX_CHARS:] + "\n\n…(截断)…"
    return ExpandableSection(label="💭 思考链", content=thinking)


def _shorten(s: str, limit: int) -> str:
    s = " ".join(s.split())
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"
