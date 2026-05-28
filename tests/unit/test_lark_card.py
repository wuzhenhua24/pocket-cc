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
    """Heading conversion is idempotent — ``** ... **`` re-wraps cleanly
    on a second pass (the unwrap-then-rewrap logic in _heading_to_bold
    handles the already-bold case)."""
    src = "## hello\n### world\n"
    once = normalize_markdown_for_lark(src)
    twice = normalize_markdown_for_lark(once)
    assert once == twice


def test_normalize_leaves_tables_alone() -> None:
    """Phase 4-C moved table handling to markdown_body_to_v2_elements.
    normalize_markdown_for_lark only does heading conversion now — any
    pipe-table content passes through unchanged."""
    src = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    assert normalize_markdown_for_lark(src) == src


def test_normalize_leaves_non_table_pipes_alone() -> None:
    """A line with `|` but no separator-row underneath isn't a table.
    (Still passes through trivially — normalize no longer touches pipes
    at all, but the test stays as a regression guard for the heading
    pass not accidentally munging pipe-bearing text.)"""
    src = "you can use `a | b` syntax in shell pipes\nand `find . | grep foo`"
    assert normalize_markdown_for_lark(src) == src


# =========================================================================
# Schema 2.0 (cardkit) builders
# =========================================================================

from pocket_cc.lark.card import (  # noqa: E402  (grouped after normalize tests)
    ELEMENT_ID_ACTIONS,
    ELEMENT_ID_ACTIONS_DIVIDER,
    ELEMENT_ID_BODY,
    ELEMENT_ID_DETAIL_CONTENT,
    ELEMENT_ID_DETAIL_DIVIDER,
    ELEMENT_ID_DETAIL_LABEL,
    build_restart_notice_card_v2,
    build_status_card_v2,
    build_text_card_v2,
    markdown_body_to_v2_elements,
)


def _md_body(content: str) -> list[dict[str, object]]:
    """Single-markdown-element body convenience for tests that don't
    exercise the table-extraction path."""
    return [{"tag": "markdown", "element_id": ELEMENT_ID_BODY, "content": content}]


def test_v2_minimal_running_card_shape() -> None:
    card = build_status_card_v2(title="task A", body_elements=_md_body("hello world"))
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
        card = build_status_card_v2(title="t", body_elements=_md_body("b"), state=state)  # type: ignore[arg-type]
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
        card = build_status_card_v2(title="t", body_elements=_md_body("b"), state=state)  # type: ignore[arg-type]
        assert card["header"]["template"] == color, state
        assert card["header"]["title"]["content"] == f"{emoji} t", state
        assert card["config"]["summary"]["content"] == f"{emoji} t", state


def test_v2_body_element_id_is_stable() -> None:
    """Regression guard: CardStream hard-codes ELEMENT_ID_BODY as the
    streaming target. If this constant ever drifts away from what
    markdown_body_to_v2_elements emits for the first segment, streaming
    PUTs would silently target a phantom element id."""
    card = build_status_card_v2(title="t", body_elements=_md_body("x"))
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
    card = build_status_card_v2(title="t", body_elements=_md_body("b"), actions=actions)
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
        body_elements=_md_body("b"),
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
        body_elements=_md_body("b"),
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
        body_elements=_md_body("b"),
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


# ===================================================== markdown_body_to_v2_elements


def test_md_body_plain_text_is_single_markdown_element() -> None:
    """Common case: no tables → one markdown element with ELEMENT_ID_BODY."""
    out = markdown_body_to_v2_elements("hello world")
    assert len(out) == 1
    assert out[0] == {
        "tag": "markdown",
        "element_id": ELEMENT_ID_BODY,
        "content": "hello world",
    }


def test_md_body_empty_string_still_anchors_streaming_target() -> None:
    """Empty body still emits one markdown element so CardStream's fast
    path has a target to PUT to on the first streaming tick."""
    out = markdown_body_to_v2_elements("")
    assert out == [{"tag": "markdown", "element_id": ELEMENT_ID_BODY, "content": ""}]


