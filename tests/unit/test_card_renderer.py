"""Unit tests for relay/card_renderer.py — accumulator + render shape."""

from __future__ import annotations

from typing import Any

from pocket_cc.claude.transcript import (
    AssistantText,
    AssistantThinking,
    ModeChange,
    ToolResult,
    ToolUse,
    UserText,
)
from pocket_cc.relay.card_renderer import (
    TurnAccumulator,
    format_tool_call,
    mode_label,
    render_card,
    should_rotate,
)


def _action_buttons(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the flat list of button dicts from a v2 card's column_set row.

    Schema 2.0 has no ``action`` container — the action row is a
    ``column_set`` with one ``column`` per button. Tests don't care about
    the column wrapping; this flatten keeps assertions readable.
    """
    column_set = next(
        e for e in card["body"]["elements"] if e.get("tag") == "column_set"
    )
    return [col["elements"][0] for col in column_set["columns"]]


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


def test_format_tool_call_exit_plan_mode_has_dedicated_label() -> None:
    """ExitPlanMode gets its own one-liner — the actual plan body is rendered
    separately in the waiting card, so this line just records the call."""
    s = format_tool_call("ExitPlanMode", {"plan": "# Plan\n\nstuff"})
    assert "ExitPlanMode" in s
    assert "📋" in s
    # The plan content itself must NOT leak into the tool-call list (that's
    # the waiting card body's job).
    assert "Plan\n" not in s
    assert "stuff" not in s


def test_accumulator_captures_exit_plan_mode_payload() -> None:
    acc = TurnAccumulator()
    acc.ingest(
        ToolUse(
            uuid="p1",
            timestamp="t",
            tool_use_id="t-plan-1",
            tool_name="ExitPlanMode",
            tool_input={"plan": "# Refactor turn_controller\n\n- step 1\n- step 2"},
        )
    )
    snap = acc.snapshot()
    assert snap.latest_plan == "# Refactor turn_controller\n\n- step 1\n- step 2"
    # The one-line summary still shows up in tool_calls — the plan content is
    # additional, not a replacement.
    assert any("ExitPlanMode" in t for t in snap.tool_calls)


def test_accumulator_overwrites_plan_on_each_exit_plan_mode_call() -> None:
    """If Claude refines and re-emits the plan, the latest wins."""
    acc = TurnAccumulator()
    acc.ingest(
        ToolUse(
            uuid="p1",
            timestamp="t",
            tool_use_id="t1",
            tool_name="ExitPlanMode",
            tool_input={"plan": "old plan"},
        )
    )
    acc.ingest(
        ToolUse(
            uuid="p2",
            timestamp="t",
            tool_use_id="t2",
            tool_name="ExitPlanMode",
            tool_input={"plan": "refined plan"},
        )
    )
    assert acc.snapshot().latest_plan == "refined plan"


def test_accumulator_plan_field_empty_when_no_exit_plan_mode_seen() -> None:
    acc = TurnAccumulator()
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="just talking"))
    assert acc.snapshot().latest_plan == ""


def test_render_waiting_card_plan_source_renders_plan_above_options() -> None:
    """Plan card body must include the proposed plan markdown so the user
    can actually read what they're approving."""
    from pocket_cc.relay.waiting import TextResponse, WaitingFor, WaitingOption

    acc = TurnAccumulator()
    acc.ingest(UserText(uuid="u", timestamp="t", text="重构一下吧"))
    acc.ingest(
        ToolUse(
            uuid="p",
            timestamp="t",
            tool_use_id="t-p",
            tool_name="ExitPlanMode",
            tool_input={"plan": "# 重构方案\n\n- 拆 generation 出去\n- 加单测"},
        )
    )
    waiting = WaitingFor(
        source="plan",
        question="Claude has written up a plan and is ready to execute. Would you like to proceed?",
        options=(
            WaitingOption(label="Yes, auto-accept edits", response=TextResponse(text="1")),
            WaitingOption(label="Yes, manually approve edits", response=TextResponse(text="2")),
        ),
        fingerprint="fp",
    )
    card = render_card(acc.snapshot(state="waiting", waiting_for=waiting))
    body = card["body"]["elements"][0]["content"]
    # Plan markdown is in the body, above the option bullets.
    assert "重构方案" in body
    assert "拆 generation 出去" in body
    plan_idx = body.index("重构方案")
    options_idx = body.index("Yes, auto-accept edits")
    assert plan_idx < options_idx
    # Header is orange waiting state.
    assert card["header"]["template"] == "orange"


def test_render_waiting_card_permission_source_does_not_inject_plan() -> None:
    """A plan that arrived earlier in the turn (e.g. session was in plan
    mode, exited, then asked for permission) must not bleed into a
    permission-source waiting card."""
    from pocket_cc.relay.waiting import TextResponse, WaitingFor, WaitingOption

    acc = TurnAccumulator()
    # latest_plan is set from an earlier ExitPlanMode call…
    acc.ingest(
        ToolUse(
            uuid="p",
            timestamp="t",
            tool_use_id="t-p",
            tool_name="ExitPlanMode",
            tool_input={"plan": "stale plan from earlier"},
        )
    )
    waiting = WaitingFor(
        source="permission",
        question="Do you want to proceed?",
        options=(WaitingOption(label="Yes", response=TextResponse(text="1")),),
        fingerprint="fp-perm",
    )
    body = render_card(acc.snapshot(state="waiting", waiting_for=waiting))["body"]["elements"][0]["content"]
    # …but the permission card shouldn't surface it (it's not what this prompt
    # is about). Confirm with substring check.
    assert "stale plan from earlier" not in body


def test_accumulator_captures_ask_user_question_payload() -> None:
    """AskUserQuestion tool_use → structured AskUserQuestion list on accumulator."""
    acc = TurnAccumulator()
    acc.ingest(
        ToolUse(
            uuid="a1",
            timestamp="t",
            tool_use_id="t-ask-1",
            tool_name="AskUserQuestion",
            tool_input={
                "questions": [
                    {
                        "question": "日志来自哪里？",
                        "header": "日志来源",
                        "multiSelect": True,
                        "options": [
                            {"label": "本地文件", "description": "用 Read/Grep"},
                            {"label": "ELK", "description": "走 Elasticsearch API"},
                        ],
                    },
                    {
                        "question": "运行形态？",
                        "header": "运行形态",
                        "multiSelect": False,
                        "options": [{"label": "CI 一次性"}, {"label": "交互式"}],
                    },
                ]
            },
        )
    )
    snap = acc.snapshot()
    qs = snap.latest_ask_user_questions
    assert len(qs) == 2
    assert qs[0].header == "日志来源"
    assert qs[0].multi_select is True
    assert qs[0].options[0].label == "本地文件"
    assert qs[0].options[0].description == "用 Read/Grep"
    assert qs[1].multi_select is False
    # Optional `description` defaults to "" when absent.
    assert qs[1].options[0].description == ""
    # Tool-call one-liner shows up in the tool_calls list too.
    assert any("AskUserQuestion" in t for t in snap.tool_calls)


def test_accumulator_ask_user_questions_empty_for_malformed_payload() -> None:
    acc = TurnAccumulator()
    # No `questions` key at all.
    acc.ingest(
        ToolUse(
            uuid="a1",
            timestamp="t",
            tool_use_id="t-x",
            tool_name="AskUserQuestion",
            tool_input={},
        )
    )
    assert acc.snapshot().latest_ask_user_questions == ()


def test_format_tool_call_ask_user_question_has_dedicated_label() -> None:
    s = format_tool_call("AskUserQuestion", {"questions": []})
    assert "AskUserQuestion" in s
    assert "❓" in s


def test_render_waiting_card_ask_user_shows_question_header_and_options() -> None:
    """The ask_user waiting card body must include the question header,
    question text, all option labels + descriptions, and the multi-select
    hint when applicable."""
    from pocket_cc.relay.card_renderer import AskUserOption, AskUserQuestion
    from pocket_cc.relay.waiting import TextResponse, WaitingFor, WaitingOption

    acc = TurnAccumulator()
    # Inject the structured questions directly (mimics what _parse_…
    # produced from a real transcript).
    acc._latest_ask_user_questions = (
        AskUserQuestion(
            question="日志主要来自哪里？",
            header="日志来源",
            options=(
                AskUserOption(label="本地文件", description="用 Read/Grep 直接读"),
                AskUserOption(label="ELK", description="通过 Elasticsearch API"),
            ),
            multi_select=True,
        ),
    )
    waiting = WaitingFor(
        source="ask_user_question",
        question="日志主要来自哪里？",
        options=(
            WaitingOption(
                label="本地文件", description="用 Read/Grep 直接读", response=TextResponse(text="1")
            ),
            WaitingOption(
                label="ELK", description="通过 Elasticsearch API", response=TextResponse(text="2")
            ),
        ),
        fingerprint="fp",
    )
    body = render_card(acc.snapshot(state="waiting", waiting_for=waiting))["body"]["elements"][0]["content"]
    assert "日志来源" in body  # header
    assert "日志主要来自哪里" in body  # question text
    assert "多选" in body  # multi-select hint
    assert "本地文件" in body and "用 Read/Grep 直接读" in body
    assert "ELK" in body and "通过 Elasticsearch API" in body


def test_render_waiting_card_ask_user_surfaces_remaining_questions_hint() -> None:
    """When there are multiple questions, the body must tell the user how
    many more remain (so they know the buttons only act on Q1)."""
    from pocket_cc.relay.card_renderer import AskUserOption, AskUserQuestion
    from pocket_cc.relay.waiting import TextResponse, WaitingFor, WaitingOption

    acc = TurnAccumulator()
    acc._latest_ask_user_questions = (
        AskUserQuestion(
            question="Q1?",
            header="日志来源",
            options=(AskUserOption(label="A"), AskUserOption(label="B")),
        ),
        AskUserQuestion(question="Q2?", header="运行形态", options=()),
        AskUserQuestion(question="Q3?", header="分析目标", options=()),
    )
    waiting = WaitingFor(
        source="ask_user_question",
        question="Q1?",
        options=(WaitingOption(label="A", response=TextResponse(text="1")),),
        fingerprint="fp",
    )
    body = render_card(acc.snapshot(state="waiting", waiting_for=waiting))["body"]["elements"][0]["content"]
    assert "还问了 2 个问题" in body
    assert "运行形态" in body
    assert "分析目标" in body


def test_render_waiting_card_ask_user_single_question_no_remaining_hint() -> None:
    """One question → no "还有 N 题" line (would be misleading)."""
    from pocket_cc.relay.card_renderer import AskUserOption, AskUserQuestion
    from pocket_cc.relay.waiting import TextResponse, WaitingFor, WaitingOption

    acc = TurnAccumulator()
    acc._latest_ask_user_questions = (
        AskUserQuestion(
            question="only one?",
            header="only",
            options=(AskUserOption(label="A"),),
        ),
    )
    waiting = WaitingFor(
        source="ask_user_question",
        question="only one?",
        options=(WaitingOption(label="A", response=TextResponse(text="1")),),
        fingerprint="fp",
    )
    body = render_card(acc.snapshot(state="waiting", waiting_for=waiting))["body"]["elements"][0]["content"]
    assert "还问了" not in body


def test_render_waiting_card_ask_user_placeholder_when_accumulator_empty() -> None:
    """If pane saw the widget but transcript hasn't ingested the tool_use yet,
    we render a graceful "loading" message rather than crashing."""
    from pocket_cc.relay.waiting import WaitingFor

    acc = TurnAccumulator()
    waiting = WaitingFor(
        source="ask_user_question",
        question="Claude 在向你提问，正在加载选项…",
        options=(),
        fingerprint="fp",
    )
    body = render_card(acc.snapshot(state="waiting", waiting_for=waiting))["body"]["elements"][0]["content"]
    # Doesn't crash; the placeholder question text appears.
    assert "Claude" in body


def test_render_waiting_card_plan_source_empty_plan_gracefully_omits_section() -> None:
    """Defensive: if somehow the waiting state flips to "plan" without a
    plan body in the accumulator (e.g. transcript lag), don't crash and
    don't render an empty "📋 方案" header."""
    from pocket_cc.relay.waiting import TextResponse, WaitingFor, WaitingOption

    acc = TurnAccumulator()
    waiting = WaitingFor(
        source="plan",
        question="Claude has written up a plan…",
        options=(WaitingOption(label="Yes", response=TextResponse(text="1")),),
        fingerprint="fp",
    )
    body = render_card(acc.snapshot(state="waiting", waiting_for=waiting))["body"]["elements"][0]["content"]
    assert "📋 方案" not in body  # no empty heading
    assert "Yes" in body  # options still render


def test_render_card_running_state_has_blue_header_and_actions() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "fix the bug"
    card = render_card(acc.snapshot(state="running"))
    assert card["header"]["template"] == "blue"
    assert "fix the bug" in card["header"]["title"]["content"]
    # running cards have an action row — v2 buttons live in a column_set.
    tags = [e.get("tag") for e in card["body"]["elements"]]
    assert "column_set" in tags


def test_render_card_done_state_no_actions() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    card = render_card(acc.snapshot(state="done"))
    assert card["header"]["template"] == "green"
    assert "column_set" not in [e.get("tag") for e in card["body"]["elements"]]


def test_render_card_running_keeps_tool_calls() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="working on it"))
    acc.ingest(
        ToolUse(
            uuid="b",
            timestamp="t",
            tool_use_id="t1",
            tool_name="Read",
            tool_input={"file_path": "foo.py"},
        )
    )
    body = render_card(acc.snapshot(state="running"))["body"]["elements"][0]["content"]
    assert "工具调用" in body
    assert "foo.py" in body


