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
from typing import TYPE_CHECKING, Any

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
    CardButton,
    CardState,
    ExpandableSection,
    build_status_card,
)

if TYPE_CHECKING:
    from pocket_cc.relay.waiting import WaitingFor

# Lark cards larger than ~30KB get rejected; the body field is the long pole.
# These two thresholds:
#   _BODY_MAX_CHARS  — soft cap; over this we tail-truncate within a single
#                      card (used when M2-F rotation isn't applicable, e.g.
#                      waiting cards, failed-state error dumps).
#   ROTATE_AT_CHARS — bootstrap watches this; once a card's rendered body
#                      passes it, the card is sealed with a "⏬ 续下条" footer
#                      and a fresh card begins (M2-F). Bootstrap also uses
#                      this as `max_chars` for chunked rotation's fit window.
_BODY_MAX_CHARS = 3000
ROTATE_AT_CHARS = 2500
_THINKING_MAX_CHARS = 2000
_TITLE_MAX_CHARS = 60
# Continuation marker appended to the body of a card we're about to close
# because its content reached the rotation threshold. The next card carries
# `_CONTINUATION_TITLE_PREFIX` so the user can visually thread them together.
_CONTINUATION_FOOTER = "\n\n_⏬ 内容续下条_"
_CONTINUATION_TITLE_PREFIX = "(续) "
# Lark renders action rows OK up to ~6 buttons on mobile; we reserve 2 slots
# for ⏹ 中断 / ⎋ Esc and dedicate up to 4 to waiting-prompt options. Options
# beyond this still appear in the body text (numbered), and the user can
# reply with the number as free-form text.
_WAITING_OPTION_BUTTONS_CAP = 4
_WAITING_BUTTON_LABEL_MAX = 24


@dataclass(frozen=True, slots=True)
class TurnSnapshot:
    """Render-ready summary of a turn. Pure data, no Lark concepts.

    ``waiting_for`` is set when Claude is blocked on a user response
    (Permission / AskUserQuestion / Plan). When present, ``state`` should
    be "waiting" and :func:`render_card` produces an options card instead
    of the normal running/done/failed layout.
    """

    user_prompt: str
    assistant_text: str
    tool_calls: list[str]
    thinking: str
    state: CardState
    error: str = ""
    waiting_for: WaitingFor | None = None


