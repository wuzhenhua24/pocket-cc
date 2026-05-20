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

Per-binding turn state lives behind one :class:`TurnController` (see
:meth:`_controller_for`); the poller, pane-watcher, input router and hook
callbacks all drive transitions through it rather than mutating binding
state directly.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from pocket_cc.app.config import events_jsonl_path
from pocket_cc.app.persistence import Registry
from pocket_cc.claude.events import EventsReader
from pocket_cc.claude.hooks import all_installed as hooks_all_installed
from pocket_cc.lark.client import LarkOapiClient
from pocket_cc.lark.event_loop import LarkEventLoop
from pocket_cc.relay.events_router import EventsPoller, HookEventsDispatcher
from pocket_cc.relay.input import InputRouter
from pocket_cc.relay.output import TranscriptPoller
from pocket_cc.relay.pane_watcher import PaneWatcher
from pocket_cc.relay.turn_controller import TurnController
from pocket_cc.tmux import TmuxManager

if TYPE_CHECKING:
    from pocket_cc.app.config import Config
    from pocket_cc.app.persistence import ChatBinding
    from pocket_cc.claude.events import HookEvent

logger = logging.getLogger(__name__)


class Pocketcc:
    """The pocket-cc process, hosted in one object for easy lifecycle control."""

    def __init__(self, config: Config) -> None:
        self._config = config

        self._tmux = TmuxManager(config.tmux_session)
        self._lark = LarkOapiClient(config.app_id, config.app_secret, domain=config.lark_domain)
        self._loop = LarkEventLoop(config.app_id, config.app_secret, domain=config.lark_domain)
        self._registry = Registry()
        # One TurnController per binding (keyed by chat_id), created lazily on
        # first use. Bootstrap owns construction so InputRouter / pollers never
        # build one themselves — they only ever reach a controller via the
        # callbacks below. Guarded so concurrent callbacks don't double-create.
        self._controllers: dict[str, TurnController] = {}
        self._controllers_lock = threading.Lock()
        self._router = InputRouter(
            config=config,
            tmux=self._tmux,
            lark=self._lark,
            registry=self._registry,
            controller_for=self._controller_for,
        )
        self._poller = TranscriptPoller(
            registry=self._registry,
            controller_for=self._controller_for,
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
            controller_for=self._controller_for,
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
            if binding.current_turn is None:
                continue
            try:
                # Rotation-aware seal so a long final turn isn't truncated.
                self._controller_for(binding).seal(state="done")
            except Exception:
                logger.warning(
                    "shutdown: closing turn failed",
                    extra={"chat_id": binding.chat_id},
                    exc_info=True,
                )
                binding.current_turn = None

    # -------------------------------------------------------------- internal

    def _controller_for(self, binding: ChatBinding) -> TurnController:
        """Get-or-create the binding's TurnController.

        Bindings are created once per chat_id and reused, so a controller
        keyed by chat_id is stable. We still verify identity and refresh if
        a binding object was somehow replaced, so the controller never holds
        a stale binding reference.
        """
        with self._controllers_lock:
            controller = self._controllers.get(binding.chat_id)
            if controller is None or controller.binding is not binding:
                controller = TurnController(binding=binding, lark=self._lark, config=self._config)
                self._controllers[binding.chat_id] = controller
            return controller

    # ---------------------------------------------------- hook event callbacks

    def _handle_session_start(self, binding: ChatBinding, event: HookEvent) -> None:
        """SessionStart for our Claude — lock the transcript path so the
        transcript poller doesn't need to guess via mtime + snapshot exclude."""
        if not event.transcript_path:
            return
        path = Path(event.transcript_path)
        if self._controller_for(binding).lock_transcript(path):
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
        self._controller_for(binding).seal_on_stop(state="done")

    def _handle_stop_failure(self, binding: ChatBinding, event: HookEvent) -> None:
        """StopFailure hook — seal as ❌ failed with whatever error info Claude gave us."""
        error_msg = str(event.raw.get("error") or event.raw.get("message") or "")
        logger.info(
            "StopFailure hook → sealing turn",
            extra={"chat_id": binding.chat_id, "session_id": event.session_id},
        )
        self._drain_transcript_for_seal(binding)
        self._controller_for(binding).seal_on_stop(state="failed", error=error_msg)

    def _drain_transcript_for_seal(self, binding: ChatBinding) -> None:
        """Pull any pending transcript events into the accumulator before sealing.

        Claude writes its final assistant message to the jsonl moments before
        the Stop hook fires. The transcript poller's 0.5s tick is usually
        too slow to catch it — by the time the poller next runs, we've
        already closed the card stream and the final reply gets discarded.
        Drain here ensures the final snapshot reflects whatever Claude
        actually said. (M2-fix-seal-drain)
        """
        try:
            count = self._controller_for(binding).drain_and_ingest()
        except Exception:
            logger.warning(
                "final transcript drain failed",
                extra={"chat_id": binding.chat_id},
                exc_info=True,
            )
            return
        if count:
            logger.info(
                "drained transcript events on stop",
                extra={"chat_id": binding.chat_id, "count": count},
            )
