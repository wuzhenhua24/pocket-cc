"""Claude Code integration — transcript parsing, hooks, session indexing."""

from pocket_cc.claude.events import (
    KNOWN_EVENTS,
    EventsReader,
    HookEvent,
    write_event,
)
from pocket_cc.claude.events import (
    parse_line as parse_event_line,
)
from pocket_cc.claude.pane_inspector import (
    ParsedOption,
    ParsedPrompt,
    PromptKind,
    inspect_pane,
)
from pocket_cc.claude.session_index import (
    encode_cwd_loose,
    encode_cwd_strict,
    find_active_transcript,
    find_project_dir,
    snapshot_existing_transcripts,
)
from pocket_cc.claude.transcript import (
    AssistantText,
    AssistantThinking,
    Event,
    ToolResult,
    ToolUse,
    TranscriptReader,
    UserText,
    parse_record,
)

__all__ = [
    "KNOWN_EVENTS",
    "AssistantText",
    "AssistantThinking",
    "Event",
    "EventsReader",
    "HookEvent",
    "ParsedOption",
    "ParsedPrompt",
    "PromptKind",
    "ToolResult",
    "ToolUse",
    "TranscriptReader",
    "UserText",
    "encode_cwd_loose",
    "encode_cwd_strict",
    "find_active_transcript",
    "find_project_dir",
    "inspect_pane",
    "parse_event_line",
    "parse_record",
    "snapshot_existing_transcripts",
    "write_event",
]
