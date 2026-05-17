"""Unit tests for claude/transcript.py — pure parsing, no I/O for parse_*."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pocket_cc.claude.transcript import (
    AssistantText,
    AssistantThinking,
    ToolResult,
    ToolUse,
    TranscriptReader,
    UserText,
    parse_line,
    parse_record,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------- parse_record


def test_parse_user_plain_text() -> None:
    record = {
        "type": "user",
        "uuid": "u1",
        "timestamp": "2026-04-22T08:58:11.131Z",
        "message": {"role": "user", "content": "hello"},
    }
    events = parse_record(record)
    assert events == [UserText(uuid="u1", timestamp="2026-04-22T08:58:11.131Z", text="hello")]


def test_parse_user_with_tool_result() -> None:
    record = {
        "type": "user",
        "uuid": "u2",
        "timestamp": "t",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_abc",
                    "content": "file contents here",
                    "is_error": False,
                }
            ],
        },
    }
    events = parse_record(record)
    assert events == [
        ToolResult(
            uuid="u2",
            timestamp="t",
            tool_use_id="toolu_abc",
            content="file contents here",
            is_error=False,
        )
    ]


def test_parse_user_with_tool_result_error_flag() -> None:
    record = {
        "type": "user",
        "uuid": "u3",
        "timestamp": "t",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_x",
                    "content": "ENOENT",
                    "is_error": True,
                }
            ],
        },
    }
    events = parse_record(record)
    assert len(events) == 1
    result = events[0]
    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert result.content == "ENOENT"


def test_parse_user_tool_result_content_as_text_blocks() -> None:
    # Some tools return content as [{type:"text", text:"..."}]
    record = {
        "type": "user",
        "uuid": "u4",
        "timestamp": "t",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_y",
                    "content": [
                        {"type": "text", "text": "line1"},
                        {"type": "text", "text": "line2"},
                    ],
                }
            ],
        },
    }
    events = parse_record(record)
    assert len(events) == 1
    result = events[0]
    assert isinstance(result, ToolResult)
    assert result.content == "line1\nline2"
    assert result.is_error is False


def test_parse_assistant_text_thinking_tool_use_mixed() -> None:
    record = {
        "type": "assistant",
        "uuid": "a1",
        "timestamp": "t",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "let me check", "signature": "sig"},
                {"type": "text", "text": "Reading the file..."},
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "Read",
                    "input": {"file_path": "/tmp/foo.py"},
                },
            ],
        },
    }
    events = parse_record(record)
    assert events == [
        AssistantThinking(uuid="a1", timestamp="t", text="let me check"),
        AssistantText(uuid="a1", timestamp="t", text="Reading the file..."),
        ToolUse(
            uuid="a1",
            timestamp="t",
            tool_use_id="toolu_abc",
            tool_name="Read",
            tool_input={"file_path": "/tmp/foo.py"},
        ),
    ]


def test_parse_skips_empty_strings() -> None:
    record = {
        "type": "assistant",
        "uuid": "a",
        "timestamp": "t",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "   "},
                {"type": "thinking", "thinking": ""},
            ],
        },
    }
    assert parse_record(record) == []


def test_parse_ignores_known_metadata_records() -> None:
    for record_type in [
        "system",
        "attachment",
        "permission-mode",
        "last-prompt",
        "file-history-snapshot",
        "summary",
    ]:
        assert parse_record({"type": record_type, "uuid": "x"}) == []


def test_parse_unknown_record_type() -> None:
    assert parse_record({"type": "wat", "uuid": "x", "message": {}}) == []


def test_parse_malformed_records() -> None:
    # missing message
    assert parse_record({"type": "user", "uuid": "x"}) == []
    # message not a dict
    assert parse_record({"type": "user", "uuid": "x", "message": "oops"}) == []
    # assistant content not a list
    assert parse_record({"type": "assistant", "uuid": "x", "message": {"content": "txt"}}) == []


def test_parse_tool_use_with_missing_input_falls_back_to_empty_dict() -> None:
    record = {
        "type": "assistant",
        "uuid": "a",
        "timestamp": "t",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash"}],
        },
    }
    events = parse_record(record)
    assert len(events) == 1
    tu = events[0]
    assert isinstance(tu, ToolUse)
    assert tu.tool_input == {}


# ---------------------------------------------------------- parse_line


def test_parse_line_handles_str_and_bytes() -> None:
    record = json.dumps(
        {
            "type": "user",
            "uuid": "u",
            "timestamp": "t",
            "message": {"role": "user", "content": "hi"},
        }
    )
    assert parse_line(record) == [UserText(uuid="u", timestamp="t", text="hi")]
    assert parse_line(record.encode()) == [UserText(uuid="u", timestamp="t", text="hi")]


def test_parse_line_empty_and_invalid() -> None:
    assert parse_line("") == []
    assert parse_line("   ") == []
    assert parse_line(b"") == []
    assert parse_line("{not json") == []
    assert parse_line("123") == []  # JSON but not a dict
    assert parse_line("[1,2,3]") == []  # JSON but not a dict


def test_parse_line_handles_invalid_utf8() -> None:
    assert parse_line(b"\xff\xfeinvalid") == []


# ---------------------------------------------------------- TranscriptReader


def _write_lines(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _append_lines(path: Path, records: list[dict[str, object]]) -> None:
    with path.open("a") as fp:
        for r in records:
            fp.write(json.dumps(r) + "\n")


def test_reader_missing_file_returns_empty(tmp_path: Path) -> None:
    reader = TranscriptReader(path=tmp_path / "nope.jsonl")
    assert reader.read_new() == []
    assert reader.byte_offset == 0


def test_reader_reads_all_then_nothing(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    _write_lines(
        f,
        [
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "t",
                "message": {"role": "user", "content": "hello"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "t",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                },
            },
        ],
    )
    reader = TranscriptReader(path=f)
    events = reader.read_new()
    assert [type(e).__name__ for e in events] == ["UserText", "AssistantText"]
    assert reader.events_emitted == 2
    # Second call: no new content
    assert reader.read_new() == []


def test_reader_incremental_append(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    _write_lines(
        f,
        [
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "t",
                "message": {"role": "user", "content": "first"},
            }
        ],
    )
    reader = TranscriptReader(path=f)
    first = reader.read_new()
    assert len(first) == 1
    offset_after_first = reader.byte_offset

    _append_lines(
        f,
        [
            {
                "type": "user",
                "uuid": "u2",
                "timestamp": "t",
                "message": {"role": "user", "content": "second"},
            }
        ],
    )
    second = reader.read_new()
    assert len(second) == 1
    assert isinstance(second[0], UserText)
    assert second[0].text == "second"
    assert reader.byte_offset > offset_after_first


def test_reader_handles_partial_line(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    record = json.dumps(
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "t",
            "message": {"role": "user", "content": "complete"},
        }
    )
    # Write a complete line + a partial (no trailing \n) second line
    f.write_text(record + "\n" + '{"type": "user", "uu')
    reader = TranscriptReader(path=f)
    events = reader.read_new()
    # Only the complete line is consumed
    assert len(events) == 1
    # Now finish the second line — reader should pick it up
    f.write_text(
        record
        + "\n"
        + json.dumps(
            {
                "type": "user",
                "uuid": "u2",
                "timestamp": "t",
                "message": {"role": "user", "content": "now complete"},
            }
        )
        + "\n"
    )
    events2 = reader.read_new()
    assert len(events2) == 1
    assert isinstance(events2[0], UserText)
    assert events2[0].text == "now complete"


def test_reader_handles_truncation(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    _write_lines(
        f,
        [
            {
                "type": "user",
                "uuid": f"u{i}",
                "timestamp": "t",
                "message": {"role": "user", "content": f"msg{i}"},
            }
            for i in range(5)
        ],
    )
    reader = TranscriptReader(path=f)
    assert len(reader.read_new()) == 5

    # Truncate to a single shorter record — simulates Claude's `/clear`
    _write_lines(
        f,
        [
            {
                "type": "user",
                "uuid": "fresh",
                "timestamp": "t",
                "message": {"role": "user", "content": "after clear"},
            }
        ],
    )
    events = reader.read_new()
    # File size is smaller than previous offset → reset and replay from top
    assert len(events) == 1
    assert isinstance(events[0], UserText)
    assert events[0].text == "after clear"


def test_reader_reset(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    _write_lines(
        f,
        [
            {
                "type": "user",
                "uuid": "u",
                "timestamp": "t",
                "message": {"role": "user", "content": "msg"},
            }
        ],
    )
    reader = TranscriptReader(path=f)
    reader.read_new()
    assert reader.events_emitted == 1
    reader.reset()
    assert reader.byte_offset == 0
    assert reader.events_emitted == 0
    again = reader.read_new()
    assert len(again) == 1


def test_reader_skips_invalid_json_lines(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    valid = json.dumps(
        {
            "type": "user",
            "uuid": "u",
            "timestamp": "t",
            "message": {"role": "user", "content": "ok"},
        }
    )
    f.write_text("garbage\n" + valid + "\n{not json\n")
    reader = TranscriptReader(path=f)
    events = reader.read_new()
    # Only the middle (valid) line parses to an event
    assert len(events) == 1
    assert isinstance(events[0], UserText)
    assert events[0].text == "ok"
