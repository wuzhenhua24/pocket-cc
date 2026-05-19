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
    should_rotate,
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


# ============================================================== waiting (M2-0)

# Local imports kept inside the test section so the existing tests above remain
# unaffected by any refactor of the waiting module location.

from pocket_cc.relay.waiting import (  # noqa: E402 — keep waiting tests grouped
    KeysResponse,
    TextResponse,
    WaitingFor,
    WaitingOption,
)


def _waiting(question: str = "Pick one", n: int = 2) -> WaitingFor:
    return WaitingFor(
        source="permission",
        question=question,
        options=tuple(
            WaitingOption(label=f"option-{i}", response=TextResponse(text=str(i + 1)))
            for i in range(n)
        ),
    )


def test_waiting_card_uses_orange_header_and_question_in_body() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "run dangerous"
    card = render_card(acc.snapshot(state="waiting", waiting_for=_waiting(question="Proceed?")))
    assert card["header"]["template"] == "orange"
    assert "❓" in card["header"]["title"]["content"]
    body = card["elements"][0]["content"]
    assert "Proceed?" in body


def test_waiting_card_lists_options_numbered_in_body() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    card = render_card(acc.snapshot(state="waiting", waiting_for=_waiting(n=3)))
    body = card["elements"][0]["content"]
    assert "**1.** option-0" in body
    assert "**2.** option-1" in body
    assert "**3.** option-2" in body


def test_waiting_card_option_descriptions_appear() -> None:
    waiting = WaitingFor(
        source="ask_user_question",
        question="Network?",
        options=(
            WaitingOption(
                label="Public",
                response=TextResponse(text="1"),
                description="Outbound to feishu.cn OK",
            ),
        ),
    )
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    card = render_card(acc.snapshot(state="waiting", waiting_for=waiting))
    body = card["elements"][0]["content"]
    assert "Outbound to feishu.cn OK" in body


def test_waiting_card_buttons_cap_options_at_4() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    card = render_card(acc.snapshot(state="waiting", waiting_for=_waiting(n=10)))
    action_row = card["elements"][-1]
    assert action_row["tag"] == "action"
    buttons = action_row["actions"]
    # 4 option buttons + cancel + esc
    assert len(buttons) == 6
    # First 4 are options
    option_btns = buttons[:4]
    for i, btn in enumerate(option_btns):
        assert btn["value"]["action"] == "waiting_response"
        assert btn["value"]["index"] == i


def test_waiting_card_first_option_is_primary() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    card = render_card(acc.snapshot(state="waiting", waiting_for=_waiting(n=3)))
    buttons = card["elements"][-1]["actions"]
    assert buttons[0]["type"] == "primary"
    assert buttons[1]["type"] == "default"
    assert buttons[2]["type"] == "default"


def test_waiting_card_always_has_cancel_and_esc() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    card = render_card(acc.snapshot(state="waiting", waiting_for=_waiting(n=1)))
    buttons = card["elements"][-1]["actions"]
    cancel = [b for b in buttons if b["value"].get("action") == "cancel"]
    # Esc button uses key_sequence (double-Escape to avoid Lark's
    # "操作太频繁" rate limit on consecutive single-Esc taps)
    esc = [b for b in buttons if b["value"].get("action") == "key_sequence"]
    assert len(cancel) == 1
    assert cancel[0]["type"] == "danger"
    assert len(esc) == 1
    assert esc[0]["value"]["keys"] == ["Escape", "Escape"]


def test_waiting_card_truncates_long_button_label() -> None:
    long_label = "X" * 200
    waiting = WaitingFor(
        source="permission",
        question="q",
        options=(WaitingOption(label=long_label, response=TextResponse(text="1")),),
    )
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    card = render_card(acc.snapshot(state="waiting", waiting_for=waiting))
    first_button = card["elements"][-1]["actions"][0]
    assert len(first_button["text"]["content"]) <= 24