def test_render_card_done_drops_tool_calls_when_text_present() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="here is the answer"))
    acc.ingest(
        ToolUse(
            uuid="b",
            timestamp="t",
            tool_use_id="t1",
            tool_name="Read",
            tool_input={"file_path": "foo.py"},
        )
    )
    body = render_card(acc.snapshot(state="done"))["body"]["elements"][0]["content"]
    assert "here is the answer" in body
    assert "工具调用" not in body
    assert "foo.py" not in body


def test_render_card_done_keeps_tool_calls_as_fallback_when_no_text() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(
        ToolUse(
            uuid="b",
            timestamp="t",
            tool_use_id="t1",
            tool_name="Bash",
            tool_input={"command": "pytest"},
        )
    )
    body = render_card(acc.snapshot(state="done"))["body"]["elements"][0]["content"]
    # No assistant text → keep tool calls so the body isn't just a placeholder.
    assert "工具调用" in body
    assert "pytest" in body


def test_render_card_failed_drops_tool_calls() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(
        ToolUse(
            uuid="b",
            timestamp="t",
            tool_use_id="t1",
            tool_name="Read",
            tool_input={"file_path": "foo.py"},
        )
    )
    body = render_card(acc.snapshot(state="failed", error="boom"))["body"]["elements"][0]["content"]
    assert "boom" in body
    assert "工具调用" not in body


