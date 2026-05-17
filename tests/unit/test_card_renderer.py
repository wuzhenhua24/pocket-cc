"""Unit tests for relay/card_renderer.py — accumulator + render shape."""

from __future__ import annotations

from pocket_cc.claude.transcript import (
    AssistantText,
    AssistantThinking,
    ToolResult,
    ToolUse,
    UserText,
)
from pocket_cc.relay.card_renderer import (
    TurnAccumulator,
    format_tool_call,
    render_card,
)


def test_accumulator_records_user_prompt_once() -> None:
    acc = TurnAccumulator()
    acc.ingest(UserText(uuid="u1", timestamp="t", text="first"))
    acc.ingest(UserText(uuid="u2", timestamp="t", text="second"))
    snap = acc.snapshot()
    assert snap.user_prompt == "first", "user_prompt should be first user message only"


def test_accumulator_collects_assistant_text_and_tools() -> None:
    acc = TurnAccumulator()
    acc.ingest(UserText(uuid="u", timestamp="t", text="please run X"))
    acc.ingest(AssistantText(uuid="a1", timestamp="t", text="ok, reading file"))
    acc.ingest(
        ToolUse(
            uuid="a2",
            timestamp="t",
            tool_use_id="t1",
            tool_name="Read",
            tool_input={"file_path": "/tmp/foo.py"},
        )
    )
    acc.ingest(
        ToolResult(
            uuid="u2",
            timestamp="t",
            tool_use_id="t1",
            content="contents",
            is_error=False,
        )
    )
    acc.ingest(AssistantText(uuid="a3", timestamp="t", text="done"))
    snap = acc.snapshot()
    assert snap.user_prompt == "please run X"
    assert snap.assistant_text == "ok, reading file\n\ndone"
    assert len(snap.tool_calls) == 1
    assert "Read" in snap.tool_calls[0]
    assert "foo.py" in snap.tool_calls[0]


def test_accumulator_thinking_collected() -> None:
    acc = TurnAccumulator()
    acc.ingest(AssistantThinking(uuid="a", timestamp="t", text="hmm let me think"))
    acc.ingest(AssistantThinking(uuid="b", timestamp="t", text="ok got it"))
    assert acc.snapshot().thinking == "hmm let me think\n\nok got it"


def test_format_tool_call_known_tools() -> None:
    assert "Read" in format_tool_call("Read", {"file_path": "/a/b/foo.py"})
    assert "foo.py" in format_tool_call("Read", {"file_path": "/a/b/foo.py"})
    assert "Write" in format_tool_call("Write", {"file_path": "/a/b/bar.py"})
    assert "Edit" in format_tool_call("Edit", {"file_path": "/a/b/bar.py"})
    assert "ls -la" in format_tool_call("Bash", {"command": "ls -la"})
    assert "pattern" in format_tool_call("Grep", {"pattern": "pattern"})
    assert "foo*" in format_tool_call("Glob", {"pattern": "foo*"})
    assert "https://" in format_tool_call("WebFetch", {"url": "https://example.com"})
    assert "query" in format_tool_call("WebSearch", {"query": "query"})


def test_format_tool_call_unknown_falls_back_to_name() -> None:
    s = format_tool_call("WeirdNewTool", {"foo": "bar"})
    assert "WeirdNewTool" in s


def test_format_tool_call_bash_long_command_truncated() -> None:
    s = format_tool_call("Bash", {"command": "echo " + "X" * 200})
    assert len(s) < 150  # bounded


def test_render_card_running_state_has_blue_header_and_actions() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "fix the bug"
    card = render_card(acc.snapshot(state="running"))
    assert card["header"]["template"] == "blue"
    assert "fix the bug" in card["header"]["title"]["content"]
    # running cards have an action row
    tags = [e.get("tag") for e in card["elements"]]
    assert "action" in tags


def test_render_card_done_state_no_actions() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    card = render_card(acc.snapshot(state="done"))
    assert card["header"]["template"] == "green"
    assert "action" not in [e.get("tag") for e in card["elements"]]


def test_render_card_failed_includes_error_in_body() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    snap = acc.snapshot(state="failed", error="tmux not found")
    card = render_card(snap)
    assert card["header"]["template"] == "red"
    body = card["elements"][0]["content"]
    assert "tmux not found" in body


def test_render_card_empty_running_shows_placeholder() -> None:
    acc = TurnAccumulator()
    card = render_card(acc.snapshot(state="running"))
    body = card["elements"][0]["content"]
    assert "运行中" in body


def test_render_card_thinking_renders_as_detail_section() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantThinking(uuid="a", timestamp="t", text="secret thoughts"))
    card = render_card(acc.snapshot(state="running"))
    tags = [e.get("tag") for e in card["elements"]]
    # body, hr, note, markdown (detail), hr, action — depending on state
    assert "note" in tags
    note_idx = tags.index("note")
    note = card["elements"][note_idx]
    assert "思考链" in note["elements"][0]["content"]
    # the actual thinking content follows in the next markdown block
    assert card["elements"][note_idx + 1]["tag"] == "markdown"
    assert "secret thoughts" in card["elements"][note_idx + 1]["content"]


def test_render_card_body_truncated_when_huge() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="A" * 5000))
    card = render_card(acc.snapshot(state="running"))
    body = card["elements"][0]["content"]
    assert len(body) <= 3050  # ~_BODY_MAX_CHARS + truncation hint
    assert "截断" in body


def test_render_card_title_shortened_for_long_prompt() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "a very long prompt " * 20
    card = render_card(acc.snapshot(state="running"))
    title = card["header"]["title"]["content"]
    # emoji + space + title; title body itself bounded to ~60 chars
    assert len(title) < 80
