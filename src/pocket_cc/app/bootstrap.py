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
from pocket_cc.lark.client import LarkOapiClient
from pocket_cc.lark.event_loop import LarkEventLoop
from pocket_cc.relay.card_renderer import render_card
from pocket_cc.relay.events_router import EventsPoller, HookEventsDispatcher
from pocket_cc.relay.input import InputRouter
from pocket_cc.relay.output import TranscriptPoller
from pocket_cc.tmux import TmuxManager

if TYPE_CHECKING:
    from pocket_cc.app.config import Config
    from pocket_cc.app.persistence import ChatBinding
    from pocket_cc.claude.events import HookEvent
    from pocket_cc.claude.transcript import Event

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
        """Poller → accumulator → re-render → throttled patch.

        Skips silently when there is no active turn (e.g. Claude emitted
        startup/clear-prompt records while no user message is pending).
        """
        turn = binding.current_turn
        if turn is None:
            return
        for ev in events:
            turn.accumulator.ingest(ev)
        snapshot = turn.accumulator.snapshot(state="running")
        turn.card_stream.update(render_card(snapshot))

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
        self._router.close_active_turn(binding, state="done")

    def _handle_stop_failure(self, binding: ChatBinding, event: HookEvent) -> None:
        """StopFailure hook — seal as ❌ failed with whatever error info Claude gave us."""
        error_msg = str(event.raw.get("error") or event.raw.get("message") or "")
        logger.info(
            "StopFailure hook → sealing turn",
            extra={"chat_id": binding.chat_id, "session_id": event.session_id},
        )
        self._router.close_active_turn(binding, state="failed", error=error_msg)