def test_render_card_failed_includes_error_in_body() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    snap = acc.snapshot(state="failed", error="tmux not found")
    card = render_card(snap)
    assert card["header"]["template"] == "red"
    body = card["body"]["elements"][0]["content"]
    assert "tmux not found" in body


def test_render_card_empty_running_shows_placeholder() -> None:
    acc = TurnAccumulator()
    card = render_card(acc.snapshot(state="running"))
    body = card["body"]["elements"][0]["content"]
    assert "运行中" in body


def test_render_card_thinking_renders_as_detail_section_when_enabled() -> None:
    from pocket_cc.lark.card import ELEMENT_ID_DETAIL_CONTENT, ELEMENT_ID_DETAIL_LABEL

    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantThinking(uuid="a", timestamp="t", text="secret thoughts"))
    card = render_card(acc.snapshot(state="running"), show_thinking=True)
    body_els = card["body"]["elements"]
    # v2 detail section: body, detail_divider(hr), detail_label(markdown grey),
    # detail_content(markdown), actions_divider(hr), column_set
    label_idx = next(
        i for i, e in enumerate(body_els) if e.get("element_id") == ELEMENT_ID_DETAIL_LABEL
    )
    label_el = body_els[label_idx]
    assert label_el["tag"] == "markdown"
    assert "思考链" in label_el["content"]
    content_el = body_els[label_idx + 1]
    assert content_el["element_id"] == ELEMENT_ID_DETAIL_CONTENT
    assert content_el["tag"] == "markdown"
    assert "secret thoughts" in content_el["content"]