@dataclass
class TurnAccumulator:
    """Stateful aggregator — fed Events, emits TurnSnapshot.

    Supports **card rotation** (M2-F): when a single Lark card's body grows
    past the rotation threshold, the bootstrap layer seals it and starts a
    fresh card. Call :meth:`commit` after sealing — subsequent
    ``snapshot(from_committed=True)`` calls yield only the new content that
    hasn't been shown yet.

    The accumulator itself keeps the **full** history so the final summary
    (or a "show everything" feature later) can still see the whole turn.
    """

    user_prompt: str = ""
    _assistant_text_parts: list[str] = field(default_factory=list)
    _tool_calls: list[str] = field(default_factory=list)
    _thinking_parts: list[str] = field(default_factory=list)
    _seen_uuids: set[str] = field(default_factory=set)
    # M2-F rotation cursors — number of parts/calls already shown in
    # previous cards (closed). snapshot(from_committed=True) returns only
    # items past these indices.
    _committed_text_parts: int = 0
    _committed_tool_calls: int = 0
    _committed_thinking_parts: int = 0

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

    def commit(self) -> None:
        """Mark everything currently in the accumulator as "already shown".

        Called by the bootstrap when it seals an oversized card and opens
        a new one. The next ``snapshot(from_committed=True)`` will only
        contain text/tool_calls/thinking that arrived **after** this commit.
        """
        self._committed_text_parts = len(self._assistant_text_parts)
        self._committed_tool_calls = len(self._tool_calls)
        self._committed_thinking_parts = len(self._thinking_parts)

    def commit_to(self, *, text_end: int, tool_end: int, thinking_end: int) -> None:
        """Commit up to specific indices instead of "everything seen so far".

        Used by chunked rotation: when the uncommitted slice is too big to
        fit in one Lark card, we seal only parts[committed:end] into the
        current card and leave parts[end:] uncommitted for the next card.
        Indices are clamped to the valid range so callers can pass the
        output of :meth:`find_fit_window` without worrying about new parts
        that may have been ingested in the meantime.
        """
        self._committed_text_parts = max(
            self._committed_text_parts,
            min(text_end, len(self._assistant_text_parts)),
        )
        self._committed_tool_calls = max(
            self._committed_tool_calls,
            min(tool_end, len(self._tool_calls)),
        )
        self._committed_thinking_parts = max(
            self._committed_thinking_parts,
            min(thinking_end, len(self._thinking_parts)),
        )

    def snapshot(
        self,
        state: CardState = "running",
        error: str = "",
        waiting_for: WaitingFor | None = None,
        *,
        from_committed: bool = False,
    ) -> TurnSnapshot:
        """Render a TurnSnapshot of the current state.

        Args:
            from_committed: When True, returns only content that arrived
                **after** the last :meth:`commit`. Used for the active
                (in-progress) card during M2-F rotation. False (default)
                yields the full turn — used for final / shutdown snapshots.
        """
        text_parts = self._assistant_text_parts
        tool_calls = self._tool_calls
        thinking_parts = self._thinking_parts
        if from_committed:
            text_parts = text_parts[self._committed_text_parts :]
            tool_calls = tool_calls[self._committed_tool_calls :]
            thinking_parts = thinking_parts[self._committed_thinking_parts :]
        return TurnSnapshot(
            user_prompt=self.user_prompt,
            assistant_text="\n\n".join(p for p in text_parts if p),
            tool_calls=list(tool_calls),
            thinking="\n\n".join(p for p in thinking_parts if p),
            state=state,
            error=error,
            waiting_for=waiting_for,
        )

    def snapshot_window(
        self,
        *,
        text_end: int,
        tool_end: int,
        thinking_end: int,
        state: CardState = "running",
        error: str = "",
        waiting_for: WaitingFor | None = None,
    ) -> TurnSnapshot:
        """Render a snapshot of parts[committed:end] for each list.

        Used by chunked rotation to render the *exact* slice that's about
        to be sealed into the current card (which may be a strict prefix
        of the uncommitted content when the full slice is too big to fit).
        """
        text_parts = self._assistant_text_parts[self._committed_text_parts : text_end]
        tool_calls = self._tool_calls[self._committed_tool_calls : tool_end]
        thinking_parts = self._thinking_parts[self._committed_thinking_parts : thinking_end]
        return TurnSnapshot(
            user_prompt=self.user_prompt,
            assistant_text="\n\n".join(p for p in text_parts if p),
            tool_calls=list(tool_calls),
            thinking="\n\n".join(p for p in thinking_parts if p),
            state=state,
            error=error,
            waiting_for=waiting_for,
        )

    def find_fit_window(self, max_chars: int) -> tuple[int, int, int]:
        """Find the largest split such that snapshot_window renders ≤ max_chars.

        Returns ``(text_end, tool_end, thinking_end)`` indices into the
        respective full part lists. The window is parts[committed:end].

        Shrinks from the *end* of the rendered body — tool_calls first
        (since they render at the bottom), then assistant_text — so the
        sealed card holds the *earliest* uncommitted content and the
        leftover goes onto the next card. Thinking is only trimmed when
        the body alone is fine but the detail (thinking) puts us over.

        Forward-progress guarantee: when uncommitted content exists, this
        never returns the empty (zero-progress) window. If even a single
        leading part exceeds ``max_chars`` on its own, that part is still
        included — `_render_body`'s tail-truncation handles the oversized
        single-part case, so rotation can't get stuck.
        """
        text_end = len(self._assistant_text_parts)
        tool_end = len(self._tool_calls)
        thinking_end = len(self._thinking_parts)
        text_lo = self._committed_text_parts
        tool_lo = self._committed_tool_calls
        thinking_lo = self._committed_thinking_parts

        while True:
            snap = self.snapshot_window(
                text_end=text_end, tool_end=tool_end, thinking_end=thinking_end
            )
            if len(_render_body(snap)) <= max_chars:
                # Empty window only ever wins when nothing is uncommitted.
                # When there *is* uncommitted content but it doesn't fit,
                # force one leading part in so rotation makes progress.
                if (
                    text_end == text_lo
                    and tool_end == tool_lo
                    and thinking_end == thinking_lo
                ):
                    if text_lo < len(self._assistant_text_parts):
                        return text_lo + 1, tool_lo, thinking_lo
                    if tool_lo < len(self._tool_calls):
                        return text_lo, tool_lo + 1, thinking_lo
                    if thinking_lo < len(self._thinking_parts):
                        return text_lo, tool_lo, thinking_lo + 1
                return text_end, tool_end, thinking_end
            if tool_end > tool_lo:
                tool_end -= 1
            elif text_end > text_lo:
                text_end -= 1
            elif thinking_end > thinking_lo:
                thinking_end -= 1
            else:  # unreachable — empty body always fits, see "Empty window" branch above
                return text_lo, tool_lo, thinking_lo


# --------------------------------------------------------------- render layer


