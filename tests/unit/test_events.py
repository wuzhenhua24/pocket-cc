"""Unit tests for claude/events.py — parse, write, EventsReader."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pocket_cc.claude.events import (
    EventsReader,
    HookEvent,
    parse_line,
    write_event,
)

if TYPE_CHECKING:
    from pathlib import Path


# ----------------------------------------------------------------- parse_line


def test_parse_line_valid_full_event() -> None:
    line = json.dumps(
        {
            "event": "Stop",
            "timestamp": 1234.5,
            "session_id": "uuid-1",
            "transcript_path": "/path.jsonl",
            "cwd": "/cwd",
            "raw": {"foo": "bar"},
        }
    )
    ev = parse_line(line)
    assert ev is not None
    assert ev.event == "Stop"
    assert ev.timestamp == 1234.5
    assert ev.session_id == "uuid-1"
    assert ev.transcript_path == "/path.jsonl"
    assert ev.cwd == "/cwd"
    assert ev.raw == {"foo": "bar"}


def test_parse_line_accepts_bytes() -> None:
    line = json.dumps({"event": "Stop"}).encode()
    ev = parse_line(line)
    assert ev is not None
    assert ev.event == "Stop"


def test_parse_line_missing_event_returns_none() -> None:
    assert parse_line('{"timestamp": 1.0}') is None


def test_parse_line_empty_returns_none() -> None:
    assert parse_line("") is None
    assert parse_line("   ") is None
    assert parse_line(b"") is None


def test_parse_line_invalid_json_returns_none() -> None:
    assert parse_line("{not json") is None
    assert parse_line("[1,2]") is None  # JSON but not a dict
    assert parse_line("42") is None


def test_parse_line_invalid_utf8_returns_none() -> None:
    assert parse_line(b"\xff\xfe") is None


def test_parse_line_non_dict_raw_falls_back_to_empty() -> None:
    line = json.dumps({"event": "Stop", "raw": "not-a-dict"})
    ev = parse_line(line)
    assert ev is not None
    assert ev.raw == {}


def test_parse_line_timestamp_string_coerced() -> None:
    line = json.dumps({"event": "Stop", "timestamp": "1.5"})
    ev = parse_line(line)
    assert ev is not None
    assert ev.timestamp == 1.5


def test_parse_line_timestamp_garbage_defaults_to_zero() -> None:
    line = json.dumps({"event": "Stop", "timestamp": "garbage"})
    ev = parse_line(line)
    assert ev is not None
    assert ev.timestamp == 0.0


# ----------------------------------------------------------------- write_event


def test_write_event_appends_one_line(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    write_event(log, "Stop", {"session_id": "s", "transcript_path": "/p", "cwd": "/c"})
    assert log.exists()
    content = log.read_text()
    assert content.endswith("\n")
    records = [json.loads(line) for line in content.splitlines() if line]
    assert len(records) == 1
    r = records[0]
    assert r["event"] == "Stop"
    assert r["session_id"] == "s"
    assert r["transcript_path"] == "/p"
    assert r["cwd"] == "/c"
    assert "timestamp" in r
    assert r["raw"] == {"session_id": "s", "transcript_path": "/p", "cwd": "/c"}


def test_write_event_creates_parent_dir(tmp_path: Path) -> None:
    log = tmp_path / "nested" / "deep" / "events.jsonl"
    write_event(log, "Stop", {"session_id": "s"})
    assert log.exists()


def test_write_event_multiple_appends(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    for i in range(3):
        write_event(log, "Stop", {"session_id": f"s{i}"})
    records = [json.loads(line) for line in log.read_text().splitlines() if line]
    assert len(records) == 3
    assert [r["session_id"] for r in records] == ["s0", "s1", "s2"]


def test_write_event_handles_unicode(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    write_event(log, "Stop", {"cwd": "/项目/路径"})
    r = json.loads(log.read_text())
    assert r["cwd"] == "/项目/路径"


# ----------------------------------------------------------------- EventsReader


def test_reader_missing_file_returns_empty(tmp_path: Path) -> None:
    reader = EventsReader(path=tmp_path / "nope.jsonl")
    assert reader.read_new() == []


def test_reader_reads_all_then_nothing(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    for i in range(3):
        write_event(log, "Stop", {"session_id": f"s{i}"})

    reader = EventsReader(path=log)
    events = reader.read_new()
    assert len(events) == 3
    assert all(isinstance(e, HookEvent) for e in events)
    assert reader.events_emitted == 3
    assert reader.read_new() == []


def test_reader_seek_to_end_skips_existing(tmp_path: Path) -> None:
    """seek_to_end should leave preexisting events unread."""
    log = tmp_path / "events.jsonl"
    for i in range(5):
        write_event(log, "Stop", {"session_id": f"old{i}"})

    reader = EventsReader(path=log)
    reader.seek_to_end()
    assert reader.read_new() == []

    # New events arriving *after* seek should be picked up
    write_event(log, "Stop", {"session_id": "fresh"})
    fresh = reader.read_new()
    assert len(fresh) == 1
    assert fresh[0].session_id == "fresh"


def test_reader_seek_to_end_on_missing_file(tmp_path: Path) -> None:
    reader = EventsReader(path=tmp_path / "nope.jsonl")
    reader.seek_to_end()
    assert reader.byte_offset == 0


def test_reader_incremental_append(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    write_event(log, "Stop", {"session_id": "a"})
    reader = EventsReader(path=log)
    first = reader.read_new()
    assert len(first) == 1

    write_event(log, "Stop", {"session_id": "b"})
    second = reader.read_new()
    assert len(second) == 1
    assert second[0].session_id == "b"


def test_reader_handles_partial_line(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    write_event(log, "Stop", {"session_id": "complete"})
    # Manually append a partial line (no trailing \n)
    with log.open("a") as fp:
        fp.write('{"event": "Stop", "sessio')

    reader = EventsReader(path=log)
    events = reader.read_new()
    # Only the complete line consumed
    assert len(events) == 1
    assert events[0].session_id == "complete"

    # Complete the partial line
    with log.open("a") as fp:
        fp.write('n_id": "newly-complete"}\n')
    events2 = reader.read_new()
    assert len(events2) == 1
    assert events2[0].session_id == "newly-complete"


def test_reader_handles_truncation(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    for i in range(5):
        write_event(log, "Stop", {"session_id": f"s{i}"})

    reader = EventsReader(path=log)
    assert len(reader.read_new()) == 5

    # Simulate log rotation: file shrunk
    write_event(log.with_suffix(".jsonl.bak"), "Stop", {"session_id": "moved"})  # just to exist
    log.unlink()
    write_event(log, "Stop", {"session_id": "after-rotation"})

    events = reader.read_new()
    assert len(events) == 1
    assert events[0].session_id == "after-rotation"


def test_reader_skips_invalid_lines(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text(
        "garbage line\n" + json.dumps({"event": "Stop", "session_id": "valid"}) + "\n{not json\n"
    )
    reader = EventsReader(path=log)
    events = reader.read_new()
    assert len(events) == 1
    assert events[0].session_id == "valid"