def test_render_card_body_is_normalized_before_v2_builder() -> None:
    """v2's ``markdown`` element still drops GFM pipe-tables and (pending
    verification) renders raw ``##`` literally. The renderer is responsible
    for calling normalize_markdown_for_lark on body content so the user
    sees ``**Heading**`` (and bullet-listed tables) instead of raw markup.
    Regression guard for the legacy → cardkit migration."""
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="## Result\nAll good."))
    card = render_card(acc.snapshot(state="running"))
    body_content = card["body"]["elements"][0]["content"]
    assert "**Result**" in body_content
    assert "## " not in body_content


def test_render_card_detail_is_normalized_before_v2_builder() -> None:
    """Same contract as body: detail (thinking) content must be normalized
    before the v2 builder embeds it. The v2 builder is intentionally a
    no-op on its content fields (see test_v2_does_not_pre_normalize_…)."""
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantThinking(uuid="a", timestamp="t", text="## Inner\nthought"))
    card = render_card(acc.snapshot(state="running"), show_thinking=True)
    from pocket_cc.lark.card import ELEMENT_ID_DETAIL_CONTENT

    content_el = next(
        e for e in card["body"]["elements"] if e.get("element_id") == ELEMENT_ID_DETAIL_CONTENT
    )
    assert "**Inner**" in content_el["content"]
    assert "## " not in content_el["content"]


