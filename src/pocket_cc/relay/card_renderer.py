"""Render Claude transcript events as a Lark card.

Two layers:
  - :class:`TurnAccumulator` ingests an Event stream (from
    `claude.transcript.TranscriptReader`) and keeps a running snapshot of
    "what should the card look like right now".
  - :func:`render_card` turns that snapshot into a Schema 2.0 (cardkit)
    card dict via :func:`lark.card.build_status_card_v2`.

A "turn" = one user prompt + everything Claude does until it stops. The
accumulator is reset per turn so cards stay short. Long turns get **body
truncation** (last 3000 chars wins) so we don't hit Lark's per-card limit.

Phase 4-C of the cardkit migration: body markdown goes through
:func:`markdown_body_to_v2_elements`, which splits GFM pipe-tables out
into v2 native ``{tag: "table"}`` elements and applies heading conversion
to the surrounding markdown chunks. The renderer used to feed
``build_status_card_v2`` a pre-normalized string; now it feeds it a list
of pre-built body elements. Detail (thinking) content still goes through
:func:`normalize_markdown_for_lark` because it stays inside a single
markdown element (no table extraction).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePath
from typing import TYPE_CHECKING, Any

from pocket_cc.claude.transcript import (
    AssistantText,
    AssistantThinking,
    Event,
    ModeChange,
    ToolResult,
    ToolUse,
    UserText,
)
from pocket_cc.lark.card import (
    CardButton,
    CardState,
    ExpandableSection,
    build_running_actions,
    build_status_card_v2,
    markdown_body_to_v2_elements,
    normalize_markdown_for_lark,
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
# Max size of a single stored assistant_text part. A long assistant block
# is split into chunks no bigger than this *at ingest* so that chunked
# rotation (which splits at part boundaries) can always fit at least one
# whole part in a card — otherwise a single oversized block would force
# itself into one card and tail-truncate. Kept comfortably under
# ROTATE_AT_CHARS to leave room for tool-call bullets + headers in the body.
_TEXT_PART_MAX_CHARS = 2000
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

# Default Claude permission mode at session start. Used when no
# `permission-mode` record has been seen yet (e.g. transcript drained
# mid-session, or initial card before first poll tick).
DEFAULT_PERMISSION_MODE = "default"

# permissionMode key → user-facing Chinese label for the Mode button.
# Unknown modes fall back to the raw mode string (see `mode_label`).
_MODE_LABELS: dict[str, str] = {
    "default": "默认",
    "acceptEdits": "自动接受",
    "plan": "计划",
    "bypassPermissions": "跳过权限",
}


def mode_label(mode: str) -> str:
    """Friendly label for a permission mode; falls back to raw if unknown.

    Public so the card layer can format the Mode button consistently. We
    accept unknown values verbatim rather than masking schema drift —
    surfacing the raw key tells us a new Claude mode shipped.
    """
    return _MODE_LABELS.get(mode, mode)


@dataclass(frozen=True, slots=True)
class AskUserOption:
    """One option in an AskUserQuestion question — label + free-form description.

    Sourced from the transcript's ``AskUserQuestion`` tool_use input (much
    cleaner than scraping the pane's `[ ]` checkbox rendering)."""

    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class AskUserQuestion:
    """One question in an AskUserQuestion call. A single tool_use may carry
    multiple questions — we render Q1 with buttons and surface "还有 N 题"
    in the body so the user knows the rest must be answered in the terminal."""

    question: str
    header: str
    options: tuple[AskUserOption, ...]
    multi_select: bool = False


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
    # Latest permission mode known for this turn — set by ingesting
    # transcript `permission-mode` records (see :class:`ModeChange`). The
    # renderer mirrors this onto the Mode button label so users in Lark
    # can see at a glance which mode Claude is in.
    current_mode: str = DEFAULT_PERMISSION_MODE
    # Most recent plan markdown from an ExitPlanMode tool_use, or "". The
    # waiting card uses this to show the actual plan body above the option
    # buttons (the pane-side detector intentionally doesn't extract it, since
    # long plans overflow the captured pane viewport).
    latest_plan: str = ""
    # Most recent AskUserQuestion tool_use payload (one tool_use may contain
    # several questions — the whole list is retained so the waiting card can
    # show "还有 N 题待答" when N > 1).
    latest_ask_user_questions: tuple[AskUserQuestion, ...] = ()


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
    # Latest permission mode seen on this turn's transcript. The bootstrap
    # initializes this from the binding's carry-over so a new turn opening
    # while Claude is in (say) acceptEdits doesn't briefly flash "default"
    # on the button before the next permission-mode record lands.
    current_mode: str = DEFAULT_PERMISSION_MODE
    # Latest plan markdown from an ExitPlanMode tool_use — kept turn-scoped
    # so the waiting card can render the proposal inline above the option
    # buttons. Overwritten on each ExitPlanMode (Claude may refine and re-emit).
    _latest_plan: str = ""
    # Latest AskUserQuestion payload parsed from a tool_use. Tuple keeps
    # multiple questions for the body to display; pane_watcher pulls Q1's
    # options for buttons. Overwritten on each AskUserQuestion call.
    _latest_ask_user_questions: tuple[AskUserQuestion, ...] = ()

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
            # Split oversized blocks so each stored part fits one card —
            # see `_split_text_part`. A single huge assistant message would
            # otherwise be force-included whole by `find_fit_window` and then
            # tail-truncated (the "已截断早期内容" bug).
            self._assistant_text_parts.extend(_split_text_part(event.text))
            return
        if isinstance(event, ToolUse):
            self._tool_calls.append(format_tool_call(event.tool_name, event.tool_input))
            # ExitPlanMode carries the user-visible plan in its `plan` input.
            # Stash it separately so the waiting card can render the full
            # markdown body above the option buttons; the one-line entry above
            # still appears in the tool-call list for accounting.
            if event.tool_name == "ExitPlanMode":
                plan_text = event.tool_input.get("plan")
                if isinstance(plan_text, str):
                    self._latest_plan = plan_text
            elif event.tool_name == "AskUserQuestion":
                self._latest_ask_user_questions = _parse_ask_user_questions(event.tool_input)
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
        if isinstance(event, ModeChange):
            # Track the latest mode — the renderer reads `current_mode`
            # on every snapshot and labels the Mode button accordingly.
            # We don't add a UI marker for the *change* itself (would be
            # noisy across a session); just keep the value current.
            self.current_mode = event.mode
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
            current_mode=self.current_mode,
            latest_plan=self._latest_plan,
            latest_ask_user_questions=self._latest_ask_user_questions,
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
            current_mode=self.current_mode,
            latest_plan=self._latest_plan,
            latest_ask_user_questions=self._latest_ask_user_questions,
        )

    def snapshot_from(
        self,
        *,
        text_start: int,
        tool_start: int,
        thinking_start: int,
        state: CardState = "running",
        error: str = "",
        waiting_for: WaitingFor | None = None,
    ) -> TurnSnapshot:
        """Render a snapshot of parts[start:] for each list — the complement of
        :meth:`snapshot_window`'s parts[committed:end].

        Used by chunked rotation to compute the *continuation* card's content
        (everything after the sealed window) **before** committing, so the new
        card can be sent first and the commit deferred until that send succeeds.
        After a matching ``commit_to(end)`` this equals
        ``snapshot(from_committed=True)``.
        """
        text_parts = self._assistant_text_parts[text_start:]
        tool_calls = self._tool_calls[tool_start:]
        thinking_parts = self._thinking_parts[thinking_start:]
        return TurnSnapshot(
            user_prompt=self.user_prompt,
            assistant_text="\n\n".join(p for p in text_parts if p),
            tool_calls=list(tool_calls),
            thinking="\n\n".join(p for p in thinking_parts if p),
            state=state,
            error=error,
            waiting_for=waiting_for,
            current_mode=self.current_mode,
            latest_plan=self._latest_plan,
            latest_ask_user_questions=self._latest_ask_user_questions,
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
                if text_end == text_lo and tool_end == tool_lo and thinking_end == thinking_lo:
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
    show_thinking: bool = False,
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
        return _render_waiting_card(snapshot, snapshot.waiting_for, show_thinking=show_thinking)

    title_body = _shorten(snapshot.user_prompt, _TITLE_MAX_CHARS) or "Claude"
    title = f"{_CONTINUATION_TITLE_PREFIX}{title_body}" if is_continuation else title_body
    body = _render_body(snapshot)
    if ends_with_continuation_marker:
        body = body + _CONTINUATION_FOOTER
    detail = _render_detail(snapshot) if show_thinking else None
    actions: list[CardButton] | None = None
    # Sealed continuation cards (ends_with_continuation_marker) are historical —
    # the action row belongs only on the live (last) card, so a turn that spans
    # N cards doesn't litter every sealed card with 中断/Esc/Mode/内容 buttons.
    if snapshot.state == "running" and not ends_with_continuation_marker:
        # Build a per-card action row so the Mode button can carry the
        # current permission mode as a suffix. The body of the action row
        # is otherwise static — only the Mode button label is dynamic.
        actions = list(build_running_actions(mode_label(snapshot.current_mode)))

    return build_status_card_v2(
        title=title,
        body_elements=markdown_body_to_v2_elements(body),
        state=snapshot.state,
        detail=detail,
        actions=actions,
    )


def _render_waiting_card(
    snapshot: TurnSnapshot, waiting: WaitingFor, *, show_thinking: bool = False
) -> dict[str, Any]:
    """Render a ❓ waiting card with question + option buttons.

    Options beyond `_WAITING_OPTION_BUTTONS_CAP` are still listed (numbered)
    in the body so the user can reply with that number as free-form text —
    this preserves the "everything passes through to Claude" contract for
    long option lists where buttons would overflow.
    """
    title = _shorten(snapshot.user_prompt, _TITLE_MAX_CHARS) or "Claude"
    body = _render_waiting_body(snapshot, waiting)
    detail = _render_detail(snapshot) if show_thinking else None
    actions = _render_waiting_actions(waiting)

    return build_status_card_v2(
        title=title,
        body_elements=markdown_body_to_v2_elements(body),
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
    if name == "ExitPlanMode":
        # The plan body is rendered separately in the waiting card (see
        # `_render_waiting_body`); this one-liner just marks that the call
        # happened so the tool-call counter stays consistent.
        return "📋 **ExitPlanMode** 等待用户确认方案"
    if name == "AskUserQuestion":
        # Structured questions are rendered separately in the waiting card
        # (see `_render_waiting_body`); one-liner keeps the counter honest.
        return "❓ **AskUserQuestion** 等待用户回答"
    return f"🔧 **{name}**"


def _parse_ask_user_questions(
    tool_input: dict[str, Any],
) -> tuple[AskUserQuestion, ...]:
    """Convert an ``AskUserQuestion`` tool_use input into structured questions.

    Defensive throughout: malformed transcripts (missing keys, wrong types)
    yield empty tuples or empty options rather than raising — the waiting
    card just degrades to "Claude is asking you something" instead of
    breaking the whole turn render.
    """
    raw_questions = tool_input.get("questions")
    if not isinstance(raw_questions, list):
        return ()
    parsed: list[AskUserQuestion] = []
    for raw in raw_questions:
        if not isinstance(raw, dict):
            continue
        raw_options = raw.get("options")
        options: list[AskUserOption] = []
        if isinstance(raw_options, list):
            for opt in raw_options:
                if not isinstance(opt, dict):
                    continue
                label = opt.get("label")
                if not isinstance(label, str):
                    continue
                description = opt.get("description", "")
                if not isinstance(description, str):
                    description = ""
                options.append(AskUserOption(label=label, description=description))
        parsed.append(
            AskUserQuestion(
                question=str(raw.get("question", "")),
                header=str(raw.get("header", "")),
                options=tuple(options),
                multi_select=bool(raw.get("multiSelect", False)),
            )
        )
    return tuple(parsed)


# -------------------------------------------------------------------- helpers


def _render_waiting_body(snapshot: TurnSnapshot, waiting: WaitingFor) -> str:
    sections: list[str] = []
    # AskUserQuestion gets a richer section header that includes the question
    # number / header and a multi-select hint (the buttons can only pick one
    # option per tap, so multi-select questions need a terminal fallback).
    if waiting.source == "ask_user_question":
        sections.append(_render_ask_user_question_header(snapshot, waiting))
    elif waiting.question:
        sections.append(f"**{waiting.question}**")
    # Plan-source waiting: surface the proposed plan inline so the user can
    # actually read what they're approving. Sits above the option list since
    # the options refer to the plan ("Yes, auto-accept edits" only makes
    # sense if you can see the edits being proposed). Sourced from the
    # ExitPlanMode tool_use payload, not the pane (which can't fit it).
    if waiting.source == "plan" and snapshot.latest_plan:
        sections.append(f"**📋 方案**\n\n{snapshot.latest_plan}")
    if waiting.options:
        bullets = []
        for i, opt in enumerate(waiting.options, start=1):
            line = f"**{i}.** {opt.label}"
            if opt.description:
                line += f" — {opt.description}"
            bullets.append(line)
        sections.append("\n".join(bullets))
    # AskUserQuestion may carry multiple questions per tool_use; we only show
    # buttons for Q1 because the buttons send a single keystroke. Tell the
    # user how many more questions remain so they know to follow up.
    if waiting.source == "ask_user_question":
        remaining = max(0, len(snapshot.latest_ask_user_questions) - 1)
        if remaining > 0:
            other_headers = [
                q.header or f"Q{i + 2}"
                for i, q in enumerate(snapshot.latest_ask_user_questions[1:])
            ]
            sections.append(
                f"_Claude 还问了 {remaining} 个问题（{' / '.join(other_headers)}），"
                f"请在终端继续完成或用文字回复。_"
            )
    # Show recent assistant text if any (gives context for what Claude was
    # doing when the prompt fired).
    if snapshot.assistant_text:
        sections.append(snapshot.assistant_text)
    body = "\n\n".join(sections) or "_（Claude 在等你响应）_"
    if len(body) > _BODY_MAX_CHARS:
        body = "…(已截断早期内容)…\n\n" + body[-(_BODY_MAX_CHARS - 30) :]
    return body


def _render_ask_user_question_header(snapshot: TurnSnapshot, waiting: WaitingFor) -> str:
    """Header line(s) for an AskUserQuestion waiting card.

    Uses the snapshot's structured questions list (richer than waiting.question
    alone) to render: "❓ Q1 [<header>] (多选): <question>". Falls back to
    just the waiting.question text when accumulator data isn't ready.
    """
    if not snapshot.latest_ask_user_questions:
        return f"**❓ {waiting.question}**" if waiting.question else "**❓ Claude 在向你提问**"
    q1 = snapshot.latest_ask_user_questions[0]
    label = q1.header or "Q1"
    multi = "（多选 · 飞书一次选一项，多选请去终端）" if q1.multi_select else ""
    head = f"**❓ {label}**{multi}"
    if q1.question:
        head = f"{head}\n\n{q1.question}"
    return head


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

    # Terminal cards (done/failed) drop the tool-call list — once the answer
    # is in, it's just process noise (the user saw it scroll by while running,
    # and the full trace lives in tmux). Fallback: if there's no other content
    # to show, keep it so the card body isn't just a "(运行中…)" placeholder.
    is_terminal = snapshot.state in ("done", "failed")
    if snapshot.tool_calls and (not is_terminal or not sections):
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
    # Normalize here (not in the v2 builder) so Phase 4 only has to delete
    # one call site to retire `normalize_markdown_for_lark` entirely. See
    # module docstring.
    return ExpandableSection(label="💭 思考链", content=normalize_markdown_for_lark(thinking))


def _shorten(s: str, limit: int) -> str:
    s = " ".join(s.split())
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _split_text_part(text: str, limit: int = _TEXT_PART_MAX_CHARS) -> list[str]:
    """Split an assistant text block into chunks no longer than ``limit``.

    Splits at paragraph (``\\n\\n``) boundaries where possible so the chunks
    re-join seamlessly through the accumulator's ``"\\n\\n".join(...)`` — a
    chunk holds one or more whole paragraphs. A single paragraph longer than
    ``limit`` is hard-split on line/character boundaries (content preserved;
    at most a cosmetic blank line where a hard cut lands). The point is to
    keep every stored part small enough that chunked rotation can always fit
    at least one whole part in a card, so no card ever tail-truncates.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if len(para) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_hard_split(para, limit))
            continue
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) > limit:
            if buf:
                chunks.append(buf)
            buf = para
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks


def _hard_split(s: str, limit: int) -> list[str]:
    """Last-resort split of an over-limit paragraph: prefer line boundaries,
    fall back to a hard character cut for a single over-limit line."""
    out: list[str] = []
    buf = ""
    for line in s.split("\n"):
        if len(line) > limit:
            if buf:
                out.append(buf)
                buf = ""
            for i in range(0, len(line), limit):
                out.append(line[i : i + limit])
            continue
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > limit:
            if buf:
                out.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out
