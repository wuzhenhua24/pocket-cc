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
from pocket_cc.claude.transcript import TranscriptReader

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pocket_cc.app.persistence import ChatBinding, Registry
    from pocket_cc.claude.transcript import Event

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 0.5


class TranscriptPoller:
    """Polls all active chat bindings for new Claude transcript events."""

    def __init__(
        self,
        registry: Registry,
        on_events: Callable[[ChatBinding, list[Event]], None],
        *,
        projects_dir: Path,
        interval_s: float = _DEFAULT_INTERVAL_S,
    ) -> None:
        self._registry = registry
        self._on_events = on_events
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
        with binding.lock:
            # Two-layer defense against picking up the wrong transcript:
            #   1. `exclude` — files that existed at binding creation. These
            #      were written by other Claude instances (desktop, prior
            #      pocket-cc runs) and never count, even if they keep
            #      mtime-bumping from concurrent use. (M1-D-15)
            #   2. `after_ts` — secondary filter for files modified long
            #      before the binding started. (M1-D-14, kept as belt+braces)
            current = find_active_transcript(
                binding.cwd,
                self._projects_dir,
                after_ts=binding.created_at,
                exclude=binding.excluded_transcripts,
            )
            if current is None:
                logger.debug(
                    "no active transcript found",
                    extra={
                        "chat_id": binding.chat_id,
                        "cwd": str(binding.cwd),
                        "after_ts": binding.created_at,
                        "excluded_count": len(binding.excluded_transcripts),
                        "has_hook_path": binding.transcript_path is not None,
                    },
                )
                return
            if binding.transcript_path != current:
                # First time, or Claude rotated session (e.g. `/clear`).
                binding.transcript_path = current
                binding.transcript_reader = TranscriptReader(path=current)
                logger.info(
                    "transcript switched",
                    extra={"chat_id": binding.chat_id, "path": str(current)},
                )

            reader = binding.transcript_reader
            if reader is None:
                return
            events = reader.read_new()
            logger.debug(
                "transcript poll tick",
                extra={
                    "chat_id": binding.chat_id,
                    "path": str(binding.transcript_path),
                    "offset": reader.byte_offset,
                    "new_events": len(events),
                },
            )

        if events:
            try:
                self._on_events(binding, events)
            except Exception:
                logger.warning(
                    "on_events callback raised",
                    extra={"chat_id": binding.chat_id},
                    exc_info=True,
                )
