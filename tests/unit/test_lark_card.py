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


def test_normalize_converts_two_column_table_to_code_block() -> None:
    """Narrow plain-text tables render as a fenced code block so Lark's
    monospace font preserves column alignment — users *see* a table."""
    src = (
        "| 模块 | 说明 |\n"
        "|---|---|\n"
        "| ploto-bff | Backend for Frontend 层 |\n"
        "| ploto-monitor | 监控指标功能 |\n"
    )
    out = normalize_markdown_for_lark(src)
    # Old leftover syntax must be gone regardless of which rendering picked
    assert "|---" not in out
    assert "| 模块 | 说明 |" not in out
    # Code-block fence opens and closes
    assert "```" in out
    assert out.count("```") == 2
    # Header + separator + data rows all live inside the fence
    assert "模块" in out and "说明" in out
    assert "ploto-bff" in out
    assert "ploto-monitor" in out
    # Separator row uses dashes (no pipes-with-dashes since we rebuild it)
    assert any(set(line.strip()) <= {"-", "|", " "} and "-" in line
               for line in out.splitlines())
    # Bullets path NOT taken — no `**模块**:` pairs
    assert "**模块**:" not in out


def test_normalize_converts_three_column_table_to_code_block() -> None:
    src = "| h1 | h2 | h3 |\n|---|---|---|\n| a | b | c |\n"
    out = normalize_markdown_for_lark(src)
    assert "```" in out
    assert "h1" in out and "h2" in out and "h3" in out
    assert "a" in out and "b" in out and "c" in out


def test_normalize_handles_separator_with_alignment() -> None:
    """GFM tables may use `:---:` / `:---` / `---:` for alignment markers
    in the separator row — those must still be recognized as a table."""
    src = "| a | b |\n| :--- | ---: |\n| 1 | 2 |\n"
    out = normalize_markdown_for_lark(src)
    assert "```" in out
    assert "a" in out and "b" in out
    assert "1" in out and "2" in out


def test_normalize_table_with_wide_cell_falls_back_to_bullets() -> None:
    """A long-prose cell would force horizontal scroll inside a code
    block (much worse than wrapped bullets on mobile) — fall back."""
    long_prose = "Backend for Frontend 层负责承接前端请求并做模型聚合编排"  # >24 disp chars
    src = (
        "| 模块 | 说明 |\n"
        "|---|---|\n"
        f"| ploto-bff | {long_prose} |\n"
    )
    out = normalize_markdown_for_lark(src)
    assert "```" not in out
    assert "- **模块**: ploto-bff" in out
    assert long_prose in out


def test_normalize_table_with_inline_markdown_in_cell_falls_back_to_bullets() -> None:
    """A code-block fence prints inline markdown as literal text —
    regression vs the bullet form. Detect and fall back."""
    src = (
        "| 名称 | 文档 |\n"
        "|---|---|\n"
        "| api | [docs](http://x) |\n"
        "| ui  | **WIP** |\n"
    )
    out = normalize_markdown_for_lark(src)
    assert "```" not in out
    # Both inline-md flavors preserved in bullet form (rendered live by Lark)
    assert "[docs](http://x)" in out
    assert "**WIP**" in out


def test_normalize_single_column_pseudo_table_is_left_alone() -> None:
    """One-column ``|---|`` blocks aren't recognized as GFM tables by the
    detector at all (separator-row regex requires ≥2 columns) — they
    pass through unchanged. Also documents the defensive
    ``len(header) < 2 → bullets`` branch in ``_should_use_code_block_table``:
    if the detector ever broadens we still avoid emitting a degenerate
    1-column code-block."""
    src = "| Tasks |\n|---|\n| build |\n| test |\n"
    out = normalize_markdown_for_lark(src)
    assert "```" not in out
    # Unchanged passthrough — no bullets, no code block, no table rewrite
    assert out == src


def test_normalize_code_block_table_aligns_cjk_columns() -> None:
    """CJK chars are 2 display cells wide — column alignment must
    account for that or the table looks ragged."""
    src = (
        "| 名 | desc |\n"
        "|---|---|\n"
        "| 张三 | dev |\n"
        "| 李 | qa |\n"
    )
    out = normalize_markdown_for_lark(src)
    assert "```" in out
    # The header / data rows that have a 2-char-CJK name should align
    # with the data row that has a 1-char-CJK name (李) — both end at
    # the same pipe column. Easiest invariant to assert: each data line
    # inside the fence has the same display width up to the first ` | `.
    lines = [line for line in out.splitlines() if "|" in line]
    name_col_widths = {
        sum(2 if ord(c) > 0x2E80 else 1 for c in line.split(" | ")[0])
        for line in lines
    }
    # All lines pad to the same name-column width.
    assert len(name_col_widths) == 1


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
    # Table → fenced code block (narrow plain cells qualify)
    assert "```" in out
    assert "ploto-bff" in out and "BFF 层" in out
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
