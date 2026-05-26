"""Per-binding owner of turn-lifecycle state.

One :class:`TurnController` per :class:`ChatBinding`. It is the **single
owner** of a binding's turn state — ``current_turn``, the accumulator, the
``CardStream``, ``waiting_for``, the permission mode and the transcript
reader. Every mutation goes through a controller method; nothing else in the
codebase writes that state directly.

Threading: the controller is reached from several threads (transcript-poller,
pane-watcher, events-poller, WS handlers, shutdown). A single reentrant lock
(:attr:`_lock`) serializes every transition, so those threads can't interleave
their mutations. Two fencing mechanisms ride on top of the lock:

  - **generation** (``_active_gen``) — each opened turn gets a fresh id;
    deferred/async work re-checks it before acting so it can't touch a turn
    that was superseded or sealed in the meantime.
  - **pending-stop FIFO** (``_pending_stops``) — attributes each Stop hook to
    the turn that actually awaits it, since the hook carries no turn token.

Lock discipline: ``send_card`` and ``CardStream.close`` (rare, bounded) may
run inside the lock; tmux I/O and sleeps never do (they live in InputRouter /
PaneWatcher). The high-frequency ``CardStream.update`` is flushed *outside*
the lock — methods compute the card under the lock, release, then update.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pocket_cc.app.persistence import TurnState
from pocket_cc.claude.transcript import ModeChange, TranscriptReader
from pocket_cc.lark.client import LarkApiError
from pocket_cc.relay.card_renderer import (
    ROTATE_AT_CHARS,
    TurnAccumulator,
    render_card,
    should_rotate,
)
from pocket_cc.relay.card_stream import CardStream

if TYPE_CHECKING:
    from pathlib import Path

    from pocket_cc.app.config import Config
    from pocket_cc.app.persistence import ChatBinding, StateStore
    from pocket_cc.claude.transcript import Event
    from pocket_cc.lark.card import CardState
    from pocket_cc.lark.client import LarkClient
    from pocket_cc.relay.card_renderer import TurnSnapshot
    from pocket_cc.relay.waiting import WaitingFor

logger = logging.getLogger(__name__)

# Retry policy for the terminal card patch (the done/failed/cancelled state).
# Runs *outside* the controller lock, so the backoff sleeps are safe. The
# terminal patch is the only signal the turn ended, so unlike the best-effort
# background tick it gets a few retries before we fall back to a text notice.
_FINAL_CLOSE_RETRIES = 3
_FINAL_CLOSE_BACKOFF_S = 0.5


@dataclass(frozen=True)
class _FinalCloseJob:
    """Work handed from seal's locked section to its unlocked finish.

    The locked section detaches the stream + computes the terminal card; the
    unlocked section flushes it with retries (which must not hold the lock).
    """

    stream: CardStream
    final_card: dict[str, Any]
    chat_id: str


class TurnPhase(enum.Enum):
    """Lifecycle phase of a binding's turn — a read-only projection of the
    controller's existing state (``current_turn`` + ``waiting_for`` +
    ``_pending_stops`` + ``_active_gen``), not a separately-stored field.

    Computed by :meth:`TurnController.phase`. There is no stored phase to keep
    in sync, so WAITING can't drift out of step with ``waiting_for`` and
    RUNNING can't drift from the pending-stop FIFO.

      - ``IDLE``    — no active turn; an incoming message opens a new one.
      - ``OPENED``  — card created and prompt text injected, but the deferred
        Enter hasn't fired yet (the cancellable grace window). No Stop is
        owed for it, so cancel only has to drop the deferred Enter.
      - ``RUNNING`` — the Enter was submitted and the turn awaits a
        Stop/StopFailure (its generation is in ``_pending_stops``). Cancel
        here means interrupting a live Claude task.
      - ``WAITING`` — Claude paused on a prompt (Permission / AskUserQuestion
        / Plan); ``waiting_for`` is set. An incoming message is a *reply*
        (continuation), not a new turn. Takes precedence over RUNNING because
        the prompt is what the user is responding to.
    """

    IDLE = "idle"
    OPENED = "opened"
    RUNNING = "running"
    WAITING = "waiting"


class TurnController:
    """Owns the render/rotate/seal/re-render lifecycle for one binding's turn."""

    def __init__(
        self,
        *,
        binding: ChatBinding,
        lark: LarkClient,
        config: Config,
        state_store: StateStore | None = None,
    ) -> None:
        self._binding = binding
        self._lark = lark
        self._config = config
        # Optional so existing tests that don't care about persistence keep
        # working unchanged. Production wires a real StateStore via bootstrap.
        self._state_store = state_store
        # The single per-binding lock. Every turn-state transition (open / seal
        # / ingest / pane-apply / mode / transcript swap) runs under it, so the
        # transcript-poller, pane-watcher, events-poller, WS and shutdown
        # threads can't interleave their mutations. Reentrant because some
        # methods compose (e.g. seal_on_stop → seal, open_turn → begin_turn).
        #
        # Lock discipline (see DESIGN): allowed inside the lock are state
        # decisions, accumulator ops, `send_card` and `CardStream.close`
        # (rare, bounded). Forbidden inside: tmux I/O and sleeps (those live in
        # InputRouter / PaneWatcher, never here). The high-frequency
        # `CardStream.update` is flushed *outside* the lock — methods compute
        # the card under the lock, release, then update.
        self._lock = threading.RLock()
        # Turn generation — a fencing token. Each opened turn gets a fresh,
        # monotonically increasing id; `_active_gen` is the id of the turn
        # that's current right now (None once it's sealed). Deferred/async work
        # (the deferred Enter; Stop-hook attribution) captures the gen at
        # schedule time and re-checks before acting, so it can't operate on a
        # turn that has since been superseded or sealed.
        self._next_gen = 0
        self._active_gen: int | None = None
        # Generations whose pending submission the user cancelled during the
        # deferred-Enter grace window — the worker checks this (via
        # `should_submit_deferred_enter`) and drops the Enter. Replaces the old
        # per-turn cancel_event.
        self._submit_cancelled: set[int] = set()
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
        with self._lock:
            self._next_gen += 1
            self._active_gen = self._next_gen
            return self._active_gen

    def is_current_gen(self, gen: int) -> bool:
        """True iff ``gen`` is still the active turn (not superseded/sealed)."""
        with self._lock:
            return self._active_gen == gen

    def phase(self) -> TurnPhase:
        """Current lifecycle phase, derived from existing state under the lock.

        See :class:`TurnPhase` for the meaning of each value. Pure projection:
        nothing here mutates state, and the precedence (WAITING before RUNNING)
        is what the routing layer relies on so a waiting turn is answered as a
        continuation rather than treated as a busy one.
        """
        with self._lock:
            turn = self._binding.current_turn
            if turn is None:
                return TurnPhase.IDLE
            if turn.waiting_for is not None:
                return TurnPhase.WAITING
            if self._active_gen in self._pending_stops:
                return TurnPhase.RUNNING
            return TurnPhase.OPENED

    def mark_submit_cancelled(self) -> None:
        """Cancel the active turn's pending submission (⏹ during the grace
        window). The deferred-Enter worker will drop the Enter. No-op if no
        turn is active. Replaces the old per-turn cancel_event."""
        with self._lock:
            if self._active_gen is not None:
                self._submit_cancelled.add(self._active_gen)

    def should_submit_deferred_enter(self, gen: int) -> bool:
        """Whether the deferred-Enter worker for ``gen`` should still submit.

        False if the turn was superseded/sealed (gen no longer active) or its
        submission was cancelled during the grace window.
        """
        with self._lock:
            return self._active_gen == gen and gen not in self._submit_cancelled

    def expect_stop(self, gen: int) -> None:
        """Record that ``gen``'s prompt was submitted and now awaits a Stop.

        Called once the deferred Enter actually reaches Claude (a cancelled /
        never-submitted turn produces no Stop, so it must not enqueue).
        """
        with self._lock:
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
        job: _FinalCloseJob | None = None
        with self._lock:
            if not self._pending_stops:
                logger.info(
                    "stop hook with no pending turn — ignored",
                    extra={"chat_id": self._binding.chat_id},
                )
                return
            expected = self._pending_stops.popleft()
            if expected == self._active_gen:
                job = self._seal_locked(state=state, error=error)
            else:
                logger.info(
                    "stop hook for superseded/sealed turn discarded",
                    extra={
                        "chat_id": self._binding.chat_id,
                        "expected_gen": expected,
                        "active_gen": self._active_gen,
                    },
                )
        if job is not None:
            self._close_final_stream(job)

    def cancel_active_turn(self) -> bool:
        """Cancel + seal the active turn (the ⏹ 中断 button). Returns True if a
        turn was cancelled, False if nothing was active (IDLE).

        Interrupting a running Claude (C-c / Esc) fires **no** Stop hook
        (verified empirically), so we cannot wait for ``seal_on_stop`` to
        archive the turn — it would stay stuck "running" forever, and its
        generation would linger in ``_pending_stops`` where a *future* turn's
        Stop would later mis-pop it (sealing the wrong turn, or no turn). So we
        seal here and drop the FIFO entry. Both phases are covered:

          - OPENED — the deferred Enter hasn't fired; ``mark_submit_cancelled``
            makes the worker drop it (the prompt was never submitted, so no
            Stop is owed and nothing was enqueued).
          - RUNNING — the prompt was submitted and its generation sits in
            ``_pending_stops``; since the interrupt yields no Stop, we remove
            that entry so it can't shift attribution of a later turn's Stop.

        The tmux C-c / Escape side-effects stay in :class:`InputRouter`
        (terminal I/O must not run under the lock).
        """
        job: _FinalCloseJob | None = None
        with self._lock:
            if self._binding.current_turn is None:
                return False
            gen = self._active_gen
            # OPENED: drop the not-yet-sent deferred Enter. Harmless if RUNNING
            # (its Enter already fired; the worker won't re-check).
            self.mark_submit_cancelled()
            if gen is not None:
                # RUNNING: the submitted gen is awaiting a Stop that will never
                # come. Remove it so the FIFO stays aligned with reality.
                # OPENED phase was never enqueued → remove() is a no-op there.
                with contextlib.suppress(ValueError):
                    self._pending_stops.remove(gen)
            job = self._seal_locked(state="cancelled", error="")
        if job is not None:
            self._close_final_stream(job)
        return True

    # --------------------------------------------------------------- public API

    def open_turn(self, user_text: str) -> int | None:
        """Open a fresh turn: send its initial card, install it as current,
        and return the new generation. Returns None if the initial card send
        fails (no turn is installed in that case).

        The caller (InputRouter) still owns the tmux side-effects — injecting
        the prompt text and the deferred Enter — because those are terminal
        I/O that must stay out of the controller's critical section.
        """
        with self._lock:
            accumulator = TurnAccumulator()
            accumulator.user_prompt = user_text  # show immediately; transcript confirms
            # Carry over the session-level permission mode so the Mode button on
            # the very first card render doesn't flash "默认" before the next
            # transcript `permission-mode` record lands.
            accumulator.current_mode = self._binding.current_mode

            try:
                message_id = self._lark.send_card(
                    self._binding.chat_id,
                    render_card(
                        accumulator.snapshot(state="running"),
                        show_thinking=self._config.show_thinking,
                    ),
                )
            except LarkApiError:
                logger.exception("send initial card failed")
                return None

            stream = CardStream(self._lark, message_id, interval_s=self._config.patch_interval_s)
            stream.start()
            turn = TurnState(
                card_message_id=message_id,
                card_stream=stream,
                accumulator=accumulator,
            )
            self._binding.current_turn = turn
            gen = self.begin_turn()
            # Persist L2 (active_card pointer) so a restart can patch the
            # orphan card instead of leaving it stuck "运行中".
            self._persist_state()
            return gen

    def poll_transcript(self, found: Path | None) -> None:
        """Adopt/swap the active transcript and ingest any new events.

        ``found`` is the result of the transcript poller's filesystem scan
        (None if no active transcript yet). The scan stays in the poller (slow
        I/O); the swap + read + ingest happen here so that *all* transcript
        reader access — this poll and the Stop-hook drain — is serialized by a
        single owner instead of racing on the reader's byte offset.
        """
        pending = None
        with self._lock:
            if found is not None and self._binding.transcript_path != found:
                self._binding.transcript_path = found
                self._binding.transcript_reader = TranscriptReader(path=found)
                logger.info(
                    "transcript switched",
                    extra={"chat_id": self._binding.chat_id, "path": str(found)},
                )
            reader = self._binding.transcript_reader
            if reader is not None:
                pending = self._ingest_locked(reader.read_new())
        if pending is not None:
            pending[0].update(pending[1])

    def lock_transcript(self, path: Path) -> bool:
        """Lock the binding's transcript path (idempotent). Returns True if it
        was just locked (so the caller can log), False if already set.

        Called from the SessionStart hook to pin the transcript early, before
        the poller's mtime heuristic would otherwise pick it up.
        """
        with self._lock:
            if self._binding.transcript_path is not None:
                return False
            self._binding.transcript_path = path
            self._binding.transcript_reader = TranscriptReader(path=path)
            return True

    def drain_and_ingest(self) -> int:
        """Pull any pending transcript events into the accumulator (no render).

        Used right before a Stop/StopFailure seal: Claude writes its final
        assistant message moments before the hook fires, too fast for the
        0.5s poll tick, so we drain synchronously so the final snapshot
        reflects it. Returns the number of events ingested.
        """
        with self._lock:
            reader = self._binding.transcript_reader
            turn = self._binding.current_turn
            if reader is None or turn is None:
                return 0
            events = reader.read_new()
            for ev in events:
                turn.accumulator.ingest(ev)
            return len(events)

    def apply_pane_state(self, new_waiting: WaitingFor | None, detected_mode: str | None) -> None:
        """Apply a pane-watcher observation to the active turn.

        ``new_waiting`` is the prompt parsed from the pane (None = no prompt
        visible). ``detected_mode`` is the permission mode read off the same
        capture (None = no banner, which must NOT force "default"). Re-renders
        only when something actually changed.
        """
        pending = None
        with self._lock:
            turn = self._binding.current_turn
            if turn is not None:
                changed = False
                current = turn.waiting_for
                if new_waiting is None:
                    if current is not None:
                        turn.waiting_for = None
                        changed = True
                elif current is None or current.fingerprint != new_waiting.fingerprint:
                    turn.waiting_for = new_waiting
                    changed = True
                mode_changed = False
                if detected_mode is not None and detected_mode != self._binding.current_mode:
                    self._binding.current_mode = detected_mode
                    turn.accumulator.current_mode = detected_mode
                    changed = True
                    mode_changed = True
                if changed:
                    pending = self._render_locked(turn)
                if mode_changed:
                    self._persist_state()
        if pending is not None:
            pending[0].update(pending[1])

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
        with self._lock:
            pending = self._ingest_locked(events)
        if pending is not None:
            pending[0].update(pending[1])

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

        Two-phase so the terminal patch's retries don't run under the lock:
        the locked :meth:`_seal_locked` rotates + computes the final card +
        detaches the stream + clears active state and returns a close job; the
        unlocked :meth:`_close_final_stream` flushes it (with retries).
        """
        with self._lock:
            job = self._seal_locked(state=state, error=error)
        if job is not None:
            self._close_final_stream(job)

    def _seal_locked(self, *, state: CardState, error: str) -> _FinalCloseJob | None:
        """Locked half of :meth:`seal`. **Lock must be held.**

        Rolls the oversized uncommitted tail across continuation cards, renders
        the terminal card, then detaches the stream and clears the active turn
        + generation so no later work touches it. Returns the close job to run
        outside the lock, or None when there is no active turn.
        """
        binding = self._binding
        turn = binding.current_turn
        if turn is None:
            return None
        # Roll the oversized uncommitted tail across continuation cards. We
        # check the *running* snapshot (content only) so the loop stops once the
        # remaining tail fits — that tail is closed by the caller with the
        # terminal state. Same 32-iteration safety cap as _render_locked.
        for _ in range(32):
            if not should_rotate(turn.accumulator.snapshot(state="running", from_committed=True)):
                break
            self._rotate_card(turn)
        final_card = render_card(
            turn.accumulator.snapshot(state=state, error=error, from_committed=True),
            is_continuation=turn.is_continuation,
            show_thinking=self._config.show_thinking,
        )
        stream = turn.card_stream
        binding.current_turn = None
        # Retire the generation so any in-flight deferred work (e.g. a deferred
        # Enter scheduled for this turn) sees a stale gen and aborts instead of
        # acting on a closed/next turn.
        self._active_gen = None
        # Clear the persisted active-card pointer — the card is being closed,
        # so on a future restart there's no orphan to recover.
        self._persist_state()
        return _FinalCloseJob(stream=stream, final_card=final_card, chat_id=binding.chat_id)

    def _close_final_stream(self, job: _FinalCloseJob) -> None:
        """Unlocked half of :meth:`seal`: flush the terminal card with retries.

        Unlike rotation's best-effort intermediate closes, the terminal patch
        is the only signal the turn ended — so if every retry fails we surface
        a fallback text notice rather than silently leaving the user staring at
        a "running" card while the turn is internally done.
        """
        try:
            delivered = job.stream.close(
                job.final_card,
                retries=_FINAL_CLOSE_RETRIES,
                backoff_s=_FINAL_CLOSE_BACKOFF_S,
            )
        except Exception:
            logger.warning(
                "seal: closing final card raised",
                extra={"chat_id": job.chat_id},
                exc_info=True,
            )
            delivered = False
        if delivered:
            return
        logger.error(
            "seal: final card patch failed after retries — sending fallback notice",
            extra={"chat_id": job.chat_id},
        )
        try:
            self._lark.send_text(
                job.chat_id,
                "✅ 上一轮已处理完成，但最终卡片更新失败——内容以实际会话为准，可刷新查看。",
            )
        except LarkApiError:
            logger.warning(
                "seal: fallback notice send also failed",
                extra={"chat_id": job.chat_id},
                exc_info=True,
            )

    def clear_waiting_and_rerender(self) -> None:
        """Clear the active turn's waiting state and re-render it as running.

        Called when the user replies to a waiting prompt (button or free-form
        text). Clears ``waiting_for`` optimistically — the pane-watcher
        re-sets it if Claude re-prompts. Goes through the rotation-aware
        rerender so a turn that already rotated to a "(续)" card isn't
        re-dumped onto the current card (which would tail-truncate).
        """
        pending = None
        with self._lock:
            turn = self._binding.current_turn
            if turn is not None:
                turn.waiting_for = None
                pending = self._render_locked(turn)
        if pending is not None:
            pending[0].update(pending[1])

    def update_mode(self, mode: str) -> None:
        """Record a permission-mode change and refresh the Mode button.

        Always updates the binding-level (session) mode so the *next* turn
        opens with the right mode even when no turn is active right now. When
        a turn is active, also syncs the mode into its accumulator (drives the
        Mode button label) and re-renders. A no-op mode write still re-renders,
        but CardStream's hash de-dupe drops the redundant patch.
        """
        pending = None
        with self._lock:
            mode_changed = self._binding.current_mode != mode
            self._binding.current_mode = mode
            turn = self._binding.current_turn
            if turn is not None:
                turn.accumulator.current_mode = mode
                pending = self._render_locked(turn)
            if mode_changed:
                self._persist_state()
        if pending is not None:
            pending[0].update(pending[1])

    # ----------------------------------------------------------------- internal

    def _persist_state(self) -> None:
        """Write the registry snapshot to disk. No-op when no store is wired.

        Called from inside the controller lock — keeps the write linearized
        with the mutation that triggered it, so consecutive saves observe
        consistent in-memory state for *this* binding. StateStore has its own
        lock for cross-binding serialization.
        """
        if self._state_store is None:
            return
        self._state_store.save()

    def _ingest_locked(self, events: list[Event]) -> tuple[CardStream, dict[str, Any]] | None:
        """Fold events into the accumulator and render. **Lock must be held.**

        Returns the ``(stream, card)`` to flush (outside the lock), or None
        when there's no active turn. Permission-mode records are applied to the
        binding mode even between turns so the next turn opens with it.
        """
        binding = self._binding
        mode_changed = False
        for ev in events:
            if isinstance(ev, ModeChange) and binding.current_mode != ev.mode:
                binding.current_mode = ev.mode
                mode_changed = True
        if mode_changed:
            self._persist_state()
        turn = binding.current_turn
        if turn is None:
            return None
        for ev in events:
            turn.accumulator.ingest(ev)
        return self._render_locked(turn)

    def _render_locked(self, turn: TurnState) -> tuple[CardStream, dict[str, Any]]:
        """Run the rotation loop and render the current card. **Lock must be
        held.** Returns ``(stream, card)``; the caller flushes via
        ``stream.update(card)`` *after* releasing the lock (the only
        high-frequency op kept off the lock).

        Loops the rotation step: when one transcript batch ingests enough
        content to fill *several* cards (e.g. one long assistant response
        plus a flurry of tool calls), each iteration seals the largest prefix
        that fits without tail-truncation, then re-checks — so a long batch
        becomes N "(续)" cards instead of one truncated card.
        """
        # Safety cap so a logic bug in find_fit_window can't busy-loop forever.
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
        return turn.card_stream, card

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

        # Build both cards up front, *without* mutating any state. The sealing
        # card is parts[committed:end]; the continuation card is parts[end:]
        # (snapshot_from, so we don't need to commit first).
        sealing_card = render_card(
            turn.accumulator.snapshot_window(
                text_end=text_end,
                tool_end=tool_end,
                thinking_end=thinking_end,
                state="running",
            ),
            is_continuation=turn.is_continuation,
            ends_with_continuation_marker=True,
            show_thinking=self._config.show_thinking,
        )
        new_card = render_card(
            turn.accumulator.snapshot_from(
                text_start=text_end,
                tool_start=tool_end,
                thinking_start=thinking_end,
                state="running",
            ),
            is_continuation=True,
            show_thinking=self._config.show_thinking,
        )

        # Send the continuation card FIRST — it's the only fallible external
        # call, and ordering it ahead of the irreversible local mutations
        # (closing the old stream, advancing the commit cursor) means a failed
        # send leaves the turn fully intact: the old stream stays live and the
        # tail stays uncommitted, so the next render just retries the rotation
        # instead of orphaning the card.
        try:
            new_message_id = self._lark.send_card(binding.chat_id, new_card)
        except LarkApiError:
            logger.warning(
                "rotation: failed to send continuation card — keeping current "
                "card live, will retry on next render",
                extra={"chat_id": binding.chat_id},
                exc_info=True,
            )
            return

        # Send succeeded → now commit the irreversible local steps.
        try:
            turn.card_stream.close(sealing_card)
        except Exception:
            logger.warning(
                "rotation: closing sealed card failed",
                extra={"chat_id": binding.chat_id},
                exc_info=True,
            )
        # Commit *only* what we just sealed. Any leftover uncommitted parts show
        # up on the next card (and trigger another rotation iteration if still
        # too big).
        turn.accumulator.commit_to(
            text_end=text_end,
            tool_end=tool_end,
            thinking_end=thinking_end,
        )

        new_stream = CardStream(
            self._lark, new_message_id, interval_s=self._config.patch_interval_s
        )
        new_stream.start()
        turn.card_stream = new_stream
        turn.card_message_id = new_message_id
        turn.is_continuation = True
        # Persist the new active-card pointer — the previous message_id is no
        # longer the orphan target; the continuation card is.
        self._persist_state()
        logger.info(
            "rotated card (continuation)",
            extra={
                "chat_id": binding.chat_id,
                "new_card_id": new_message_id,
            },
        )
