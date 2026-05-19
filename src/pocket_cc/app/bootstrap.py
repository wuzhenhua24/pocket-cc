"""Application wiring — connect Lark loop / tmux / Claude poller / relay.

`Pocketcc` is the single object responsible for owning all live threads and
clients. It's constructed once with a :class:`Config`, exposes `start()`
(blocking until the WS loop exits) and `shutdown()` (releases threads).

Lifecycle:
    1. start()
       ├─ mkdir workspace_root if missing
       ├─ tmux.ensure_session
       ├─ TranscriptPoller.start()        ← background thread
       └─ LarkEventLoop.start()            ← blocks here
    2. (WS dies or user Ctrl-C)
    3. shutdown()
       ├─ TranscriptPoller.stop()
       └─ close every live TurnState (one final card PATCH with state=done)

The transcript→card update path goes through :meth:`_handle_events`, which
is registered as the poller's `on_events` callback.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pocket_cc.app.config import events_jsonl_path
from pocket_cc.app.persistence import Registry
from pocket_cc.claude.events import EventsReader
from pocket_cc.claude.hooks import all_installed as hooks_all_installed
from pocket_cc.claude.transcript import TranscriptReader
from pocket_cc.lark.client import LarkApiError, LarkOapiClient
from pocket_cc.lark.event_loop import LarkEventLoop
from pocket_cc.relay.card_renderer import ROTATE_AT_CHARS, render_card, should_rotate
from pocket_cc.relay.card_stream import CardStream
from pocket_cc.relay.events_router import EventsPoller, HookEventsDispatcher
from pocket_cc.relay.input import InputRouter
from pocket_cc.relay.output import TranscriptPoller
from pocket_cc.relay.pane_watcher import PaneWatcher
from pocket_cc.tmux import TmuxManager

if TYPE_CHECKING:
    from pocket_cc.app.config import Config
    from pocket_cc.app.persistence import ChatBinding, TurnState
    from pocket_cc.claude.events import HookEvent
    from pocket_cc.claude.transcript import Event
    from pocket_cc.relay.card_renderer import TurnSnapshot

logger = logging.getLogger(__name__)


class Pocketcc:
    """The pocket-cc process, hosted in one object for easy lifecycle control."""

    def __init__(self, config: Config) -> None:
        self._config = config

        self._tmux = TmuxManager(config.tmux_session)
        self._lark = LarkOapiClient(config.app_id, config.app_secret, domain=config.lark_domain)
        self._loop = LarkEventLoop(config.app_id, config.app_secret, domain=config.lark_domain)
        self._registry = Registry()
        self._router = InputRouter(
            config=config,
            tmux=self._tmux,
            lark=self._lark,
            registry=self._registry,
        )
        self._poller = TranscriptPoller(
            registry=self._registry,
            on_events=self._handle_events,
            projects_dir=config.claude_projects_dir,
            interval_s=config.transcript_poll_s,
        )

        # Hooks pipeline — tails events.jsonl, dispatches Stop/StopFailure
        # to the input router for immediate card sealing.
        self._events_reader = EventsReader(path=events_jsonl_path())
        self._dispatcher = HookEventsDispatcher(
            registry=self._registry,
            on_session_start=self._handle_session_start,
            on_stop=self._handle_stop,
            on_stop_failure=self._handle_stop_failure,
        )
        self._events_poller = EventsPoller(
            reader=self._events_reader,
            dispatcher=self._dispatcher,
            interval_s=config.events_poll_s,
        )

        # M2-C: pane watcher detects Claude TUI permission prompts by reading
        # the tmux pane (since they're not in the transcript). On transition
        # (set / change / clear) it re-renders the card so the user sees a
        # ❓ waiting card with option buttons.
        self._pane_watcher = PaneWatcher(
            registry=self._registry,
            tmux=self._tmux,
            on_change=self._handle_pane_state_change,
            interval_s=config.pane_poll_s,
        )

        self._loop.on_message(self._router.handle_message)
        self._loop.on_card_action(self._router.handle_card_action)

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Boot all subsystems and block on the Lark WS loop."""
        self._config.workspace_root.mkdir(parents=True, exist_ok=True)
        self._tmux.ensure_session()
        if not hooks_all_installed():
            logger.warning(
                "Claude hooks not fully installed — card completion will be "
                "delayed (next-message-triggered) instead of instant. "
                "Run `pocket-cc hook install` to enable instant completion."
            )
        self._poller.start()
        self._events_poller.start()
        self._pane_watcher.start()
        logger.info(
            "pocket-cc starting",
            extra={
                "tmux_session": self._config.tmux_session,
                "workspace": str(self._config.workspace_root),
                "whitelist_open": self._config.is_whitelist_open,
            },
        )
        try:
            self._loop.start()  # blocks until WS closes
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Stop the pollers and seal every still-running turn card."""
        self._pane_watcher.stop()
        self._events_poller.stop()
        self._poller.stop()
        for binding in self._registry.all():
            turn = binding.current_turn
            if turn is None:
                continue
            try:
                snapshot = turn.accumulator.snapshot(state="done")
                turn.card_stream.close(render_card(snapshot))
            except Exception:
                logger.warning(
                    "shutdown: closing turn failed",
                    extra={"chat_id": binding.chat_id},
                    exc_info=True,
                )
            binding.current_turn = None

    # -------------------------------------------------------------- internal

    def _handle_events(self, binding: ChatBinding, events: list[Event]) -> None:
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
        turn = binding.current_turn
        if turn is None:
            logger.debug(
                "_handle_events skipped — no active turn",
                extra={"chat_id": binding.chat_id, "event_count": len(events)},
            )
            return
        logger.debug(
            "_handle_events ingesting",
            extra={
                "chat_id": binding.chat_id,
                "event_count": len(events),
                "kinds": [type(e).__name__ for e in events],
            },
        )
        for ev in events:
            turn.accumulator.ingest(ev)
        self._publish_card(binding, turn)

    def _publish_card(self, binding: ChatBinding, turn: TurnState) -> None:
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
            self._rotate_card(binding, turn)
        card = render_card(snapshot, is_continuation=turn.is_continuation)
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

    def _rotate_card(self, binding: ChatBinding, turn: TurnState) -> None:
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
        starter_snapshot = turn.accumulator.snapshot(
            state="running", from_committed=True
        )
        new_card = render_card(starter_snapshot, is_continuation=True)
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

    # ----------------------------------------------------- pane watcher (M2-C)

    def _handle_pane_state_change(self, binding: ChatBinding) -> None:
        """PaneWatcher detected a waiting_for transition (set/changed/cleared).

        Re-render the card to reflect the new state. CardStream's throttle
        + hash dedupe handles the case where the rendered content didn't
        actually change. Goes through the same rotation-aware path as
        transcript updates.
        """
        turn = binding.current_turn
        if turn is None:
            return
        self._publish_card(binding, turn)

    # ---------------------------------------------------- hook event callbacks

    def _handle_session_start(self, binding: ChatBinding, event: HookEvent) -> None:
        """SessionStart for our Claude — lock the transcript path so the
        transcript poller doesn't need to guess via mtime + snapshot exclude."""
        if not event.transcript_path:
            return
        path = Path(event.transcript_path)
        with binding.lock:
            if binding.transcript_path is None:
                binding.transcript_path = path
                binding.transcript_reader = TranscriptReader(path=path)
                logger.info(
                    "transcript locked via SessionStart hook",
                    extra={"chat_id": binding.chat_id, "path": str(path)},
                )

    def _handle_stop(self, binding: ChatBinding, event: HookEvent) -> None:
        """Stop hook for our Claude — seal the active card as ✅ done."""
        logger.info(
            "Stop hook → sealing turn",
            extra={"chat_id": binding.chat_id, "session_id": event.session_id},
        )
        self._drain_transcript_for_seal(binding)
        self._router.close_active_turn(binding, state="done")

    def _handle_stop_failure(self, binding: ChatBinding, event: HookEvent) -> None:
        """StopFailure hook — seal as ❌ failed with whatever error info Claude gave us."""
        error_msg = str(event.raw.get("error") or event.raw.get("message") or "")
        logger.info(
            "StopFailure hook → sealing turn",
            extra={"chat_id": binding.chat_id, "session_id": event.session_id},
        )
        self._drain_transcript_for_seal(binding)
        self._router.close_active_turn(binding, state="failed", error=error_msg)

    def _drain_transcript_for_seal(self, binding: ChatBinding) -> None:
        """Pull any pending transcript events into the accumulator before sealing.

        Claude writes its final assistant message to the jsonl moments before
        the Stop hook fires. The transcript poller's 0.5s tick is usually
        too slow to catch it — by the time the poller next runs, we've
        already closed the card stream and the final reply gets discarded.
        Drain here ensures the final snapshot reflects whatever Claude
        actually said. (M2-fix-seal-drain)
        """
        reader = binding.transcript_reader
        turn = binding.current_turn
        if reader is None or turn is None:
            return
        try:
            events = reader.read_new()
        except Exception:
            logger.warning(
                "final transcript drain failed",
                extra={"chat_id": binding.chat_id},
                exc_info=True,
            )
            return
        if not events:
            return
        logger.info(
            "drained transcript events on stop",
            extra={"chat_id": binding.chat_id, "count": len(events)},
        )
        for ev in events:
            turn.accumulator.ingest(ev)
