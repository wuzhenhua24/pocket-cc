"""Unit tests for claude/pane_inspector.py — Permission prompt parsing."""

from __future__ import annotations

import textwrap

from pocket_cc.claude.pane_inspector import (
    ParsedOption,
    ParsedPrompt,
    detect_mode,
    inspect_pane,
)

# A realistic capture of the prompt we observed in the bug report
_REAL_PROMPT = textwrap.dedent(
    """\
    > 你好,src下有哪些目录

      Listed 1 directory (ctrl+o to expand)

    Bash command

      cd /Users/zhenhua/Documents/gitpro/pocket-cc/src/pocket_cc && for d in app claude lark relay tmux; do echo "=== $d ==="; ls "$d"; done
      List contents of each subdirectory

    Contains shell syntax (string) that cannot be statically analyzed

    Do you want to proceed?
    ❯ 1. Yes
      2. No

    Esc to cancel · Tab to amend
    """
)


def test_no_prompt_returns_none() -> None:
    pane = "some output\nmore output\n$ "
    assert inspect_pane(pane) is None


def test_empty_pane_returns_none() -> None:
    assert inspect_pane("") is None
    assert inspect_pane("\n\n\n") is None


def test_real_world_permission_prompt_detected() -> None:
    parsed = inspect_pane(_REAL_PROMPT)
    assert parsed is not None
    assert parsed.kind == "permission"
    assert parsed.question == "Do you want to proceed?"
    assert len(parsed.options) == 2
    assert parsed.options[0].number == 1
    assert parsed.options[0].label == "Yes"
    assert parsed.options[0].selected is True
    assert parsed.options[1].number == 2
    assert parsed.options[1].label == "No"
    assert parsed.options[1].selected is False


def test_context_extracted_above_question() -> None:
    parsed = inspect_pane(_REAL_PROMPT)
    assert parsed is not None
    assert "Bash command" in parsed.context
    assert "for d in app claude" in parsed.context
    assert "Contains shell syntax" in parsed.context


def test_question_without_options_returns_none() -> None:
    """A stray 'Do you want to proceed?' without options must not match.
    (Defensive against stale renders where the option list got cleared.)"""
    pane = textwrap.dedent(
        """\
        Some text

        Do you want to proceed?

        $ next prompt
        """
    )
    assert inspect_pane(pane) is None


def test_multiple_prompts_uses_latest() -> None:
    """If two prompts appear in the same capture, only the most recent one matters."""
    pane = textwrap.dedent(
        """\
        First Bash command
          old-command

        Do you want to proceed?
        ❯ 1. Yes
          2. No

        Second Bash command
          newer-command

        Do you want to proceed?
        ❯ 1. Accept
          2. Decline
          3. Skip
        """
    )
    parsed = inspect_pane(pane)
    assert parsed is not None
    assert len(parsed.options) == 3
    assert parsed.options[0].label == "Accept"


def test_options_terminate_on_footer_hint() -> None:
    pane = textwrap.dedent(
        """\
        Bash command
          cmd

        Do you want to proceed?
        ❯ 1. Yes
          2. No
          3. Maybe
        Esc to cancel · Tab to amend
        4. Should-be-ignored-after-footer
        """
    )
    parsed = inspect_pane(pane)
    assert parsed is not None
    assert {o.number for o in parsed.options} == {1, 2, 3}


def test_options_terminate_on_blank_line() -> None:
    pane = textwrap.dedent(
        """\
        Do you want to proceed?
        ❯ 1. Yes
          2. No

        Esc to cancel
        """
    )
    parsed = inspect_pane(pane)
    assert parsed is not None
    assert len(parsed.options) == 2


def test_fingerprint_is_stable_for_same_prompt() -> None:
    """Re-running the inspector on the same pane content yields the same fingerprint."""
    a = inspect_pane(_REAL_PROMPT)
    b = inspect_pane(_REAL_PROMPT)
    assert a is not None and b is not None
    assert a.fingerprint == b.fingerprint


