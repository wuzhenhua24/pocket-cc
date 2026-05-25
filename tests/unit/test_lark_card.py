"""Unit tests for lark/card.py — pure-function shape assertions."""

from __future__ import annotations

from pocket_cc.lark.card import (
    DEFAULT_RUNNING_ACTIONS,
    CardButton,
    ExpandableSection,
    build_status_card,
    build_text_card,
    normalize_markdown_for_lark,
)


def test_minimal_running_card() -> None:
    card = build_status_card(title="task A", body="hello world")
    assert card["config"] == {"wide_screen_mode": True}
    assert card["header"]["template"] == "blue"
    assert card["header"]["title"]["content"] == "⏳ task A"
    assert card["elements"] == [{"tag": "markdown", "content": "hello world"}]


def test_state_changes_header_color_and_emoji() -> None:
    cases = {
        "running": ("blue", "⏳"),
        "done": ("green", "✅"),
        "failed": ("red", "❌"),
        "waiting": ("orange", "❓"),
    }
    for state, (color, emoji) in cases.items():
        card = build_status_card(title="t", body="b", state=state)  # type: ignore[arg-type]
        assert card["header"]["template"] == color, state
        assert card["header"]["title"]["content"] == f"{emoji} t", state


def test_actions_are_rendered_with_hr_separator() -> None:
    actions = [
        CardButton(text="A", value={"x": 1}),
        CardButton(text="B", value={"x": 2}, style="primary"),
    ]
    card = build_status_card(title="t", body="b", actions=actions)
    elements = card["elements"]
    # body, hr, action
    assert [e["tag"] for e in elements] == ["markdown", "hr", "action"]
    rendered = elements[2]["actions"]
    assert len(rendered) == 2
    assert rendered[0]["text"]["content"] == "A"
    assert rendered[0]["type"] == "default"
    assert rendered[1]["type"] == "primary"
    assert rendered[0]["value"] == {"x": 1}


def test_detail_section_renders_as_note_plus_markdown() -> None:
    card = build_status_card(
        title="t",
        body="b",
        detail=ExpandableSection(label="Tool calls (4)", content="- Read foo.py\n- Edit bar.py"),
    )
    elements = card["elements"]
    # body, hr, note, markdown
    assert [e["tag"] for e in elements] == ["markdown", "hr", "note", "markdown"]
    note = elements[2]
    assert note["elements"][0]["content"] == "▸ Tool calls (4)"
    assert elements[3]["content"] == "- Read foo.py\n- Edit bar.py"


def test_detail_and_actions_combined_section_order() -> None:
    card = build_status_card(
        title="t",
        body="b",
        detail=ExpandableSection(label="Detail", content="hidden info"),
        actions=[CardButton(text="X", value={})],
    )
    tags = [e["tag"] for e in card["elements"]]
    # body, hr, note, markdown, hr, action
    assert tags == ["markdown", "hr", "note", "markdown", "hr", "action"]


def test_button_value_is_defensively_copied() -> None:
    payload = {"action": "cancel"}
    card = build_status_card(
        title="t",
        body="b",
        actions=[CardButton(text="cancel", value=payload)],
    )
    rendered_value = card["elements"][-1]["actions"][0]["value"]
    rendered_value["action"] = "mutated"
    assert payload == {"action": "cancel"}, "card builder should defensive-copy button.value"


def test_text_card_has_no_header() -> None:
    card = build_text_card(body="just a note")
    assert "header" not in card
    assert card["elements"] == [{"tag": "markdown", "content": "just a note"}]


def test_restart_notice_card_is_grey_and_actionless() -> None:
    from pocket_cc.lark.card import build_restart_notice_card

    card = build_restart_notice_card()
    assert card["header"]["template"] == "grey"
    assert "已重启" in card["header"]["title"]["content"]
    # No action row — restart is informational; user sends a new message
    # to start a fresh turn.
    assert not any(el.get("tag") == "action" for el in card["elements"])


def test_default_running_actions_shape() -> None:
    assert all(isinstance(b, CardButton) for b in DEFAULT_RUNNING_ACTIONS)
    cancel = next(b for b in DEFAULT_RUNNING_ACTIONS if b.value.get("action") == "cancel")
    assert cancel.style == "danger"
    # `key` action: single keystroke (e.g. BTab for mode toggle)
    single_keys = [
        b.value["key"] for b in DEFAULT_RUNNING_ACTIONS if b.value.get("action") == "key"
    ]
    assert "BTab" in single_keys
    # `key_sequence` action: multiple keys with optional delay (e.g. ⎋ Esc
    # is double-Escape to dodge Lark's rate limit on consecutive callbacks)
    seq_buttons = [b for b in DEFAULT_RUNNING_ACTIONS if b.value.get("action") == "key_sequence"]
    assert len(seq_buttons) == 1
    assert seq_buttons[0].value["keys"] == ["Escape", "Escape"]


# =================================================== normalize_markdown_for_lark


def test_normalize_passes_through_already_compatible_markdown() -> None:
    src = "**bold** and *italic* with `code` and [link](https://x)\n- a\n- b\n"
    assert normalize_markdown_for_lark(src) == src


def test_normalize_empty_string_is_noop() -> None:
    assert normalize_markdown_for_lark("") == ""


def test_normalize_converts_headings_to_bold() -> None:
    src = "# Title\n## Section\n### Sub\nbody"
    out = normalize_markdown_for_lark(src)
    assert out == "**Title**\n**Section**\n**Sub**\nbody"


