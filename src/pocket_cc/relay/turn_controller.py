"""Per-binding owner of turn-lifecycle state.

One :class:`TurnController` per :class:`ChatBinding`. It owns the render /
rotate / seal / re-render side of a turn — the logic that used to live as
loose methods on :class:`pocket_cc.app.bootstrap.Pocketcc`.

This is **step 1** of the controller extraction (see the migration plan): a
*verbatim* move with no behavioral change. There is no lock and no turn
generation yet — those land in later steps. The accumulator/`current_turn`
mutations still happen exactly where and when they did before; the only
difference is they now live behind a single object instead of being spread
across the bootstrap callbacks.

Threading note (unchanged from before this move): these methods are still
invoked from several threads (transcript-poller, pane-watcher, events-poller,
WS, shutdown) without coordination. Making that safe is the point of the
later steps; for now we preserve the status quo so the diff is reviewable.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING

from pocket_cc.claude.transcript import ModeChange
from pocket_cc.lark.client import LarkApiError
from pocket_cc.relay.card_renderer import ROTATE_AT_CHARS, render_card, should_rotate
from pocket_cc.relay.card_stream import CardStream

if TYPE_CHECKING:
    from pocket_cc.app.config import Config
    from pocket_cc.app.persistence import ChatBinding, TurnState
    from pocket_cc.claude.transcript import Event
    from pocket_cc.lark.card import CardState
    from pocket_cc.lark.client import LarkClient
    from pocket_cc.relay.card_renderer import TurnSnapshot

logger = logging.getLogger(__name__)


class TurnController:
    """Owns the render/rotate/seal/re-render lifecycle for one binding's turn."""

    def __init__(self, *, binding: ChatBinding, lark: LarkClient, config: Config) -> None:
        self._binding = binding
        self._lark = lark
        self._config = config
        # Turn generation — a fencing token. Each opened turn gets a fresh,
        # monotonically increasing id; `_active_gen` is the id of the turn
        # that's current right now (None once it's sealed). Deferred/async
        # work (the deferred Enter today; Stop-hook attribution in a later
        # step) captures the gen at schedule time and re-checks
        # `is_current_gen` before acting, so it can't operate on a turn that
        # has since been superseded or sealed. No lock yet — that lands in a
        # later step alongside the rest of the lifecycle serialization.
        self._next_gen = 0
        self._active_gen: int | None = None
        # FIFO of generations whose prompt was actually submitted to Claude and
        # are therefore awaiting a Stop/StopFailure hook. The hook can't carry a
        # Lark-turn token, and one Claude session serves many turns (same
        # session_id/transcript_path), so we attribute each Stop to the oldest
        # outstanding turn here. See :meth:`seal_on_stop`. Assumes exactly one
        # main Stop (or StopFailure) per submitted turn — subagent stops fire a
        # different hook, so that holds.
        self._pending_stops: deque[int] = deque()

    @property
    def binding(self) -> ChatBinding:
        return self._binding

    # ------------------------------------------------------------- generation

    def begin_turn(self) -> int:
        """Mark a freshly opened turn as current and return its generation."""
        self._next_gen += 1
        self._active_gen = self._next_gen
        return self._active_gen

    def is_current_gen(self, gen: int) -> bool:
        """True iff ``gen`` is still the active turn (not superseded/sealed)."""
        return self._active_gen == gen

    def expect_stop(self, gen: int) -> None:
        """Record that ``gen``'s prompt was submitted and now awaits a Stop.

        Called once the deferred Enter actually reaches Claude (a cancelled /
        never-submitted turn produces no Stop, so it must not enqueue).
        """
        self._pending_stops.append(gen)

    def seal_on_stop(self, state: CardState = "done", error: str = "") -> None:
        """Seal the turn a Stop/StopFailure hook belongs to, via the FIFO.

        Pops the oldest outstanding generation and seals it **only if it's
        still the active turn**. If it was already superseded (the user fired a
        new turn) or sealed by another path, the Stop is discarded — crucially
        without sealing whatever turn happens to be current now, which is the
        "old Stop closes new turn" race this whole mechanism exists to prevent.

        An empty FIFO means no submitted turn is awaiting a Stop (duplicate or
        stray hook) — no-op, never seal a turn that might still be running.
        """
        if not self._pending_stops:
            logger.info(
                "stop hook with no pending turn — ignored",
                extra={"chat_id": self._binding.chat_id},
            )
            return
        expected = self._pending_stops.popleft()
        if expected == self._active_gen:
            self.seal(state=state, error=error)
        else:
            logger.info(
                "stop hook for superseded/sealed turn discarded",
                extra={
                    "chat_id": self._binding.chat_id,
                    "expected_gen": expected,
                    "active_gen": self._active_gen,
                },
            )

    # --------------------------------------------------------------- public API

    def ingest_events(self, events: list[Event]) -> None:
        """Poller → accumulator → maybe rotate → re-render → throttled patch.

        Skips silently when there is no active turn (e.g. Claude emitted
        startup/clear-prompt records while no user message is pending).
        Honors `turn.waiting_for` (set by PaneWatcher) so a transcript tick
        arriving mid-prompt doesn't accidentally flip the card back to
        running.

        Card rotation (M2-F): if the body of the current card grew past
        ``ROTATE_AT_CHARS``, seal it and start a fresh "(续)" card before
        rendering. Rotation is **disabled while waiting** to avoid the
        weird UX of rotating mid-prompt.
        """
        binding = self._binding
        # Permission-mode records can arrive between turns (e.g. user
        # pressed Shift-Tab in the tmux pane while no Lark turn was open)
        # — keep the binding's mode current regardless, so the next turn
        # that opens initializes its accumulator with the right mode.
        for ev in events:
            if isinstance(ev, ModeChange):
                binding.current_mode = ev.mode

        turn = binding.current_turn
        if turn is None:
            logger.debug(
                "ingest_events skipped — no active turn",
                extra={"chat_id": binding.chat_id, "event_count": len(events)},
            )
            return
        logger.debug(
            "ingest_events ingesting",
            extra={
                "chat_id": binding.chat_id,
                "event_count": len(events),
                "kinds": [type(e).__name__ for e in events],
            },
        )
        for ev in events:
            turn.accumulator.ingest(ev)
        self._publish_card(turn)

    def seal(self, state: CardState = "done", error: str = "") -> None:
        """Rotation-aware seal of the binding's active turn.

        The seal counterpart of :meth:`_publish_card`. Naively closing the
        card with a full-history snapshot re-dumps the whole turn onto the
        *current* (possibly already-rotated) card, where it tail-truncates
        ("已截断早期内容") — the bug we keep hitting. Instead we run the same
        rotation loop on the **uncommitted tail**: any content beyond one
        card's worth is rolled across "(续)" continuation cards first, then
        the remaining tail (≤ one card) is closed with the terminal
        done/failed state and the correct ``is_continuation`` flag.

        Reached from the new-message-closes-prior path and error paths (via
        ``InputRouter._close_turn``), from shutdown, and — through
        :meth:`seal_on_stop` — from the Stop / StopFailure hooks.
        """
        binding = self._binding
        turn = binding.current_turn
        if turn is None:
            return
        # Roll the oversized uncommitted tail across continuation cards. We
        # check the *running* snapshot (content only) so the loop stops once
        # the remaining tail fits — that tail is then closed below with the
        # terminal state. Same 32-iteration safety cap as _publish_card.
        for _ in range(32):
            if not should_rotate(
                turn.accumulator.snapshot(state="running", from_committed=True)
            ):
                break
            self._rotate_card(turn)
        final_snapshot = turn.accumulator.snapshot(
            state=state, error=error, from_committed=True
        )
        try:
            turn.card_stream.close(
                render_card(
                    final_snapshot,
                    is_continuation=turn.is_continuation,
                    show_thinking=self._config.show_thinking,
                )
            )
        except Exception:
            logger.warning(
                "seal: closing final card failed",
                extra={"chat_id": binding.chat_id},
                exc_info=True,
            )
        binding.current_turn = None
        # Retire the generation so any in-flight deferred work (e.g. a
        # deferred Enter that was scheduled for this turn) sees a stale gen
        # and aborts instead of acting on a closed/next turn.
        self._active_gen = None

    def clear_waiting_and_rerender(self) -> None:
        """Clear the active turn's waiting state and re-render it as running.

        Called when the user replies to a waiting prompt (button or free-form
        text). Clears ``waiting_for`` optimistically — the pane-watcher
        re-sets it if Claude re-prompts. Goes through the rotation-aware
        rerender so a turn that already rotated to a "(续)" card isn't
        re-dumped onto the current card (which would tail-truncate).
        """
        turn = self._binding.current_turn
        if turn is None:
            return
        turn.waiting_for = None
        self._publish_card(turn)

    def update_mode(self, mode: str) -> None:
        """Record a permission-mode change and refresh the Mode button.

        Always updates the binding-level (session) mode so the *next* turn
        opens with the right mode even when no turn is active right now. When
        a turn is active, also syncs the mode into its accumulator (drives the
        Mode button label) and re-renders. A no-op mode write still re-renders,
        but CardStream's hash de-dupe drops the redundant patch.
        """
        self._binding.current_mode = mode
        turn = self._binding.current_turn
        if turn is None:
            return
        turn.accumulator.current_mode = mode
        self._publish_card(turn)

    def on_pane_change(self) -> None:
        """PaneWatcher detected a waiting_for transition (set/changed/cleared).

        Re-render the card to reflect the new state. CardStream's throttle
        + hash dedupe handles the case where the rendered content didn't
        actually change. Goes through the same rotation-aware path as
        transcript updates.
        """
        turn = self._binding.current_turn
        if turn is None:
            return
        self._publish_card(turn)

    # ----------------------------------------------------------------- internal

    def _publish_card(self, turn: TurnState) -> None:
        """Render the current accumulator state to a card and patch it.

        Single re-render path used by both the transcript-poller callback
        and the pane-watcher callback. Owns the rotation decision so both
        paths get the same long-content handling.

        Loops the rotation step: when one transcript batch ingests enough
        content to fill *several* cards (e.g. one long assistant response
        plus a flurry of tool calls), each iteration seals the largest
        prefix that fits without tail-truncation, then re-checks. Without
        the loop, a single oversized batch produces one truncated sealed
        card + one fresh card holding the leftover — losing the early
        content the user expected to see across multiple "(续)" cards.
        """
        # Safety cap so a logic bug in find_fit_window can't busy-loop
        # forever. Number of cards a single batch could *plausibly* need
        # is bounded by (max batch size) / ROTATE_AT_CHARS; 32 is far
        # past any realistic value and below the Lark per-chat send rate.
        for _ in range(32):
            snapshot = self._snapshot_active(turn)
            if not should_rotate(snapshot):
                break
            self._rotate_card(turn)
        card = render_card(
            snapshot,
            is_continuation=turn.is_continuation,
            show_thinking=self._config.show_thinking,
        )
        turn.card_stream.update(card)

    def _snapshot_active(self, turn: TurnState) -> TurnSnapshot:
        """Snapshot helper that respects waiting_for + from_committed."""
        if turn.waiting_for is not None:
            return turn.accumulator.snapshot(
                state="waiting",
                waiting_for=turn.waiting_for,
                from_committed=True,
            )
        return turn.accumulator.snapshot(state="running", from_committed=True)

    def _rotate_card(self, turn: TurnState) -> None:
        """Seal the current card with a "⏬ 续下条" footer and open a new one.

        Chunked rotation: instead of dumping the entire uncommitted slice
        into the sealed card (which then tail-truncates with "…(已截断早
        期内容)…" when the slice is bigger than `_BODY_MAX_CHARS`), we ask
        the accumulator for the largest split point that fits within
        ``ROTATE_AT_CHARS``. Only that prefix is sealed + committed;
        the rest stays uncommitted and gets picked up by `_publish_card`'s
        loop, which calls back into here for another rotation. End result:
        a long batch becomes N "(续)" cards instead of one truncated card.
        """
        binding = self._binding
        # Find the largest commit-onward prefix that renders within the
        # rotation budget. Forward progress is guaranteed by the
        # accumulator (see `find_fit_window` docstring).
        text_end, tool_end, thinking_end = turn.accumulator.find_fit_window(ROTATE_AT_CHARS)

        sealing_snapshot = turn.accumulator.snapshot_window(
            text_end=text_end,
            tool_end=tool_end,
            thinking_end=thinking_end,
            state="running",
        )
        sealing_card = render_card(
            sealing_snapshot,
            is_continuation=turn.is_continuation,
            ends_with_continuation_marker=True,
            show_thinking=self._config.show_thinking,
        )
        try:
            turn.card_stream.close(sealing_card)
        except Exception:
            logger.warning(
                "rotation: closing sealed card failed",
                extra={"chat_id": binding.chat_id},
                exc_info=True,
            )

        # Commit *only* what we just sealed. Any leftover uncommitted
        # parts will show up on the next card (and trigger another
        # rotation iteration in `_publish_card` if they're still too big).
        turn.accumulator.commit_to(
            text_end=text_end,
            tool_end=tool_end,
            thinking_end=thinking_end,
        )

        # Open a fresh continuation card.
        starter_snapshot = turn.accumulator.snapshot(state="running", from_committed=True)
        new_card = render_card(
            starter_snapshot, is_continuation=True, show_thinking=self._config.show_thinking
        )
        try:
            new_message_id = self._lark.send_card(binding.chat_id, new_card)
        except LarkApiError:
            logger.exception(
                "rotation: failed to send continuation card — turn is now orphaned",
                extra={"chat_id": binding.chat_id},
            )
            # Leave the old (closed) card_stream in place. Subsequent
            # updates will be no-ops; that's at least better than crashing.
            return

        new_stream = CardStream(
            self._lark, new_message_id, interval_s=self._config.patch_interval_s
        )
        new_stream.start()
        turn.card_stream = new_stream
        turn.card_message_id = new_message_id
        turn.is_continuation = True
        logger.info(
            "rotated card (continuation)",
            extra={
                "chat_id": binding.chat_id,
                "new_card_id": new_message_id,
            },
        )
