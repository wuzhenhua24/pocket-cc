"""Application wiring — connect Lark loop / tmux / Claude poller / relay.

`Pocketcc` is the single object responsible for owning all live threads and
clients. It's constructed once with a :class:`Config`, exposes `start()`
(blocking until the WS loop exits) and `shutdown()` (releases threads).

Lifecycle:
    1. start()
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
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pocket_cc.app.config import events_jsonl_path, pocket_cc_dir
from pocket_cc.app.persistence import ChatBinding, Registry, StateStore
from pocket_cc.claude.events import EventsReader
from pocket_cc.claude.hooks import all_installed as hooks_all_installed
from pocket_cc.lark.card import build_restart_notice_card_v2
from pocket_cc.lark.client import LarkApiError, LarkClient, LarkOapiClient
from pocket_cc.lark.event_loop import LarkEventLoop
from pocket_cc.relay.events_router import EventsPoller, HookEventsDispatcher
from pocket_cc.relay.input import InputRouter
from pocket_cc.relay.output import TranscriptPoller
from pocket_cc.relay.pane_watcher import PaneWatcher
from pocket_cc.relay.turn_controller import TurnController
from pocket_cc.tmux import TmuxError, TmuxManager

if TYPE_CHECKING:
    from pocket_cc.app.config import Config
    from pocket_cc.claude.events import HookEvent

logger = logging.getLogger(__name__)


def restore_bindings(
    *,
    config: Config,
    tmux: TmuxManager,
    lark: LarkClient,
    registry: Registry,
    state_store: StateStore,
) -> None:
    """Re-attach bindings persisted by a previous process.

    Per binding entry:
      - missing window_id / open_id     → drop (malformed snapshot)
      - open_id no longer in users.toml → drop, kill the stale tmux window,
                                           patch the orphan card. Privilege
                                           revoked; the Claude in there must
                                           not keep running.
      - workspace path changed in
        users.toml since last run       → drop + kill + patch orphan. Users
                                           config is authoritative; the next
                                           user message opens a fresh binding
                                           in the new cwd.
      - tmux window gone                → drop (user's next message re-creates)
      - tmux window alive + reconciled  → rebuild ChatBinding and register;
                                           if an active_card pointer was
                                           persisted, patch that Lark message
                                           with the "已重启" notice so the
                                           user doesn't stare at a ⏳ card

    We deliberately do not replay transcript history. The TranscriptReader
    will be re-created lazily on the next message and starts at end-of-file
    (the poller's normal swap path), so the user just continues from a
    clean slate. The active_card patch is the only side-effect that bridges
    the restart for the user.

    Best-effort throughout: any per-binding failure is logged and skipped
    rather than aborting the whole restore (one bad entry must not block
    unrelated chats from coming back online).
    """
    data = state_store.load()
    if data is None:
        return
    bindings = data.get("bindings")
    if not isinstance(bindings, dict):
        logger.warning("restore: state file missing bindings dict — skipping")
        return

    restored = 0
    for chat_id, entry in bindings.items():
        if not isinstance(entry, dict):
            logger.warning("restore: malformed entry — skipping", extra={"chat_id": chat_id})
            continue
        if _restore_one_binding(
            chat_id, entry, config=config, tmux=tmux, lark=lark, registry=registry
        ):
            restored += 1

    # Rewrite the snapshot so cleared active_card pointers (orphans we just
    # patched) and dropped bindings (dead windows) are reflected on disk.
    # Without this, a second restart would try to patch the same orphan card
    # again. Always save — even when restored==0, the file may need to be
    # pruned to reflect dropped bindings.
    state_store.save()
    logger.info("restore complete", extra={"restored": restored, "total": len(bindings)})


def _restore_one_binding(
    chat_id: str,
    entry: dict[str, Any],
    *,
    config: Config,
    tmux: TmuxManager,
    lark: LarkClient,
    registry: Registry,
) -> bool:
    """Restore a single binding entry. Returns True on success."""
    window_id = entry.get("window_id")
    if not isinstance(window_id, str):
        logger.warning("restore: entry missing window_id", extra={"chat_id": chat_id})
        return False
    open_id = entry.get("open_id")
    if not isinstance(open_id, str) or not open_id:
        # Without an open_id we can't route this binding (step 4 will also need
        # it to reconcile against config.users). Drop and let the user re-trigger.
        logger.warning("restore: entry missing open_id", extra={"chat_id": chat_id})
        return False

    # Reconcile against users.toml — the user table is the authoritative source
    # of "who's allowed + where do they work". Run *before* tmux lookup so we
    # actively tear down the stale Claude window instead of merely refusing to
    # re-attach (the running process would otherwise keep going invisibly).
    user = config.users.get(open_id)
    if user is None:
        logger.warning(
            "restore: open_id no longer in users config — dropping binding",
            extra={"chat_id": chat_id, "open_id": open_id, "window_id": window_id},
        )
        _cleanup_dropped_binding(chat_id, entry, window_id=window_id, tmux=tmux, lark=lark)
        return False

    stored_cwd = Path(str(entry.get("cwd", "")))
    if stored_cwd != user.workspace:
        logger.info(
            "restore: workspace changed in users.toml — dropping for fresh start",
            extra={
                "chat_id": chat_id,
                "open_id": open_id,
                "old_cwd": str(stored_cwd),
                "new_cwd": str(user.workspace),
            },
        )
        _cleanup_dropped_binding(chat_id, entry, window_id=window_id, tmux=tmux, lark=lark)
        return False

    try:
        window = tmux.find_window_by_id(window_id)
    except TmuxError:
        logger.warning(
            "restore: tmux lookup failed — dropping binding",
            extra={"chat_id": chat_id, "window_id": window_id},
            exc_info=True,
        )
        return False
    if window is None:
        logger.info(
            "restore: tmux window gone — dropping binding",
            extra={"chat_id": chat_id, "window_id": window_id},
        )
        return False

    try:
        binding = ChatBinding(
            chat_id=chat_id,
            open_id=open_id,
            window=window,
            cwd=Path(str(entry.get("cwd", ""))),
            created_at=float(entry.get("created_at", 0.0)),
            excluded_transcripts=frozenset(
                Path(p) for p in entry.get("excluded_transcripts", []) if isinstance(p, str)
            ),
            current_mode=str(entry.get("current_mode", "default")),
        )
    except (TypeError, ValueError):
        logger.warning(
            "restore: failed to rebuild ChatBinding — skipping",
            extra={"chat_id": chat_id},
            exc_info=True,
        )
        return False

    registry.set(binding)
    # The persisted active_card pointer is now stale (no live CardStream
    # behind it). Patch the orphan and let the next user message open a
    # fresh turn from scratch.
    active_card = entry.get("active_card")
    if isinstance(active_card, dict):
        _patch_orphan_card(chat_id, active_card, lark=lark)
    logger.info(
        "restore: binding re-attached",
        extra={"chat_id": chat_id, "window_id": window_id, "cwd": str(binding.cwd)},
    )
    return True


def _cleanup_dropped_binding(
    chat_id: str,
    entry: dict[str, Any],
    *,
    window_id: str,
    tmux: TmuxManager,
    lark: LarkClient,
) -> None:
    """Tear down a binding that reconcile refused to restore.

    Used when users.toml no longer authorizes the binding's open_id, or moved
    that user's workspace elsewhere. We:
      - kill the orphaned tmux window so the stale Claude process is stopped
        (privilege revocation must be effective; a running Claude in a
        no-longer-authorized cwd is exactly what we don't want)
      - patch any persisted active_card pointer with the "已重启" notice so
        the user's Lark UI doesn't keep showing a ⏳ running card for a turn
        the bot will never finish

    Both side effects are best-effort: failures here mustn't block dropping
    the remaining bindings.
    """
    try:
        tmux.kill_window(window_id)
    except TmuxError:
        logger.warning(
            "restore: failed to kill stale tmux window — leaving it for the operator",
            extra={"chat_id": chat_id, "window_id": window_id},
            exc_info=True,
        )
    active_card = entry.get("active_card")
    if isinstance(active_card, dict):
        _patch_orphan_card(chat_id, active_card, lark=lark)


def _patch_orphan_card(chat_id: str, active_card: dict[str, Any], *, lark: LarkClient) -> None:
    """Flip a dead-turn card to the ⏹ restart-notice state via cardkit.

    The orphan's ``card_id`` is what cardkit's whole-card replacement
    targets — no IM PATCH involved. We don't bother tracking a sequence
    here because the old CardStream is gone and won't try to PUT anything
    else: cardkit accepts ``sequence=1`` on a card it has never seen, and
    after this call the card is logically retired.

    ``message_id`` is still persisted for forensic / log correlation
    (matches what users see in chat) but isn't used as an update handle.
    """
    card_id = active_card.get("card_id")
    if not isinstance(card_id, str) or not card_id:
        return
    message_id = active_card.get("message_id")
    try:
        lark.update_card_entity(card_id, build_restart_notice_card_v2(), sequence=1)
    except LarkApiError:
        logger.warning(
            "restore: orphan card update failed (Lark side)",
            extra={"chat_id": chat_id, "card_id": card_id, "message_id": message_id},
            exc_info=True,
        )


class Pocketcc:
    """The pocket-cc process, hosted in one object for easy lifecycle control."""

    def __init__(self, config: Config) -> None:
        self._config = config

        self._tmux = TmuxManager(config.tmux_session)
        self._lark = LarkOapiClient(config.app_id, config.app_secret, domain=config.lark_domain)
        self._loop = LarkEventLoop(config.app_id, config.app_secret, domain=config.lark_domain)
        self._registry = Registry()
        # Persists the registry (L1 binding fields + L2 active-card pointer) to
        # ~/.pocket-cc/state.json on turn-lifecycle events. Step 1 is write-only
        # — restore (Step 2) reads back on startup and re-attaches to tmux.
        self._state_store = StateStore(pocket_cc_dir() / "state.json", self._registry)
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
            state_store=self._state_store,
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
        # Observability for WS disconnects: log when the SDK starts retrying
        # and the downtime once it succeeds. EventsPoller / TranscriptPoller
        # run independently of the WS so hooks + transcript updates keep
        # flowing during a brief drop; the visible failure mode is *inbound*
        # (messages + card actions Lark queued while we were unreachable).
        # These logs make "user says they sent a message but nothing
        # happened" trivially correlatable with a downtime window.
        self._ws_disconnect_started_at: float | None = None
        self._loop.on_reconnecting(self._handle_ws_reconnecting)
        self._loop.on_reconnected(self._handle_ws_reconnected)

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Boot all subsystems and block on the Lark WS loop."""
        # Per-user workspaces are validated (existence + writability) at config
        # load time; nothing to create here.
        self._tmux.ensure_session()
        # Re-attach bindings from the previous process **before** any poller
        # starts touching the registry — otherwise a restored binding could
        # race a transcript / pane tick that sees an empty Registry first.
        self._restore_bindings()
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
                "users": [
                    {"open_id": u.open_id, "workspace": str(u.workspace)}
                    for u in self._config.users.values()
                ],
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

    # ---------------------------------------------------------------- restore

    def _restore_bindings(self) -> None:
        """Bootstrap-side wrapper around :func:`restore_bindings`."""
        restore_bindings(
            config=self._config,
            tmux=self._tmux,
            lark=self._lark,
            registry=self._registry,
            state_store=self._state_store,
        )

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
                controller = TurnController(
                    binding=binding,
                    lark=self._lark,
                    config=self._config,
                    state_store=self._state_store,
                )
                self._controllers[binding.chat_id] = controller
            return controller

    # ---------------------------------------------------- hook event callbacks

    def _handle_session_start(self, binding: ChatBinding, event: HookEvent) -> None:
        """SessionStart for our Claude — (re)bind the transcript path so the
        poller follows the session authoritatively instead of guessing by
        mtime (which a concurrent same-cwd Claude could hijack). Handles
        first-bind and the /clear /compact rebind alike."""
        if not event.transcript_path:
            return
        path = Path(event.transcript_path)
        outcome = self._controller_for(binding).bind_session(path, event.session_id)
        if outcome != "noop":
            logger.info(
                "transcript %s via SessionStart hook",
                outcome,
                extra={
                    "chat_id": binding.chat_id,
                    "path": str(path),
                    "session_id": event.session_id,
                },
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

    # --------------------------------------------------- ws reconnect callbacks

    def _handle_ws_reconnecting(self) -> None:
        """SDK began retrying after a dropped WS — start the downtime clock.

        Fires on the SDK's asyncio loop thread. ``monotonic`` so the duration
        is robust against wall-clock jumps (NTP corrections, suspend/resume).
        A second drop before reconnect would overwrite the timestamp — fine,
        we report the most recent gap.
        """
        self._ws_disconnect_started_at = time.monotonic()
        logger.warning("ws reconnecting", extra={"event": "ws_reconnecting"})

    def _handle_ws_reconnected(self) -> None:
        """SDK re-established the WS — log downtime so we can correlate user
        reports ("I sent a message and nothing happened") with the gap."""
        started = self._ws_disconnect_started_at
        downtime_s = round(time.monotonic() - started, 2) if started is not None else None
        self._ws_disconnect_started_at = None
        logger.info(
            "ws reconnected",
            extra={"event": "ws_reconnected", "downtime_s": downtime_s},
        )

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