def test_waiting_card_no_options_shows_placeholder_body() -> None:
    waiting = WaitingFor(source="permission", question="")
    acc = TurnAccumulator()
    card = render_card(acc.snapshot(state="waiting", waiting_for=waiting))
    body = card["elements"][0]["content"]
    assert body == "_（Claude 在等你响应）_"


def test_waiting_card_keeps_assistant_text_context() -> None:
    """If Claude said something before the prompt fired, surface it."""
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="I'm about to run rm"))
    card = render_card(acc.snapshot(state="waiting", waiting_for=_waiting(n=2)))
    body = card["elements"][0]["content"]
    assert "I'm about to run rm" in body


def test_waiting_option_can_carry_keys_response() -> None:
    """Renderer doesn't care about the response shape (that's for input.py)."""
    waiting = WaitingFor(
        source="ask_user_question",
        question="Pick row",
        options=(WaitingOption(label="Top", response=KeysResponse(keys=("Up", "Enter"))),),
    )
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    card = render_card(acc.snapshot(state="waiting", waiting_for=waiting))
    # Just verify it renders and includes the option label
    buttons = card["elements"][-1]["actions"]
    assert "Top" in buttons[0]["text"]["content"]


# ============================================================ rotation (M2-F)


def test_should_rotate_false_for_short_body() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="short"))
    assert should_rotate(acc.snapshot(state="running")) is False


def test_should_rotate_true_for_long_body() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="A" * 3500))
    assert should_rotate(acc.snapshot(state="running")) is True


def test_should_rotate_false_when_waiting() -> None:
    """Waiting cards never rotate — option buttons stay on the current card."""
    from pocket_cc.relay.waiting import (
        TextResponse,
        WaitingFor,
        WaitingOption,
    )

    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="A" * 5000))
    waiting = WaitingFor(
        source="permission",
        question="q",
        options=(WaitingOption(label="Yes", response=TextResponse(text="1")),),
    )
    snap = acc.snapshot(state="waiting", waiting_for=waiting)
    assert should_rotate(snap) is False


def test_accumulator_commit_starts_from_committed_fresh() -> None:
    """After commit(), from_committed=True hides what was committed."""
    acc = TurnAccumulator()
    acc.ingest(UserText(uuid="u", timestamp="t", text="ask"))
    acc.ingest(AssistantText(uuid="a1", timestamp="t", text="first chunk"))
    acc.commit()
    acc.ingest(AssistantText(uuid="a2", timestamp="t", text="second chunk"))

    fresh = acc.snapshot(state="running", from_committed=True)
    full = acc.snapshot(state="running", from_committed=False)

    # Committed view sees only the post-commit text
    assert "second chunk" in fresh.assistant_text
    assert "first chunk" not in fresh.assistant_text
    # Full view still has everything
    assert "first chunk" in full.assistant_text
    assert "second chunk" in full.assistant_text


def test_accumulator_commit_also_partitions_tool_calls() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(
        ToolUse(
            uuid="t1",
            timestamp="t",
            tool_use_id="u1",
            tool_name="Read",
            tool_input={"file_path": "/a/b/before.py"},
        )
    )
    acc.commit()
    acc.ingest(
        ToolUse(
            uuid="t2",
            timestamp="t",
            tool_use_id="u2",
            tool_name="Read",
            tool_input={"file_path": "/a/b/after.py"},
        )
    )

    fresh = acc.snapshot(state="running", from_committed=True)
    assert len(fresh.tool_calls) == 1
    assert "after.py" in fresh.tool_calls[0]
    full = acc.snapshot(state="running", from_committed=False)
    assert len(full.tool_calls) == 2


def test_accumulator_commit_partitions_thinking() -> None:
    acc = TurnAccumulator()
    acc.ingest(AssistantThinking(uuid="th1", timestamp="t", text="early thoughts"))
    acc.commit()
    acc.ingest(AssistantThinking(uuid="th2", timestamp="t", text="later thoughts"))

    fresh = acc.snapshot(state="running", from_committed=True)
    assert "early thoughts" not in fresh.thinking
    assert "later thoughts" in fresh.thinking


