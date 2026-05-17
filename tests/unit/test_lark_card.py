"""Unit tests for lark/card.py — pure-function shape assertions."""

from __future__ import annotations

from pocket_cc.lark.card import (
    DEFAULT_RUNNING_ACTIONS,
    CardButton,
    ExpandableSection,
    build_status_card,
    build_text_card,
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


def test_default_running_actions_shape() -> None:
    assert all(isinstance(b, CardButton) for b in DEFAULT_RUNNING_ACTIONS)
    cancel = next(b for b in DEFAULT_RUNNING_ACTIONS if b.value.get("action") == "cancel")
    assert cancel.style == "danger"
    # key buttons carry a `key` field so the relay can translate to tmux keys
    keys = [b.value["key"] for b in DEFAULT_RUNNING_ACTIONS if b.value.get("action") == "key"]
    assert "Escape" in keys
    assert "BTab" in keys