def should_rotate(snapshot: TurnSnapshot) -> bool:
    """Whether this snapshot's body already exceeds the rotation threshold.

    Used by the bootstrap to decide if it's time to seal the current card
    and start a "(续)" continuation card. Centralized here so the threshold
    constant stays private to this module.

    Waiting cards never rotate — the option buttons need to stay on the
    user's active card.
    """
    if snapshot.waiting_for is not None:
        return False
    return len(_render_body(snapshot)) > ROTATE_AT_CHARS


def render_card(
    snapshot: TurnSnapshot,
    *,
    is_continuation: bool = False,
    ends_with_continuation_marker: bool = False,
) -> dict[str, Any]:
    """Render a TurnSnapshot to the Lark card dict.

    State-driven shape:
      - running: blue header, action row visible, "(运行中…)" placeholder if empty
      - done:    green header, no action row (turn is over)
      - failed:  red header, error in body
      - waiting: orange header, prompt + option buttons (M2-0)

    M2-F rotation parameters:
      - ``is_continuation``: render with "(续)" prefix on the title (used
        for the 2nd+ card in a rotated turn).
      - ``ends_with_continuation_marker``: append "⏬ 内容续下条" footer
        (used when sealing a card because the next card is starting).
    """
    if snapshot.waiting_for is not None:
        return _render_waiting_card(snapshot, snapshot.waiting_for)

    title_body = _shorten(snapshot.user_prompt, _TITLE_MAX_CHARS) or "Claude"
    title = f"{_CONTINUATION_TITLE_PREFIX}{title_body}" if is_continuation else title_body
    body = _render_body(snapshot)
    if ends_with_continuation_marker:
        body = body + _CONTINUATION_FOOTER
    detail = _render_detail(snapshot)
    actions = list(DEFAULT_RUNNING_ACTIONS) if snapshot.state == "running" else None

    return build_status_card(
        title=title,
        body=body,
        state=snapshot.state,
        detail=detail,
        actions=actions,
    )


def _render_waiting_card(snapshot: TurnSnapshot, waiting: WaitingFor) -> dict[str, Any]:
    """Render a ❓ waiting card with question + option buttons.

    Options beyond `_WAITING_OPTION_BUTTONS_CAP` are still listed (numbered)
    in the body so the user can reply with that number as free-form text —
    this preserves the "everything passes through to Claude" contract for
    long option lists where buttons would overflow.
    """
    title = _shorten(snapshot.user_prompt, _TITLE_MAX_CHARS) or "Claude"
    body = _render_waiting_body(snapshot, waiting)
    detail = _render_detail(snapshot)
    actions = _render_waiting_actions(waiting)

    return build_status_card(
        title=title,
        body=body,
        state="waiting",
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


def _render_waiting_body(snapshot: TurnSnapshot, waiting: WaitingFor) -> str:
    sections: list[str] = []
    if waiting.question:
        sections.append(f"**{waiting.question}**")
    if waiting.options:
        bullets = []
        for i, opt in enumerate(waiting.options, start=1):
            line = f"**{i}.** {opt.label}"
            if opt.description:
                line += f" — {opt.description}"
            bullets.append(line)
        sections.append("\n".join(bullets))
    # Show recent assistant text if any (gives context for what Claude was
    # doing when the prompt fired).
    if snapshot.assistant_text:
        sections.append(snapshot.assistant_text)
    body = "\n\n".join(sections) or "_（Claude 在等你响应）_"
    if len(body) > _BODY_MAX_CHARS:
        body = "…(已截断早期内容)…\n\n" + body[-(_BODY_MAX_CHARS - 30) :]
    return body


def _render_waiting_actions(waiting: WaitingFor) -> list[CardButton]:
    """Buttons for a waiting card: up to 4 option buttons + ⏹ 中断 + ⎋ Esc."""
    actions: list[CardButton] = []
    for i, opt in enumerate(waiting.options[:_WAITING_OPTION_BUTTONS_CAP]):
        label = f"{i + 1}. {opt.label}"
        if len(label) > _WAITING_BUTTON_LABEL_MAX:
            label = label[: _WAITING_BUTTON_LABEL_MAX - 1] + "…"
        actions.append(
            CardButton(
                text=label,
                value={"action": "waiting_response", "index": i},
                style="primary" if i == 0 else "default",
            )
        )
    actions.append(CardButton(text="⏹ 中断", value={"action": "cancel"}, style="danger"))
    # Double-Escape — matches DEFAULT_RUNNING_ACTIONS; one tap fully clears
    # Claude's prompt state without hitting Lark's "操作太频繁" rate limit.
    actions.append(
        CardButton(
            text="⎋ Esc",
            value={
                "action": "key_sequence",
                "keys": ["Escape", "Escape"],
                "delay_ms": 100,
            },
        )
    )
    return actions


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