def test_md_body_applies_heading_conversion() -> None:
    """v2 markdown renders GFM headings literally pending verification —
    we conservatively convert ## → ** as the rendering enters the
    builder."""
    out = markdown_body_to_v2_elements("## Result\nAll good.")
    assert len(out) == 1
    assert "**Result**" in out[0]["content"]
    assert "## " not in out[0]["content"]


def test_md_body_extracts_gfm_table_as_v2_table_element() -> None:
    src = (
        "Some intro text.\n"
        "\n"
        "| 模块 | 说明 |\n"
        "|---|---|\n"
        "| api | Backend |\n"
        "| ui  | Frontend |\n"
        "\n"
        "Wrap-up sentence."
    )
    out = markdown_body_to_v2_elements(src)
    tags = [e["tag"] for e in out]
    assert tags == ["markdown", "table", "markdown"]
    # Leading markdown carries the streaming target id
    assert out[0]["element_id"] == ELEMENT_ID_BODY
    assert "Some intro text." in out[0]["content"]
    # Table is a real v2 table element, not a markdown blob
    table = out[1]
    assert table["element_id"] == "body_1"
    assert table["page_size"] == 5
    assert [c["display_name"] for c in table["columns"]] == ["模块", "说明"]
    assert [c["data_type"] for c in table["columns"]] == ["text", "text"]
    assert table["rows"] == [
        {"col_0": "api", "col_1": "Backend"},
        {"col_0": "ui", "col_1": "Frontend"},
    ]
    # Trailing markdown gets a separate, auto-numbered id
    assert out[2]["element_id"] == "body_2"
    assert "Wrap-up" in out[2]["content"]


def test_md_body_table_with_inline_md_picks_lark_md_data_type() -> None:
    """The SDK's data_type auto-detect: any cell with bold / italic /
    inline code / link → ``lark_md`` for that column. Plain columns stay
    ``text``."""
    src = (
        "| 名称 | 文档 |\n"
        "|---|---|\n"
        "| api | [docs](http://x) |\n"
        "| ui  | **WIP**  |\n"
    )
    out = markdown_body_to_v2_elements(src)
    # First segment is empty leading markdown (table is at the start)
    table = next(e for e in out if e["tag"] == "table")
    assert [c["data_type"] for c in table["columns"]] == ["text", "lark_md"]


def test_md_body_leading_table_gets_empty_md_first() -> None:
    """When body starts with a table, we still emit a leading empty
    markdown element first — CardStream's fast-path target must always
    be a markdown element (the streaming PUT endpoint targets markdown
    elements only). Without this prefix the streaming layer couldn't
    keep growing text in front of the table."""
    src = "| h |\n|---|\n"  # one-column "table" — separator regex needs ≥2 cols
    # Single-column tables aren't recognized as GFM tables (intentional —
    # `|---|` separator regex requires ≥2 columns). They stay markdown.
    out = markdown_body_to_v2_elements(src)
    assert len(out) == 1
    assert out[0]["tag"] == "markdown"

    src = "| h1 | h2 |\n|---|---|\n| a | b |\n"
    out = markdown_body_to_v2_elements(src)
    assert [e["tag"] for e in out] == ["markdown", "table"]
    assert out[0]["element_id"] == ELEMENT_ID_BODY
    assert out[0]["content"] == ""  # leading empty markdown
    assert out[1]["element_id"] == "body_1"


def test_md_body_pads_short_rows_to_match_column_count() -> None:
    """Mismatched row widths must not crash the builder — pad with empty
    strings so cardkit's payload is well-formed."""
    src = (
        "| a | b | c |\n"
        "|---|---|---|\n"
        "| 1 | 2 |\n"  # short row
        "| 1 | 2 | 3 |\n"
    )
    out = markdown_body_to_v2_elements(src)
    table = next(e for e in out if e["tag"] == "table")
    assert table["rows"][0] == {"col_0": "1", "col_1": "2", "col_2": ""}
    assert table["rows"][1] == {"col_0": "1", "col_1": "2", "col_2": "3"}