def test_render_card_thinking_hidden_by_default() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantThinking(uuid="a", timestamp="t", text="secret thoughts"))
    card = render_card(acc.snapshot(state="running"))
    blob = str(card)
    assert "思考链" not in blob
    assert "secret thoughts" not in blob


def test_render_card_body_truncated_when_huge() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="A" * 5000))
    card = render_card(acc.snapshot(state="running"))
    body = card["body"]["elements"][0]["content"]
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
    body = card["body"]["elements"][0]["content"]
    assert "Proceed?" in body


def test_waiting_card_lists_options_numbered_in_body() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    card = render_card(acc.snapshot(state="waiting", waiting_for=_waiting(n=3)))
    body = card["body"]["elements"][0]["content"]
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
    body = card["body"]["elements"][0]["content"]
    assert "Outbound to feishu.cn OK" in body


def test_waiting_card_buttons_cap_options_at_4() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    card = render_card(acc.snapshot(state="waiting", waiting_for=_waiting(n=10)))
    buttons = _action_buttons(card)
    # 4 option buttons + cancel + esc
    assert len(buttons) == 6
    # First 4 are options
    for i, btn in enumerate(buttons[:4]):
        assert btn["value"]["action"] == "waiting_response"
        assert btn["value"]["index"] == i


def test_waiting_card_first_option_is_primary() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    card = render_card(acc.snapshot(state="waiting", waiting_for=_waiting(n=3)))
    buttons = _action_buttons(card)
    assert buttons[0]["type"] == "primary"
    assert buttons[1]["type"] == "default"
    assert buttons[2]["type"] == "default"


def test_waiting_card_always_has_cancel_and_esc() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    card = render_card(acc.snapshot(state="waiting", waiting_for=_waiting(n=1)))
    buttons = _action_buttons(card)
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
    first_button = _action_buttons(card)[0]
    assert len(first_button["text"]["content"]) <= 24


def test_waiting_card_no_options_shows_placeholder_body() -> None:
    waiting = WaitingFor(source="permission", question="")
    acc = TurnAccumulator()
    card = render_card(acc.snapshot(state="waiting", waiting_for=waiting))
    body = card["body"]["elements"][0]["content"]
    assert body == "_（Claude 在等你响应）_"


def test_waiting_card_keeps_assistant_text_context() -> None:
    """If Claude said something before the prompt fired, surface it."""
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="I'm about to run rm"))
    card = render_card(acc.snapshot(state="waiting", waiting_for=_waiting(n=2)))
    body = card["body"]["elements"][0]["content"]
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
    buttons = _action_buttons(card)
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
    body = card["body"]["elements"][0]["content"]
    assert "续下条" in body
    assert "⏬" in body
    # Sealed continuation cards are historical → no action row.
    assert "column_set" not in [e.get("tag") for e in card["body"]["elements"]]