def test_fingerprint_ignores_cursor_position() -> None:
    """User moving the ❯ cursor in tmux should NOT generate a new fingerprint."""
    a = inspect_pane(_REAL_PROMPT)
    moved = _REAL_PROMPT.replace("❯ 1. Yes", "  1. Yes").replace("  2. No", "❯ 2. No")
    b = inspect_pane(moved)
    assert a is not None and b is not None
    assert a.fingerprint == b.fingerprint
    # …but the cursor IS observable on the ParsedOption
    assert a.options[0].selected is True
    assert b.options[0].selected is False
    assert b.options[1].selected is True


def test_fingerprint_changes_when_options_change() -> None:
    other = _REAL_PROMPT.replace("2. No", "2. Different label")
    a = inspect_pane(_REAL_PROMPT)
    b = inspect_pane(other)
    assert a is not None and b is not None
    assert a.fingerprint != b.fingerprint


def test_fingerprint_changes_when_command_changes() -> None:
    other = _REAL_PROMPT.replace("for d in app claude lark relay tmux", "for d in different list")
    a = inspect_pane(_REAL_PROMPT)
    b = inspect_pane(other)
    assert a is not None and b is not None
    assert a.fingerprint != b.fingerprint


def test_parsed_dataclasses_are_frozen() -> None:
    """Sanity: ParsedPrompt / ParsedOption are immutable so the watcher can
    hand them around threads safely."""
    parsed = inspect_pane(_REAL_PROMPT)
    assert parsed is not None
    import dataclasses

    assert dataclasses.is_dataclass(ParsedPrompt)
    assert dataclasses.is_dataclass(ParsedOption)


# ============================================================ plan-mode

# A realistic capture of the ExitPlanMode confirmation, taken from a live
# Claude Code session running with `--permission-mode plan`. The plan body
# above (which would be hundreds of lines) is deliberately abbreviated here
# — pane_inspector does NOT extract plan context (see module docstring), so
# the test fixture only needs the prompt block itself for assertions.
_REAL_PLAN_PROMPT = textwrap.dedent(
    """\
    ────────────────────────────────────────────────────────────
     Ready to code?

     Here is Claude's plan:
    ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
     用 claude-agent-sdk-python 搭建微服务日志分析机器人

     ...（plan 主体，可能很长）...
    ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌

    ────────────────────────────────────────────────────────────
     Claude has written up a plan and is ready to execute. Would you like to proceed?

     ❯ 1. Yes, auto-accept edits
       2. Yes, manually approve edits
       3. No, refine with Ultraplan on Claude Code on the web
       4. Tell Claude what to change
          shift+tab to approve with this feedback

     ctrl-g to edit in  VS Code  · ~/.claude/plans/sdk-harmonic-flame.md
    """
)


def test_real_world_plan_prompt_detected() -> None:
    parsed = inspect_pane(_REAL_PLAN_PROMPT)
    assert parsed is not None
    assert parsed.kind == "plan"
    assert parsed.question.startswith("Claude has written up a plan")
    assert parsed.question.endswith("Would you like to proceed?")


def test_plan_prompt_extracts_all_four_options() -> None:
    parsed = inspect_pane(_REAL_PLAN_PROMPT)
    assert parsed is not None
    assert [o.number for o in parsed.options] == [1, 2, 3, 4]
    assert parsed.options[0].label == "Yes, auto-accept edits"
    assert parsed.options[1].label == "Yes, manually approve edits"
    assert parsed.options[2].label == "No, refine with Ultraplan on Claude Code on the web"
    assert parsed.options[3].label == "Tell Claude what to change"
    # The `shift+tab to approve…` continuation line under option 4 is NOT
    # captured — it doesn't match the numbered-option pattern, so
    # `_extract_options` terminates there. Losing that hint is acceptable
    # (it's not actionable from Lark anyway).


def test_plan_prompt_cursor_on_first_option() -> None:
    parsed = inspect_pane(_REAL_PLAN_PROMPT)
    assert parsed is not None
    assert parsed.options[0].selected is True
    assert all(not o.selected for o in parsed.options[1:])


def test_plan_prompt_context_is_empty() -> None:
    """pane_inspector intentionally skips plan-body extraction (see module
    docstring). The card renderer will pull the full plan from the
    transcript's ExitPlanMode tool_use payload instead."""
    parsed = inspect_pane(_REAL_PLAN_PROMPT)
    assert parsed is not None
    assert parsed.context == ""


