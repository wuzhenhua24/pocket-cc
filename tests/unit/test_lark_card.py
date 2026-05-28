"""Unit tests for lark/card.py — pure-function shape assertions."""

from __future__ import annotations

from pocket_cc.lark.card import (
    DEFAULT_RUNNING_ACTIONS,
    CardButton,
    ExpandableSection,
    normalize_markdown_for_lark,
)


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


# =========================================================================
# Schema 2.0 (cardkit) builders
# =========================================================================

from pocket_cc.lark.card import (  # noqa: E402  (grouped at end so legacy tests stay first)
    ELEMENT_ID_ACTIONS,
    ELEMENT_ID_ACTIONS_DIVIDER,
    ELEMENT_ID_BODY,
    ELEMENT_ID_DETAIL_CONTENT,
    ELEMENT_ID_DETAIL_DIVIDER,
    ELEMENT_ID_DETAIL_LABEL,
    build_restart_notice_card_v2,
    build_status_card_v2,
    build_text_card_v2,
)


def test_v2_minimal_running_card_shape() -> None:
    card = build_status_card_v2(title="task A", body="hello world")
    assert card["schema"] == "2.0"
    # Streaming on for running so cardkit's PUT endpoint accepts updates.
    assert card["config"]["streaming_mode"] is True
    assert card["config"]["summary"]["content"] == "⏳ task A"
    assert card["header"]["template"] == "blue"
    assert card["header"]["title"]["content"] == "⏳ task A"
    # Body lives under body.elements, not top-level elements (v1 → v2 move).
    assert "elements" not in card
    elements = card["body"]["elements"]
    assert elements == [
        {"tag": "markdown", "element_id": ELEMENT_ID_BODY, "content": "hello world"},
    ]


def test_v2_streaming_mode_only_on_active_states() -> None:
    """Terminal states (done/failed/cancelled) emit streaming_mode=False so
    IM list-preview / forward behavior settles. waiting stays True because
    the card is still awaiting user action."""
    cases = {
        "running": True,
        "waiting": True,
        "done": False,
        "failed": False,
        "cancelled": False,
    }
    for state, expected in cases.items():
        card = build_status_card_v2(title="t", body="b", state=state)  # type: ignore[arg-type]
        assert card["config"]["streaming_mode"] is expected, state


def test_v2_state_drives_template_and_emoji() -> None:
    cases = {
        "running": ("blue", "⏳"),
        "done": ("green", "✅"),
        "failed": ("red", "❌"),
        "waiting": ("orange", "❓"),
        "cancelled": ("grey", "⏹"),
    }
    for state, (color, emoji) in cases.items():
        card = build_status_card_v2(title="t", body="b", state=state)  # type: ignore[arg-type]
        assert card["header"]["template"] == color, state
        assert card["header"]["title"]["content"] == f"{emoji} t", state
        assert card["config"]["summary"]["content"] == f"{emoji} t", state


def test_v2_body_element_id_is_stable() -> None:
    """Regression guard: Phase 3's CardStream hard-codes ELEMENT_ID_BODY as
    the streaming target. If this constant ever drifts away from what the
    builder actually emits, streaming PUTs would silently target a phantom
    element id."""
    card = build_status_card_v2(title="t", body="x")
    body_element = card["body"]["elements"][0]
    assert body_element["element_id"] == ELEMENT_ID_BODY
    assert ELEMENT_ID_BODY == "body"  # frozen value — bumping requires a CardStream review


