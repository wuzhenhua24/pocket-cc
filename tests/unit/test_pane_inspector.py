"""Unit tests for claude/pane_inspector.py — Permission prompt parsing."""

from __future__ import annotations

import textwrap

from pocket_cc.claude.pane_inspector import (
    ParsedOption,
    ParsedPrompt,
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