def test_plan_prompt_without_options_returns_none() -> None:
    """Defensive: a stray plan-question line without numbered options must
    not be treated as an active prompt."""
    pane = textwrap.dedent(
        """\
        Claude has written up a plan and is ready to execute. Would you like to proceed?

        $ next prompt
        """
    )
    assert inspect_pane(pane) is None


def test_plan_and_permission_distinguished_by_question_wording() -> None:
    """The two prompts must not collide — `Do you want to proceed?` is
    permission, `Would you like to proceed?` (with the plan preamble) is plan."""
    permission = inspect_pane(_REAL_PROMPT)
    plan = inspect_pane(_REAL_PLAN_PROMPT)
    assert permission is not None and plan is not None
    assert permission.kind == "permission"
    assert plan.kind == "plan"


def test_plan_after_permission_in_same_capture_picks_latest() -> None:
    """Bottom-up scan returns the most recent prompt regardless of kind."""
    pane = _REAL_PROMPT + "\n" + _REAL_PLAN_PROMPT
    parsed = inspect_pane(pane)
    assert parsed is not None
    assert parsed.kind == "plan"


def test_permission_after_plan_in_same_capture_picks_latest() -> None:
    pane = _REAL_PLAN_PROMPT + "\n" + _REAL_PROMPT
    parsed = inspect_pane(pane)
    assert parsed is not None
    assert parsed.kind == "permission"


def test_plan_fingerprint_stable_across_cursor_moves() -> None:
    a = inspect_pane(_REAL_PLAN_PROMPT)
    moved = _REAL_PLAN_PROMPT.replace("❯ 1. Yes, auto-accept edits", "  1. Yes, auto-accept edits")
    moved = moved.replace("  2. Yes, manually", "❯ 2. Yes, manually")
    b = inspect_pane(moved)
    assert a is not None and b is not None
    assert a.fingerprint == b.fingerprint


# ======================================================= AskUserQuestion

# A real capture of the AskUserQuestion widget — three questions, the first
# is multiSelect (the `[ ]` checkbox markers next to each option are the
# multiSelect rendering; single-select questions render without them).
_REAL_ASK_USER_PROMPT = textwrap.dedent(
    """\
    ──────────────────────────────────────────────────────────
    ←  ☐ 日志来源  ☐ 运行形态  ☐ 分析目标  ✔ Submit  →

    测试环境日志主要来自哪里？这决定了我们怎么把日志喂给 Claude（直接读文件 vs 写 MCP/自定义工具调 API）。

    ❯ 1. [ ] 本地/服务器文件
      日志直接落在文件系统（如 /var/log），可以用 Read/Grep 直接读
      2. [ ] ELK / OpenSearch
      需要通过 Elasticsearch API 查询，要写自定义工具
      3. [ ] 云日志服务
      如阿里云 SLS、AWS CloudWatch、GCP Logging，需要对应 SDK 集成
      4. [ ] Loki / Grafana
      通过 LogQL 查询，需要 API 集成
      5. [ ] Type something
         Next
    ──────────────────────────────────────────────────────────
      6. Chat about this

    Enter to select · Tab/Arrow keys to navigate · Esc to cancel
    """
)


def test_ask_user_widget_detected_via_footer() -> None:
    parsed = inspect_pane(_REAL_ASK_USER_PROMPT)
    assert parsed is not None
    assert parsed.kind == "ask_user_question"


def test_ask_user_returns_sparse_prompt_for_transcript_to_fill() -> None:
    """For AskUserQuestion, pane data is only used to detect "is the widget
    open?" and to fingerprint. The actual question text + options come from
    the transcript (richer + cleaner), so ParsedPrompt's content fields are
    deliberately empty."""
    parsed = inspect_pane(_REAL_ASK_USER_PROMPT)
    assert parsed is not None
    assert parsed.kind == "ask_user_question"
    assert parsed.question == ""
    assert parsed.context == ""
    assert parsed.options == ()
    # Fingerprint is non-empty and deterministic.
    assert parsed.fingerprint
    assert parsed.fingerprint == inspect_pane(_REAL_ASK_USER_PROMPT).fingerprint  # type: ignore[union-attr]


