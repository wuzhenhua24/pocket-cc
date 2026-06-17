"""Claude Code JSONL transcript parser + incremental byte-offset reader.

Source of truth for what Claude is doing — pocket-cc projects this stream into
Lark cards (see DESIGN.md §2). This module does **structured parsing only**;
rendering for Lark is the relay layer's job.

Top-level JSONL record types we care about:
  - `user`       → user text, or a list with `tool_result` blocks
  - `assistant`  → list of `text` / `thinking` / `tool_use` blocks

Other record types (`system`, `attachment`, `last-prompt`,
`file-history-snapshot`, `summary`) are intentionally skipped at this layer —
they have no direct UX in the Lark projection. Hook events (Stop /
StopFailure / Notification) come from a separate `events.jsonl` channel,
implemented in `claude/monitor.py` (later slice).

The `permission-mode` record IS surfaced — Claude writes one every time the
user presses Shift-Tab (cycles default/acceptEdits/plan/bypassPermissions),
and pocket-cc reflects the current mode in the Lark card's Mode button label
so the user can tell which mode they're in without staring at tmux.

Schema reference (empirically verified against Claude Code 2.1.x transcripts):

    # user record (plain text)
    {"type":"user","uuid":"...","timestamp":"...","message":{
        "role":"user","content":"hello"}}

    # user record (tool result return)
    {"type":"user","uuid":"...","timestamp":"...","message":{
        "role":"user","content":[
            {"type":"tool_result","tool_use_id":"toolu_...",
             "content":"...","is_error":false}]}}

    # assistant record
    {"type":"assistant","uuid":"...","timestamp":"...","message":{
        "role":"assistant","content":[
            {"type":"thinking","thinking":"...","signature":"..."},
            {"type":"text","text":"..."},
            {"type":"tool_use","id":"toolu_...","name":"Read",
             "input":{"file_path":"..."}}]}}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — used as dataclass field type, must be eager
from typing import Any, Final

# JSONL records that carry no Lark-visible content. Skipped silently.
# `permission-mode` is intentionally *not* here — see `_parse_permission_mode`.
_IGNORED_RECORD_TYPES: Final[frozenset[str]] = frozenset(
    {
        "system",
        "attachment",
        "last-prompt",
        "file-history-snapshot",
        "summary",
    }
)


# ---------------------------------------------------------------- event types
# Independent frozen dataclasses (no inheritance) to keep `slots=True` happy
# and to let mypy do exhaustive union narrowing via isinstance.


@dataclass(frozen=True, slots=True)
class UserText:
    uuid: str
    timestamp: str
    text: str


@dataclass(frozen=True, slots=True)
class AssistantText:
    uuid: str
    timestamp: str
    text: str


@dataclass(frozen=True, slots=True)
class AssistantThinking:
    uuid: str
    timestamp: str
    text: str


@dataclass(frozen=True, slots=True)
class ToolUse:
    uuid: str
    timestamp: str
    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    uuid: str
    timestamp: str
    tool_use_id: str
    content: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class ModeChange:
    """Permission-mode change emitted on Shift-Tab cycling.

    Record shape (no ``message`` envelope, unlike user/assistant):
        {"type":"permission-mode","permissionMode":"acceptEdits","sessionId":"..."}

    Claude writes one of these at session start (mode == "default") and on
    every subsequent mode toggle. The relay layer uses the latest-seen value
    to label the Mode button so users in Lark know which mode is active
    without checking the tmux pane.
    """

    uuid: str
    timestamp: str
    mode: str  # "default" | "acceptEdits" | "plan" | "bypassPermissions" | ...


Event = UserText | AssistantText | AssistantThinking | ToolUse | ToolResult | ModeChange


# ----------------------------------------------------------------------- API


def parse_record(record: dict[str, Any]) -> list[Event]:
    """Parse one already-decoded JSONL record into 0..n events.

    Unknown record types and malformed messages yield an empty list — this
    module never raises on bad data, since transcripts are produced by a
    third-party tool whose schema is allowed to evolve.
    """
    record_type = record.get("type")
    if record_type in _IGNORED_RECORD_TYPES:
        return []

    uuid = record.get("uuid", "")
    timestamp = record.get("timestamp", "")

    # permission-mode records have no `message` envelope — handle before the
    # isinstance(message, dict) guard below so they don't get dropped.
    if record_type == "permission-mode":
        return _parse_permission_mode(uuid, timestamp, record)

    message = record.get("message")
    if not isinstance(message, dict):
        return []

    if record_type == "user":
        return _parse_user(uuid, timestamp, message)
    if record_type == "assistant":
        return _parse_assistant(uuid, timestamp, message)
    return []


def parse_line(line: str | bytes) -> list[Event]:
    """Decode one JSONL line and parse it. Empty/invalid lines yield []."""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return []
    line = line.strip()
    if not line:
        return []
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(record, dict):
        return []
    return parse_record(record)


# ------------------------------------------------------------- record helpers


def _parse_user(uuid: str, timestamp: str, message: dict[str, Any]) -> list[Event]:
    content = message.get("content")
    # plain text user message
    if isinstance(content, str):
        text = content.strip()
        return [UserText(uuid=uuid, timestamp=timestamp, text=text)] if text else []

    if not isinstance(content, list):
        return []

    events: list[Event] = []
    for block in content:
        if not isinstance(block, dict):
            # raw strings inside content[] occasionally appear; treat as user text
            if isinstance(block, str) and block.strip():
                events.append(UserText(uuid=uuid, timestamp=timestamp, text=block.strip()))
            continue
        block_type = block.get("type")
        if block_type == "tool_result":
            events.append(_make_tool_result(uuid, timestamp, block))
        elif block_type == "text":
            text = (block.get("text") or "").strip()
            if text:
                events.append(UserText(uuid=uuid, timestamp=timestamp, text=text))
        # other block types in a user turn are unusual; skip
    return events


def _parse_assistant(uuid: str, timestamp: str, message: dict[str, Any]) -> list[Event]:
    content = message.get("content")
    if not isinstance(content, list):
        return []

    events: list[Event] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = (block.get("text") or "").strip()
            if text:
                events.append(AssistantText(uuid=uuid, timestamp=timestamp, text=text))
        elif block_type == "thinking":
            text = (block.get("thinking") or "").strip()
            if text:
                events.append(AssistantThinking(uuid=uuid, timestamp=timestamp, text=text))
        elif block_type == "tool_use":
            events.append(_make_tool_use(uuid, timestamp, block))
    return events


def _parse_permission_mode(uuid: str, timestamp: str, record: dict[str, Any]) -> list[Event]:
    """permission-mode records have no `message`; the mode is at the top level.

    Example: ``{"type":"permission-mode","permissionMode":"acceptEdits", ...}``.
    Records missing or with a non-str ``permissionMode`` yield no event
    (rather than raising) — schema drift in third-party transcripts
    shouldn't crash the bot.
    """
    mode = record.get("permissionMode")
    if not isinstance(mode, str) or not mode:
        return []
    return [ModeChange(uuid=uuid, timestamp=timestamp, mode=mode)]


def _make_tool_use(uuid: str, timestamp: str, block: dict[str, Any]) -> ToolUse:
    raw_input = block.get("input")
    tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
    return ToolUse(
        uuid=uuid,
        timestamp=timestamp,
        tool_use_id=str(block.get("id", "")),
        tool_name=str(block.get("name", "")),
        tool_input=tool_input,
    )


def _make_tool_result(uuid: str, timestamp: str, block: dict[str, Any]) -> ToolResult:
    raw_content = block.get("content", "")
    # `content` may be a string or a list of {type:"text", text:"..."} blocks
    if isinstance(raw_content, list):
        parts: list[str] = []
        for sub in raw_content:
            if isinstance(sub, dict) and sub.get("type") == "text":
                parts.append(str(sub.get("text", "")))
            elif isinstance(sub, str):
                parts.append(sub)
        content_str = "\n".join(parts)
    else:
        content_str = str(raw_content)
    return ToolResult(
        uuid=uuid,
        timestamp=timestamp,
        tool_use_id=str(block.get("tool_use_id", "")),
        content=content_str,
        is_error=bool(block.get("is_error", False)),
    )


# ----------------------------------------------------------- incremental read


@dataclass
class TranscriptReader:
    """Incremental JSONL reader keyed by byte offset.

    Handles:
      - Files that don't exist yet (no-op).
      - Truncation (offset > file size → reset to 0, replay from start).
      - Partial lines (chunk doesn't end with `\\n` → wait until next call).

    Not thread-safe. Call from one polling task per file.
    """

    path: Path
    byte_offset: int = 0
    # Total events emitted so far — useful for cheap stats / logging.
    events_emitted: int = field(default=0, init=False)

    def read_new(self) -> list[Event]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.byte_offset:
            # File was truncated or replaced. Replay from start.
            self.byte_offset = 0
        if size == self.byte_offset:
            return []

        with self.path.open("rb") as fp:
            fp.seek(self.byte_offset)
            chunk = fp.read()

        consumed = 0
        events: list[Event] = []
        for raw_line in chunk.splitlines(keepends=True):
            if not raw_line.endswith(b"\n"):
                # Incomplete line — leave for next call so we don't half-parse.
                break
            consumed += len(raw_line)
            events.extend(parse_line(raw_line))
        self.byte_offset += consumed
        self.events_emitted += len(events)
        return events

    def reset(self) -> None:
        """Forget read progress. Next `read_new()` re-parses from the top."""
        self.byte_offset = 0
        self.events_emitted = 0


# Records to scan from the top when sniffing for a fork marker. A Claude
# branch/fork transcript begins with the records copied from its source
# session, so the marker is always near the start; 50 is ample headroom.
_FORK_SCAN_LINES: Final[int] = 50


def fork_prefix_offset(path: Path) -> int:
    """Byte offset past a fork transcript's copied prefix, else 0.

    A Claude ``/branch`` / ``/fork`` session starts a *new* transcript that
    begins with the records copied from its source session (each marked with a
    ``forkedFrom`` object), then diverges. A fresh :class:`TranscriptReader`
    started at offset 0 would replay that whole copied conversation as if it
    were new card content. Seeding the reader past the copied prefix avoids it.

    The seek is deliberately gated on an actual ``forkedFrom`` marker: a normal
    new session (``/clear``, ``/compact``) has none and returns 0, so it reads
    from the top — and so we never risk skipping a genuine first turn that a
    fast ``/clear`` follow-up may have already appended.

    :param path: The new (forked) transcript JSONL path.
    :returns: Offset of the last complete-line boundary when ``path`` is a fork
        transcript (new post-fork records are appended after it); 0 for a
        normal new session or when the file can't be read.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    is_fork = False
    for raw in data.splitlines()[:_FORK_SCAN_LINES]:
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(record, dict) and isinstance(record.get("forkedFrom"), dict):
            is_fork = True
            break
    if not is_fork:
        return 0
    # Start at the last complete line: skip everything copied into the fork so
    # far. A partial trailing line (if Claude is mid-write) is left for the
    # reader, which already tolerates incomplete lines.
    last_newline = data.rfind(b"\n")
    return last_newline + 1 if last_newline >= 0 else 0
