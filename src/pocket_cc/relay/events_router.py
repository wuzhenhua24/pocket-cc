"""Route Claude hook events into binding state transitions.

The receiver side (``claude.hooks.receive_event``) writes hook payloads into
``events.jsonl`` synchronously. This module **tails** that log in the
pocket-cc process and translates each event into the right binding action:

  - **SessionStart** — pocket-cc's Claude just booted. Lock the
    binding's `transcript_path` to the new file early (before the
    transcript poller's mtime heuristic catches it).
  - **Stop**         — current turn finishes successfully → seal card as
    ✅ done immediately (instead of waiting for the next user message).
  - **StopFailure**  — Claude crashed or hit an API error → seal as ❌ failed.
  - **Notification** / **SessionEnd** — accepted, but no card action in M1-E.

Disambiguation:
  Multiple Claude processes may be running on the same machine (e.g. user's
  desktop Claude + pocket-cc's tmux Claude). All of them write to the same
  ``events.jsonl``. We use the binding's `excluded_transcripts` snapshot
  (M1-D-15) to filter — an event's transcript_path that *was already known*
  at binding creation can't be ours, so we ignore it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pocket_cc.app.persistence import ChatBinding, Registry
    from pocket_cc.claude.events import EventsReader, HookEvent

logger = logging.getLogger(__name__)


def _same_dir(binding_cwd: Path, event_cwd: str) -> bool:
    """Symlink-tolerant cwd comparison.

    Claude reports the OS-realpath cwd in hook payloads (e.g. ``/private/tmp``
    on macOS) while the binding holds the path as configured (``/tmp``); a plain
    string compare would miss that match and the event would never attribute.
    ``resolve()`` collapses symlinks on both sides; if resolution fails (e.g.
    the dir no longer exists), fall back to a literal compare.
    """
    if not event_cwd:
        return False
    try:
        return binding_cwd.resolve() == Path(event_cwd).resolve()
    except OSError:
        return str(binding_cwd) == event_cwd


# Callback signatures the bootstrap wires up.
SessionStartCb = Callable[["ChatBinding", "HookEvent"], None]
StopCb = Callable[["ChatBinding", "HookEvent"], None]
StopFailureCb = Callable[["ChatBinding", "HookEvent"], None]


class HookEventsDispatcher:
    """Resolve event → binding, then fire the matching callback.

    All callbacks are optional — register only what you need. M1-E wires
    `on_stop` and `on_stop_failure`; SessionStart is observed for early
    transcript-path locking but does not require a user callback.
    """

    def __init__(
        self,
        registry: Registry,
        *,
        on_session_start: SessionStartCb | None = None,
        on_stop: StopCb | None = None,
        on_stop_failure: StopFailureCb | None = None,
    ) -> None:
        self._registry = registry
        self._on_session_start = on_session_start
        self._on_stop = on_stop
        self._on_stop_failure = on_stop_failure

    def dispatch(self, event: HookEvent) -> None:
        try:
            if event.event == "SessionStart":
                self._handle_session_start(event)
            elif event.event == "Stop":
                self._handle_stop(event, failed=False)
            elif event.event == "StopFailure":
                self._handle_stop(event, failed=True)
            else:
                logger.debug(
                    "hook event ignored",
                    extra={"event": event.event, "session_id": event.session_id},
                )
        except Exception:
            logger.warning(
                "hook event dispatch raised",
                extra={"event": event.event, "session_id": event.session_id},
                exc_info=True,
            )

    # --------------------------------------------------------- session_start

    def _handle_session_start(self, event: HookEvent) -> None:
        """Attribute the event's transcript_path to a waiting binding and lock
        it early (before the poller's mtime heuristic would catch it)."""
        binding = self._match_waiting_binding(event)
        if binding is not None and self._on_session_start is not None:
            self._on_session_start(binding, event)

    def _match_waiting_binding(self, event: HookEvent) -> ChatBinding | None:
        """Find the unique waiting binding an event's transcript belongs to.

        Shared by SessionStart (early lock) and the Stop fallback (lazy lock).
        Matches a binding only when *all* hold:
          - it has no transcript_path locked yet (the poller hasn't picked one
            up either)
          - the event's cwd matches the binding's cwd (symlink-tolerant)
          - the event's transcript_path is *not* in
            ``binding.excluded_transcripts`` (so a concurrent desktop/other
            Claude in the same cwd can't be mistaken for ours)
        Ambiguity (multiple matching bindings) → None, to avoid binding to the
        wrong session.
        """
        if not event.transcript_path:
            return None
        candidates = [
            b
            for b in self._registry.all()
            if b.transcript_path is None
            and _same_dir(b.cwd, event.cwd)
            and Path(event.transcript_path) not in b.excluded_transcripts
        ]
        if not candidates:
            return None
        if len(candidates) > 1:
            logger.info(
                "hook event matched multiple waiting bindings — skipping",
                extra={"event": event.event, "cwd": event.cwd, "count": len(candidates)},
            )
            return None
        return candidates[0]

    # -------------------------------------------------------------- stop / fail

    def _handle_stop(self, event: HookEvent, *, failed: bool) -> None:
        """Find the binding owning this transcript and fire the callback.

        Primary path: the transcript was already locked (by SessionStart or the
        poller), so a direct transcript_path match works. Fallback: if no
        binding has locked it yet, attribute via the same waiting-binding match
        SessionStart uses and **lazily lock** using the path the Stop event
        itself carries. Without this fallback a Stop that races ahead of the
        lock (SessionStart missed/ambiguous, or poller hadn't ticked) is
        dropped — and since a turn only leaves RUNNING via its Stop, the drop
        now wedges the turn permanently (every later message is rejected).
        """
        target = Path(event.transcript_path) if event.transcript_path else None
        if target is None:
            return
        binding = self._find_binding_by_transcript(target)
        if binding is None:
            binding = self._match_waiting_binding(event)
            if binding is None:
                # Truly not ours (desktop/other Claude), or ambiguous → leave it.
                return
            # Lazily lock so this Stop — and any later events — attribute.
            if self._on_session_start is not None:
                self._on_session_start(binding, event)
        callback = self._on_stop_failure if failed else self._on_stop
        if callback is not None:
            callback(binding, event)

    def _find_binding_by_transcript(self, transcript_path: Path) -> ChatBinding | None:
        for binding in self._registry.all():
            if binding.transcript_path is not None and binding.transcript_path == transcript_path:
                if transcript_path in binding.excluded_transcripts:
                    # Defensive: shouldn't happen given the snapshot logic,
                    # but if it ever does, refuse to act.
                    return None
                return binding
        return None


class EventsPoller:
    """Background thread that tails events.jsonl and dispatches each new line."""

    def __init__(
        self,
        reader: EventsReader,
        dispatcher: HookEventsDispatcher,
        *,
        interval_s: float = 0.5,
    ) -> None:
        self._reader = reader
        self._dispatcher = dispatcher
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="events-poller", daemon=True)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        # Skip the entire pre-existing event log. We only care about events
        # that happen after pocket-cc started.
        self._reader.seek_to_end()
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._started:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                events = self._reader.read_new()
            except Exception:
                logger.warning("events reader tick raised", exc_info=True)
                events = []
            for ev in events:
                self._dispatcher.dispatch(ev)
            self._stop.wait(self._interval_s)