def test_render_card_continuation_title_has_prefix() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "原始提问"
    card = render_card(acc.snapshot(state="running"), is_continuation=True)
    title = card["header"]["title"]["content"]
    assert "(续)" in title
    assert "原始提问" in title


def test_render_card_with_continuation_marker_appends_footer() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="some content"))
    card = render_card(
        acc.snapshot(state="running"),
        ends_with_continuation_marker=True,
    )
    body = card["elements"][0]["content"]
    assert "续下条" in body
    assert "⏬" in body


def test_render_card_no_continuation_marker_by_default() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="some content"))
    card = render_card(acc.snapshot(state="running"))
    body = card["elements"][0]["content"]
    assert "续下条" not in body


def test_rotation_dataflow_end_to_end() -> None:
    """Simulate the bootstrap rotation flow against the accumulator/renderer.

    1. Ingest enough to trigger rotation.
    2. Render the "sealing" card (with continuation marker).
    3. commit() the accumulator.
    4. Render the "starter" card (continuation prefix, ~empty body).
    5. Ingest more content; new card body should only contain *new* content.
    """
    acc = TurnAccumulator()
    acc.user_prompt = "long task"
    acc.ingest(AssistantText(uuid="a1", timestamp="t", text="A" * 3000))

    # Step 1: confirm rotation needed
    pre_snap = acc.snapshot(state="running", from_committed=True)
    assert should_rotate(pre_snap)

    # Step 2: render sealing card
    sealing = render_card(pre_snap, ends_with_continuation_marker=True)
    assert "续下条" in sealing["elements"][0]["content"]

    # Step 3: commit
    acc.commit()
    starter_snap = acc.snapshot(state="running", from_committed=True)
    assert starter_snap.assistant_text == ""
    assert should_rotate(starter_snap) is False

    # Step 4: render continuation starter
    starter_card = render_card(starter_snap, is_continuation=True)
    assert "(续)" in starter_card["header"]["title"]["content"]

    # Step 5: new content shows only on the new card
    acc.ingest(AssistantText(uuid="a2", timestamp="t", text="brand new content"))
    new_snap = acc.snapshot(state="running", from_committed=True)
    assert "brand new content" in new_snap.assistant_text
    assert "A" * 100 not in new_snap.assistant_text  # not in committed view


# ============================================================ chunked rotation


def test_find_fit_window_returns_full_window_when_content_fits() -> None:
    """When uncommitted content already fits within max_chars, no shrinking."""
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="short content"))
    acc.ingest(
        ToolUse(
            uuid="t1",
            timestamp="t",
            tool_use_id="u1",
            tool_name="Read",
            tool_input={"file_path": "/x/y/a.py"},
        )
    )
    text_end, tool_end, thinking_end = acc.find_fit_window(max_chars=2500)
    assert text_end == 1
    assert tool_end == 1
    assert thinking_end == 0


def test_find_fit_window_shrinks_tool_calls_first() -> None:
    """When body is over budget, trim tool_calls before assistant_text —
    text renders at the top of the card, so the user sees the *earliest*
    content (= assistant_text) in the sealed card and the *later* content
    (= tool_calls) on the next card. Trimming text-first would invert that."""
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    # One small assistant_text + many tool_calls; aggregate body > 200 chars
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="hi"))
    for i in range(30):
        acc.ingest(
            ToolUse(
                uuid=f"t{i}",
                timestamp="t",
                tool_use_id=f"u{i}",
                tool_name="Read",
                tool_input={"file_path": f"/some/path/file_with_long_name_{i}.py"},
            )
        )
    text_end, tool_end, _ = acc.find_fit_window(max_chars=200)
    # Assistant text kept; tool_calls shrunk
    assert text_end == 1
    assert tool_end < 30


