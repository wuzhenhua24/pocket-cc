"""Claude Code hook event log — write + incremental read.

Claude hooks (configured in `~/.claude/settings.json`) execute a command
when key events happen (SessionStart, Stop, …). Our hook command writes
one JSON line per event to `~/.pocket-cc/events.jsonl`. This module owns
the read side of that pipe.

Why a file, not a socket: hooks fire even when pocket-cc isn't running
(e.g. user using desktop Claude). A file collects events durably; pocket-cc
tails it on startup with its byte-offset positioned at the tail (we don't
replay history — only events newer than process startup matter).

Schema (one record per line):
    {
      "event": "Stop",                   # canonical event name
      "timestamp": 1742140800.123,       # UNIX float
      "session_id": "uuid…",             # from hook payload
      "transcript_path": "/Users/…",     # absolute path to the session's jsonl
      "cwd": "/Users/…",                 # working dir Claude was launched in
      "raw": { ...original payload... }  # full hook payload (escape hatch)
    }

Reader semantics mirror :class:`pocket_cc.claude.transcript.TranscriptReader`:
byte-offset cursor, truncation-safe, partial-line safe. New events between
calls are returned in arrival order.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — used as dataclass field type, must be eager
from typing import Any

logger = logging.getLogger(__name__)

# Hook events we actually act on. Others (PreToolUse / PostToolUse / etc.) get
# written by the receiver but ignored by the reader's parse — keeping them in
# the log makes debugging easier without bloating routing.
KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        "SessionStart",
        "Stop",
        "StopFailure",
        "Notification",
        "SessionEnd",
    }
)


@dataclass(frozen=True, slots=True)
class HookEvent:
    """Parsed hook event ready for routing."""

    event: str  # e.g. "Stop", "SessionStart"
    timestamp: float  # UNIX float from when the receiver ran
    session_id: str
    transcript_path: str  # absolute path str; matched against binding.transcript_path
    cwd: str
    raw: dict[str, Any]


def write_event(path: Path, event_name: str, payload: dict[str, Any]) -> None:
    """Append one HookEvent record to the events log.

    Synchronous, line-atomic for typical hook payloads (< PIPE_BUF on POSIX).
    The hook receiver calls this exactly once per Claude invocation, then exits
    — Claude waits on the hook process, so we keep this *fast* (no network,
    no expensive serialization).
    """
    record = {
        "event": event_name,
        "timestamp": time.time(),
        "session_id": str(payload.get("session_id", "")),
        "transcript_path": str(payload.get("transcript_path", "")),
        "cwd": str(payload.get("cwd", "")),
        "raw": payload,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    # Single-shot `open(...).write(line)` so the OS does one write() syscall.
    # On POSIX, O_APPEND + a single write < PIPE_BUF (4096 bytes) is atomic
    # against other concurrent appenders. Larger lines may interleave —
    # acceptable risk for M1-E since typical hook records are < 1KB.
    with path.open("a", encoding="utf-8") as fp:
        fp.write(line)


def parse_line(line: str | bytes) -> HookEvent | None:
    """Parse one JSONL line into a HookEvent. Bad lines → None (never raises)."""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    event = str(record.get("event", ""))
    if not event:
        return None
    raw = record.get("raw")
    if not isinstance(raw, dict):
        raw = {}
    return HookEvent(
        event=event,
        timestamp=_as_float(record.get("timestamp"), default=0.0),
        session_id=str(record.get("session_id", "")),
        transcript_path=str(record.get("transcript_path", "")),
        cwd=str(record.get("cwd", "")),
        raw=raw,
    )


@dataclass
class EventsReader:
    """Incremental tail of `events.jsonl` keyed by byte offset.

    Defaults to starting at the **current end of file** so historical events
    (from before pocket-cc started) are skipped. This avoids replaying days
    of hook history into stale bindings on every boot.

    Call :meth:`seek_to_end` after construction if you need that explicitly
    on an existing reader (e.g. on file rotation).

    Not thread-safe — call from one poller thread.
    """

    path: Path
    byte_offset: int = 0
    events_emitted: int = field(default=0, init=False)
    _started_at_eof: bool = field(default=False, init=False)

    def seek_to_end(self) -> None:
        """Discard everything currently in the file. New events only from here on."""
        if self.path.exists():
            self.byte_offset = self.path.stat().st_size
        else:
            self.byte_offset = 0
        self._started_at_eof = True

    def read_new(self) -> list[HookEvent]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.byte_offset:
            # Truncation / rotation. Reset and replay from start.
            logger.warning(
                "events log shrank, replaying from start",
                extra={"path": str(self.path)},
            )
            self.byte_offset = 0
        if size == self.byte_offset:
            return []

        with self.path.open("rb") as fp:
            fp.seek(self.byte_offset)
            chunk = fp.read()

        consumed = 0
        events: list[HookEvent] = []
        for raw_line in chunk.splitlines(keepends=True):
            if not raw_line.endswith(b"\n"):
                break  # partial line — wait
            consumed += len(raw_line)
            ev = parse_line(raw_line)
            if ev is not None:
                events.append(ev)
        self.byte_offset += consumed
        self.events_emitted += len(events)
        return events


def _as_float(value: object, *, default: float) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default