def test_normalize_preserves_heading_indentation() -> None:
    src = "  ## indented heading"
    out = normalize_markdown_for_lark(src)
    assert out == "  **indented heading**"


def test_normalize_only_touches_atx_headings() -> None:
    """`#tag` (no space) is NOT a heading — must stay as-is.
    Same for `text # not-a-heading` mid-line."""
    src = "#tag should-stay\nfoo #not heading"
    assert normalize_markdown_for_lark(src) == src


def test_normalize_heading_with_existing_bold_is_unwrapped() -> None:
    """Claude often writes `### **Section**` — don't double-bold."""
    src = "### **测试环境配置专家**"
    assert normalize_markdown_for_lark(src) == "**测试环境配置专家**"


def test_normalize_heading_with_existing_underline_bold_is_unwrapped() -> None:
    src = "## __Section__"
    assert normalize_markdown_for_lark(src) == "**Section**"


def test_normalize_heading_with_multiple_bold_layers_unwrapped() -> None:
    """Adversarial: heading already double-wrapped — peel both layers."""
    src = "### ****Title****"
    assert normalize_markdown_for_lark(src) == "**Title**"


def test_normalize_heading_with_empty_bold_drops_to_blank() -> None:
    src = "### ****"
    assert normalize_markdown_for_lark(src) == ""


def test_normalize_heading_with_partial_bold_kept_verbatim() -> None:
    """If only PART of the heading is bold, peel-unwrap doesn't fire (only
    whole-string wraps are peeled). The result has nested `**` which Lark
    may render imperfectly — but this case is rare in Claude output and
    we deliberately stay conservative."""
    src = "### **lead** rest"
    out = normalize_markdown_for_lark(src)
    # `**` + (`**lead** rest`) + `**` — nested bold; Lark renders imperfectly
    # but the heading is at least visible. Stays conservative on purpose.
    assert out == "****lead** rest**"


def test_normalize_is_idempotent() -> None:
    src = "## hello\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    once = normalize_markdown_for_lark(src)
    twice = normalize_markdown_for_lark(once)
    assert once == twice


def test_normalize_converts_two_column_table() -> None:
    src = (
        "| 模块 | 说明 |\n"
        "|---|---|\n"
        "| ploto-bff | Backend for Frontend 层 |\n"
        "| ploto-monitor | 监控指标功能 |\n"
    )
    out = normalize_markdown_for_lark(src)
    assert "**模块**: ploto-bff" in out
    assert "**说明**: Backend for Frontend 层" in out
    assert "**模块**: ploto-monitor" in out
    # No leftover GFM table syntax in output
    assert "|---" not in out
    assert "| 模块 | 说明 |" not in out
    # Each row becomes a list item
    rows = [line for line in out.splitlines() if line.startswith("- ")]
    assert len(rows) == 2


def test_normalize_converts_three_column_table() -> None:
    src = "| h1 | h2 | h3 |\n|---|---|---|\n| a | b | c |\n"
    out = normalize_markdown_for_lark(src)
    assert "**h1**: a" in out
    assert "**h2**: b" in out
    assert "**h3**: c" in out
    assert " · " in out  # multi-column separator


def test_normalize_handles_separator_with_alignment() -> None:
    """GFM tables may use `:---:` / `:---` / `---:` for alignment."""
    src = "| a | b |\n| :--- | ---: |\n| 1 | 2 |\n"
    out = normalize_markdown_for_lark(src)
    assert "**a**: 1" in out
    assert "**b**: 2" in out


def test_normalize_leaves_non_table_pipes_alone() -> None:
    """A line with `|` but no separator-row underneath isn't a table."""
    src = "you can use `a | b` syntax in shell pipes\nand `find . | grep foo`"
    assert normalize_markdown_for_lark(src) == src


def test_normalize_mixed_content() -> None:
    """A realistic Claude response — heading + para + table + list."""
    src = (
        "## 核心信息\n"
        "- **技术栈**: Java 8\n"
        "\n"
        "## 主要模块\n"
        "| 模块 | 说明 |\n"
        "|---|---|\n"
        "| ploto-bff | BFF 层 |\n"
        "\n"
        "结束。\n"
    )
    out = normalize_markdown_for_lark(src)
    # Headings → bold
    assert "**核心信息**" in out
    assert "**主要模块**" in out
    assert "## " not in out
    # Existing bold list item stays
    assert "- **技术栈**: Java 8" in out
    # Table → bulleted row
    assert "- **模块**: ploto-bff · **说明**: BFF 层" in out
    # Trailing text stays
    assert "结束。" in out


def test_build_status_card_normalizes_body() -> None:
    """End-to-end: heading in body → bold in rendered card content."""
    card = build_status_card(
        title="hello",
        body="## Result\nAll good.",
    )
    body_content = card["elements"][0]["content"]
    assert "**Result**" in body_content
    assert "## " not in body_content


def test_build_status_card_normalizes_detail_content() -> None:
    card = build_status_card(
        title="x",
        body="b",
        detail=ExpandableSection(label="More", content="## Detail\nbody"),
    )
    # Detail markdown is the 4th element (body, hr, note, markdown)
    detail_md = card["elements"][3]
    assert detail_md["tag"] == "markdown"
    assert "**Detail**" in detail_md["content"]


def test_build_text_card_normalizes_body() -> None:
    card = build_text_card(body="## Note\n- one")
    assert "**Note**" in card["elements"][0]["content"]