def test_render_card_no_continuation_marker_by_default() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a", timestamp="t", text="some content"))
    card = render_card(acc.snapshot(state="running"))
    body = card["body"]["elements"][0]["content"]
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
    assert "续下条" in sealing["body"]["elements"][0]["content"]

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
        sealed_cards.append(sealed["body"]["elements"][0]["content"])
        acc.commit_to(text_end=text_end, tool_end=tool_end, thinking_end=thinking_end)

    # Final (open) card should fit without rotation.
    final_card = render_card(
        acc.snapshot(state="running", from_committed=True), is_continuation=True
    )
    final_body = final_card["body"]["elements"][0]["content"]

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


# ===================================== oversized single block + rotation-aware seal


def test_split_text_part_short_passthrough() -> None:
    from pocket_cc.relay.card_renderer import _split_text_part

    assert _split_text_part("hello") == ["hello"]


def test_split_text_part_packs_paragraphs_and_rejoins_exactly() -> None:
    """Paragraph-boundary split must reconstruct the original verbatim when
    rejoined with the accumulator's "\\n\\n" separator."""
    from pocket_cc.relay.card_renderer import _split_text_part

    original = "\n\n".join(f"paragraph {i} " + "x" * 400 for i in range(12))
    chunks = _split_text_part(original, limit=1000)
    assert len(chunks) >= 2
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n\n".join(chunks) == original