def test_find_fit_window_forces_progress_on_oversize_single_part() -> None:
    """When even one leading part exceeds max_chars, include it anyway —
    the body-level truncation handles the oversized single-part case, so
    rotation never gets stuck reporting "zero progress" indefinitely."""
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    # One huge assistant_text (way over budget)
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="A" * 5000))
    text_end, tool_end, thinking_end = acc.find_fit_window(max_chars=500)
    # Forced inclusion of 1 leading text part — guarantees forward progress
    assert text_end == 1
    assert tool_end == 0
    assert thinking_end == 0


def test_commit_to_advances_only_to_specified_indices() -> None:
    """Partial commit lets chunked rotation seal a prefix and leave the rest."""
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a1", timestamp="t", text="first"))
    acc.ingest(AssistantText(uuid="a2", timestamp="t", text="second"))
    acc.ingest(AssistantText(uuid="a3", timestamp="t", text="third"))

    acc.commit_to(text_end=2, tool_end=0, thinking_end=0)

    leftover = acc.snapshot(state="running", from_committed=True)
    assert "first" not in leftover.assistant_text
    assert "second" not in leftover.assistant_text
    assert "third" in leftover.assistant_text


def test_snapshot_window_returns_only_committed_to_end_slice() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a1", timestamp="t", text="first"))
    acc.ingest(AssistantText(uuid="a2", timestamp="t", text="second"))
    acc.ingest(AssistantText(uuid="a3", timestamp="t", text="third"))

    snap = acc.snapshot_window(text_end=2, tool_end=0, thinking_end=0)
    assert "first" in snap.assistant_text
    assert "second" in snap.assistant_text
    assert "third" not in snap.assistant_text


def test_chunked_rotation_no_truncation_message_when_split() -> None:
    """End-to-end: a single big batch that would have shown the
    "已截断早期内容" message under the old rotation logic should now
    split cleanly across multiple sealed cards with no truncation hint.
    """
    from pocket_cc.relay.card_renderer import ROTATE_AT_CHARS

    acc = TurnAccumulator()
    acc.user_prompt = "x"
    # Several medium-sized assistant text parts whose total exceeds the
    # rotate threshold but each one comfortably fits in a card on its own.
    for i in range(6):
        acc.ingest(AssistantText(uuid=f"a{i}", timestamp="t", text=f"part {i}: " + "A" * 800))

    # Simulate the bootstrap loop: rotate until content fits.
    sealed_cards: list[str] = []
    iterations = 0
    while iterations < 16:
        iterations += 1
        snap = acc.snapshot(state="running", from_committed=True)
        if not should_rotate(snap):
            break
        text_end, tool_end, thinking_end = acc.find_fit_window(ROTATE_AT_CHARS)
        seal_snap = acc.snapshot_window(
            text_end=text_end, tool_end=tool_end, thinking_end=thinking_end
        )
        sealed = render_card(seal_snap, ends_with_continuation_marker=True)
        sealed_cards.append(sealed["elements"][0]["content"])
        acc.commit_to(text_end=text_end, tool_end=tool_end, thinking_end=thinking_end)

    # Final (open) card should fit without rotation.
    final_card = render_card(
        acc.snapshot(state="running", from_committed=True), is_continuation=True
    )
    final_body = final_card["elements"][0]["content"]

    # Must have produced at least one sealed card (otherwise rotation didn't fire)
    assert sealed_cards, "expected at least one sealed card from chunked rotation"
    # The bug: under the old logic, the sealed card body included
    # "…(已截断早期内容)…" because the whole uncommitted slice was rendered
    # at once and tail-truncated. Chunked rotation must never show that.
    for body in sealed_cards:
        assert "截断" not in body, (
            f"sealed card should not contain truncation hint — content was split "
            f"across rotations, not truncated. Got:\n{body}"
        )
    assert "截断" not in final_body