def test_ask_user_takes_priority_over_permission_in_mixed_pane() -> None:
    """If both signals appear (e.g. stale permission text scrolled up), the
    AskUserQuestion widget — which is what the user can interact with right
    now — wins."""
    pane = _REAL_PROMPT + "\n" + _REAL_ASK_USER_PROMPT
    parsed = inspect_pane(pane)
    assert parsed is not None
    assert parsed.kind == "ask_user_question"


def test_ask_user_fingerprint_changes_when_question_advances() -> None:
    """Simulate Claude advancing from Q1 to Q2 in a multi-question call —
    the visible question text differs, so the fingerprint must too."""
    a = inspect_pane(_REAL_ASK_USER_PROMPT)
    advanced = _REAL_ASK_USER_PROMPT.replace(
        "测试环境日志主要来自哪里？",
        "你希望这个分析工具以什么形态运行？",
    )
    b = inspect_pane(advanced)
    assert a is not None and b is not None
    assert a.fingerprint != b.fingerprint


def test_ask_user_fingerprint_stable_across_cursor_moves() -> None:
    """User moving the ❯ cursor inside the same question (e.g. via arrow
    keys in tmux) must not generate a new fingerprint — same prompt."""
    a = inspect_pane(_REAL_ASK_USER_PROMPT)
    moved = _REAL_ASK_USER_PROMPT.replace("❯ 1. [ ]", "  1. [ ]")
    moved = moved.replace("  2. [ ]", "❯ 2. [ ]", 1)
    b = inspect_pane(moved)
    assert a is not None and b is not None
    assert a.fingerprint == b.fingerprint


def test_ask_user_widget_returns_none_when_footer_absent() -> None:
    """A pane that *looks* like AskUserQuestion (tab bar + options) but is
    missing the footer (e.g. stale render mid-tick) must not be detected."""
    pane = _REAL_ASK_USER_PROMPT.replace(
        "Enter to select · Tab/Arrow keys to navigate · Esc to cancel",
        "(footer hidden / stale frame)",
    )
    parsed = inspect_pane(pane)
    # Falls through to the permission/plan scan — there's no permission/plan
    # question text in this pane, so result is None.
    assert parsed is None


# ============================================================== detect_mode


def test_detect_mode_accept_edits_marker_line() -> None:
    pane = "some output\n> the input box\n⏵⏵ accept edits on (shift+tab to cycle)"
    assert detect_mode(pane) == "acceptEdits"


def test_detect_mode_auto_accept_phrasing() -> None:
    pane = "output\n⏵⏵ auto-accept edits on  >"
    assert detect_mode(pane) == "acceptEdits"


def test_detect_mode_plan_marker_line() -> None:
    pane = "output\n⏸ plan mode on (shift+tab to cycle)"
    assert detect_mode(pane) == "plan"


def test_detect_mode_bypass_marker_line() -> None:
    pane = "output\n⏵⏵ bypass permissions  >"
    assert detect_mode(pane) == "bypassPermissions"


def test_detect_mode_default_has_no_banner() -> None:
    """Default mode shows no mode-line → None (callers map to 'default')."""
    pane = "some output\n╭─────────╮\n│ > type here │\n╰─────────╯\n  ? for shortcuts"
    assert detect_mode(pane) is None


def test_detect_mode_empty_pane() -> None:
    assert detect_mode("") is None


def test_detect_mode_hint_only_fallback_without_glyph() -> None:
    """Some renders show the text without the ⏵⏵/⏸ glyph — still detected."""
    pane = "output\nplan mode on (shift+tab to cycle)"
    assert detect_mode(pane) == "plan"


def test_detect_mode_marker_with_unknown_text_is_none() -> None:
    """A mode banner we can't map → None, rather than guessing wrong."""
    pane = "output\n⏵⏵ some brand new mode  >"
    assert detect_mode(pane) is None


def test_detect_mode_picks_bottommost_banner() -> None:
    """The active banner is the bottommost — an older one scrolled up loses."""
    pane = "⏸ plan mode\n... lots of output ...\n⏵⏵ accept edits on"
    assert detect_mode(pane) == "acceptEdits"