def test_split_text_part_hard_splits_single_giant_paragraph() -> None:
    from pocket_cc.relay.card_renderer import _split_text_part

    # One unbroken line, no paragraph/line boundaries to split on.
    giant = "A" * 5000
    chunks = _split_text_part(giant, limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == giant  # content fully preserved


def test_ingest_huge_assistant_text_is_stored_in_bounded_parts() -> None:
    from pocket_cc.relay.card_renderer import _TEXT_PART_MAX_CHARS

    acc = TurnAccumulator()
    acc.user_prompt = "x"
    huge = "\n\n".join(f"para {i}: " + "y" * 500 for i in range(20))  # ~10k chars
    acc.ingest(AssistantText(uuid="a", timestamp="t", text=huge))

    # Full content is preserved (no loss at ingest)…
    full = acc.snapshot(state="running", from_committed=False)
    assert "para 0:" in full.assistant_text
    assert "para 19:" in full.assistant_text
    # …and no single part can force a card to tail-truncate.
    text_end, _, _ = acc.find_fit_window(_TEXT_PART_MAX_CHARS + 200)
    assert text_end >= 1  # at least one whole part fits the budget


def test_single_huge_assistant_text_splits_across_cards_on_seal() -> None:
    """The seal path (Stop hook / shutdown) must roll a huge final block
    across multiple cards, not tail-truncate it onto the current one.

    Simulates bootstrap._seal_active against the accumulator primitives:
    rotate the from_committed tail until it fits, then render the final
    card from the *from_committed* tail (NOT full history)."""
    from pocket_cc.relay.card_renderer import ROTATE_AT_CHARS

    acc = TurnAccumulator()
    acc.user_prompt = "long answer"
    huge = "\n\n".join(f"section {i}\n" + "z" * 600 for i in range(15))  # ~9k chars
    acc.ingest(AssistantText(uuid="a", timestamp="t", text=huge))

    bodies: list[str] = []
    for _ in range(32):
        snap = acc.snapshot(state="running", from_committed=True)
        if not should_rotate(snap):
            break
        te, toe, the = acc.find_fit_window(ROTATE_AT_CHARS)
        seal_snap = acc.snapshot_window(text_end=te, tool_end=toe, thinking_end=the)
        bodies.append(
            render_card(seal_snap, ends_with_continuation_marker=True)["body"]["elements"][0]["content"]
        )
        acc.commit_to(text_end=te, tool_end=toe, thinking_end=the)

    # Final card: terminal state, rendered from the *uncommitted tail only*.
    final_snap = acc.snapshot(state="done", from_committed=True)
    final_card = render_card(final_snap, is_continuation=True)
    final_body = final_card["body"]["elements"][0]["content"]

    assert bodies, "a ~9k block must require at least one continuation card"
    for b in [*bodies, final_body]:
        assert "截断" not in b, f"no card should tail-truncate; got:\n{b}"
        assert len(b) <= 3100  # within the per-card body cap (+ footer slack)


def test_seal_uses_from_committed_tail_not_full_history() -> None:
    """Regression for the core bug: after rotation has committed early
    content, the final seal must render only the uncommitted tail — not the
    full turn (which re-dumped onto the last card and tail-truncated)."""
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(AssistantText(uuid="a1", timestamp="t", text="EARLY-" + "a" * 100))
    acc.ingest(AssistantText(uuid="a2", timestamp="t", text="LATE-" + "b" * 100))

    # Simulate a rotation that committed the first part.
    acc.commit_to(text_end=1, tool_end=0, thinking_end=0)

    final = acc.snapshot(state="done", from_committed=True)
    assert "LATE-" in final.assistant_text
    assert "EARLY-" not in final.assistant_text  # not re-dumped onto the final card
    # Full history still has everything (used nowhere in the seal now).
    full = acc.snapshot(state="done", from_committed=False)
    assert "EARLY-" in full.assistant_text and "LATE-" in full.assistant_text


# =============================================================== permission mode


def _mode_button_text(card: dict[str, Any]) -> str:
    """Pull the Mode button's text content out of a rendered running card."""
    buttons = _action_buttons(card)
    mode_buttons = [
        b for b in buttons if b["value"].get("action") == "key" and b["value"].get("key") == "BTab"
    ]
    assert len(mode_buttons) == 1, "expected exactly one Mode (BTab) button"
    return str(mode_buttons[0]["text"]["content"])


def test_mode_label_friendly_chinese_for_known_modes() -> None:
    assert mode_label("default") == "默认"
    assert mode_label("acceptEdits") == "自动接受"
    assert mode_label("plan") == "计划"
    assert mode_label("bypassPermissions") == "跳过权限"


def test_mode_label_unknown_falls_back_to_raw() -> None:
    """Unknown modes return the raw string so schema drift is visible
    (rather than silently masked behind a generic label)."""
    assert mode_label("newFancyMode") == "newFancyMode"


def test_accumulator_ingests_mode_change_event() -> None:
    acc = TurnAccumulator()
    assert acc.current_mode == "default"
    acc.ingest(ModeChange(uuid="m1", timestamp="t", mode="acceptEdits"))
    assert acc.current_mode == "acceptEdits"
    # Subsequent change overrides
    acc.ingest(ModeChange(uuid="m2", timestamp="t", mode="plan"))
    assert acc.current_mode == "plan"


def test_snapshot_propagates_current_mode() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "x"
    acc.ingest(ModeChange(uuid="m1", timestamp="t", mode="acceptEdits"))
    snap = acc.snapshot(state="running")
    assert snap.current_mode == "acceptEdits"


def test_running_card_mode_button_shows_default_label_by_default() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "demo"
    card = render_card(acc.snapshot(state="running"))
    text = _mode_button_text(card)
    # Suffix is shown — gives users an immediate "I can see what mode I'm in"
    # without having to click the button to find out.
    assert "默认" in text
    assert "⇧⭾" in text


def test_running_card_mode_button_reflects_current_mode() -> None:
    acc = TurnAccumulator()
    acc.user_prompt = "demo"
    acc.ingest(ModeChange(uuid="m1", timestamp="t", mode="acceptEdits"))
    card = render_card(acc.snapshot(state="running"))
    text = _mode_button_text(card)
    assert "自动接受" in text


def test_running_card_mode_button_shows_raw_for_unknown_mode() -> None:
    """If Claude ships a new permission mode we haven't mapped, the button
    surfaces the raw key — the user still sees something useful, and we
    can spot the new mode in transcripts to add a friendly label."""
    acc = TurnAccumulator()
    acc.user_prompt = "demo"
    acc.ingest(ModeChange(uuid="m1", timestamp="t", mode="someFutureMode"))
    card = render_card(acc.snapshot(state="running"))
    assert "someFutureMode" in _mode_button_text(card)


def test_done_card_has_no_action_row() -> None:
    """Sanity check — only running cards carry the action row, so mode
    label only appears on cards that have buttons. Done/failed cards
    don't have a Mode button at all (turn is over, can't change modes
    from a sealed card)."""
    acc = TurnAccumulator()
    acc.user_prompt = "done thing"
    card = render_card(acc.snapshot(state="done"))
    tags = [e.get("tag") for e in card["body"]["elements"]]
    assert "column_set" not in tags
