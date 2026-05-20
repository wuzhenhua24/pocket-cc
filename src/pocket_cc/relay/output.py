"""Background transcript poller.

One thread, ``interval_s`` cadence (default 0.5s). Each tick:

  1. snapshot the current bindings from :class:`Registry`
  2. for each binding, resolve the active transcript path via
     :func:`pocket_cc.claude.find_active_transcript`
  3. swap the binding's `transcript_reader` if the path changed (= Claude
     started a new session after `/clear`, or just woke up for the first time)
  4. call `read_new()` and dispatch any events to ``on_events``.

The poller stays narrowly responsible for "what did Claude write since last
tick". Turning events into card updates is the bootstrap layer's job — it
holds the `TurnAccumulator` and `CardStream`.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from pocket_cc.claude.session_index import find_active_transcript

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pocket_cc.app.persistence import ChatBinding, Registry
    from pocket_cc.relay.turn_controller import TurnController

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 0.5


class TranscriptPoller:
    """Polls all active chat bindings for new Claude transcript events."""

    def __init__(
        self,
        registry: Registry,
        controller_for: Callable[[ChatBinding], TurnController],
        *,
        projects_dir: Path,
        interval_s: float = _DEFAULT_INTERVAL_S,
    ) -> None:
        self._registry = registry
        self._controller_for = controller_for
        self._projects_dir = projects_dir
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="transcript-poller", daemon=True)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._started:
            self._thread.join(timeout=5)

    # -------------------------------------------------------------- internal

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for binding in self._registry.all():
                    self._poll(binding)
            except Exception:
                # Never let one bad binding crash the whole poller.
                logger.warning("transcript poller tick raised", exc_info=True)
            self._stop.wait(self._interval_s)

    def _poll(self, binding: ChatBinding) -> None:
        # The filesystem scan (slow I/O) stays here; the swap + reader read +
        # ingest happen inside the controller so all transcript-reader access
        # is serialized by one owner.
        #   1. `exclude` — files that existed at binding creation. These were
        #      written by other Claude instances (desktop, prior pocket-cc
        #      runs) and never count, even if they keep mtime-bumping from
        #      concurrent use. (M1-D-15)
        #   2. `after_ts` — secondary filter for files modified long before
        #      the binding started. (M1-D-14, kept as belt+braces)
        current = find_active_transcript(
            binding.cwd,
            self._projects_dir,
            after_ts=binding.created_at,
            exclude=binding.excluded_transcripts,
        )
        self._controller_for(binding).poll_transcript(current)