def test_v2_actions_render_as_column_set_with_per_button_columns() -> None:
    """v2 dropped the v1 ``action`` container — multi-button rows are a
    column_set whose columns each hold a single button."""
    actions = [
        CardButton(text="A", value={"x": 1}),
        CardButton(text="B", value={"x": 2}, style="primary"),
        CardButton(text="C", value={"x": 3}, style="danger"),
    ]
    card = build_status_card_v2(title="t", body="b", actions=actions)
    elements = card["body"]["elements"]
    # body, hr-divider, column_set
    tags = [e["tag"] for e in elements]
    assert tags == ["markdown", "hr", "column_set"]
    assert elements[1]["element_id"] == ELEMENT_ID_ACTIONS_DIVIDER
    col_set = elements[2]
    assert col_set["element_id"] == ELEMENT_ID_ACTIONS
    cols = col_set["columns"]
    assert len(cols) == 3
    for col, expected in zip(cols, actions, strict=True):
        assert col["tag"] == "column"
        btn = col["elements"][0]
        assert btn["tag"] == "button"
        assert btn["text"]["content"] == expected.text
        assert btn["type"] == expected.style
        assert btn["value"] == expected.value


def test_v2_button_value_is_defensively_copied() -> None:
    payload = {"action": "cancel"}
    card = build_status_card_v2(
        title="t",
        body="b",
        actions=[CardButton(text="cancel", value=payload)],
    )
    rendered = card["body"]["elements"][-1]["columns"][0]["elements"][0]["value"]
    rendered["action"] = "mutated"
    assert payload == {"action": "cancel"}


def test_v2_detail_section_uses_grey_markdown_label() -> None:
    """v1 ``note`` tag is gone in v2 — we render the label as a grey
    markdown line so the visual weight matches the legacy look."""
    card = build_status_card_v2(
        title="t",
        body="b",
        detail=ExpandableSection(label="Tool calls (4)", content="- Read foo.py"),
    )
    elements = card["body"]["elements"]
    tags = [e["tag"] for e in elements]
    assert tags == ["markdown", "hr", "markdown", "markdown"]
    body, divider, label, content = elements
    assert body["element_id"] == ELEMENT_ID_BODY
    assert divider["element_id"] == ELEMENT_ID_DETAIL_DIVIDER
    assert label["element_id"] == ELEMENT_ID_DETAIL_LABEL
    assert "▸ Tool calls (4)" in label["content"]
    assert "grey" in label["content"]
    assert content["element_id"] == ELEMENT_ID_DETAIL_CONTENT
    assert content["content"] == "- Read foo.py"


def test_v2_detail_and_actions_combined_element_order() -> None:
    card = build_status_card_v2(
        title="t",
        body="b",
        detail=ExpandableSection(label="Detail", content="hidden info"),
        actions=[CardButton(text="X", value={})],
    )
    tags = [e["tag"] for e in card["body"]["elements"]]
    # body, detail_divider, detail_label, detail_content, actions_divider, actions
    assert tags == ["markdown", "hr", "markdown", "markdown", "hr", "column_set"]


def test_v2_text_card_has_no_header_no_streaming() -> None:
    """One-shot notices: no header, no streaming_mode (the card has no
    server-side updates planned)."""
    card = build_text_card_v2(body="just a note")
    assert card["schema"] == "2.0"
    assert "header" not in card
    assert "streaming_mode" not in card["config"]
    elements = card["body"]["elements"]
    assert elements == [
        {"tag": "markdown", "element_id": ELEMENT_ID_BODY, "content": "just a note"},
    ]


def test_v2_restart_notice_card_is_grey_and_actionless() -> None:
    card = build_restart_notice_card_v2()
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "grey"
    assert "已重启" in card["header"]["title"]["content"]
    # No interactive widgets — restart is informational.
    assert not any(e.get("tag") == "column_set" for e in card["body"]["elements"])
    assert not any(e.get("tag") == "button" for e in card["body"]["elements"])
    # Terminal state ⇒ streaming_mode False.
    assert card["config"]["streaming_mode"] is False


def test_v2_does_not_pre_normalize_markdown_in_body() -> None:
    """v2's markdown element renders GFM headings natively — the legacy
    pre-normalize pass (`#` → `**`) is intentionally NOT applied here. If
    Phase 3 still wants to normalize, it must do so before calling the
    builder."""
    raw = "## Heading\n- item"
    card = build_status_card_v2(title="t", body=raw)
    assert card["body"]["elements"][0]["content"] == raw
