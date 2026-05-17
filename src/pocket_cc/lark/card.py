"""Lark interactive-card templates.

Pure functions that build the card dict structure Lark's `/im/v1/messages`
endpoint accepts (`msg_type=interactive`, `content` is the JSON of this dict).
No I/O — `LarkClient` is responsible for actually sending it.

We use the **legacy** card schema (no top-level `schema: "2.0"`) because:
  - Lark's PATCH endpoint accepts both, but legacy renders identically across
    older and newer Lark client versions.
  - All elements we need (markdown, action+button, hr, expandable note) work
    in the legacy schema without quirks.

Schema docs: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/im-v1/message-card/overview
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

CardState = Literal["running", "done", "failed", "waiting"]

# state → (header template color, emoji prefix)
_STATE_STYLE: dict[CardState, tuple[str, str]] = {
    "running": ("blue", "⏳"),
    "done": ("green", "✅"),
    "failed": ("red", "❌"),
    "waiting": ("orange", "❓"),
}

ButtonStyle = Literal["default", "primary", "danger"]


@dataclass(frozen=True, slots=True)
class CardButton:
    """A clickable button rendered in a card's action row.

    The ``value`` dict is what comes back to us in
    `P2CardActionTrigger.event.action.value` when the user taps it — design
    your routing scheme around that field (e.g. `{"action": "cancel"}`).
    """

    text: str
    value: dict[str, Any]
    style: ButtonStyle = "default"


@dataclass(frozen=True, slots=True)
class ExpandableSection:
    """A collapsed section users can tap to expand. Useful for tool-call details."""

    label: str  # e.g. "Tool calls (4)"
    content: str  # markdown body


def build_status_card(
    *,
    title: str,
    body: str,
    state: CardState = "running",
    actions: list[CardButton] | None = None,
    detail: ExpandableSection | None = None,
) -> dict[str, Any]:
    """Build a status-style card with a colored header, body and optional buttons.

    Args:
        title: Header text (emoji prefix added automatically based on state).
        body: Main content. Markdown supported (bold, lists, code).
        state: Visual state — controls header color and emoji prefix.
        actions: Optional row of buttons (max 4 fits comfortably on mobile).
        detail: Optional expandable section for verbose information that
            shouldn't be in the always-visible body.

    Returns:
        A dict ready to be JSON-serialized into a Lark interactive message's
        ``content`` field.
    """
    color, emoji = _STATE_STYLE[state]
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": body},
    ]

    if detail is not None:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": f"▸ {detail.label}"}],
            }
        )
        elements.append({"tag": "markdown", "content": detail.content})

    if actions:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "action",
                "actions": [_render_button(b) for b in actions],
            }
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": f"{emoji} {title}"},
        },
        "elements": elements,
    }


def build_text_card(*, body: str) -> dict[str, Any]:
    """Build a minimal card with just markdown body — useful for one-off notices.

    Headerless, action-less. Use for things like 'session ended' confirmations.
    """
    return {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "markdown", "content": body}],
    }


# -------------------------------------------------------------------- helpers


def _render_button(button: CardButton) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": button.text},
        "type": button.style,
        "value": dict(button.value),  # defensive copy — Lark mutates this server-side
    }


# Re-export the default action set we'll need everywhere as a convenience.
# Keep this list short — every new button is one more route in the relay layer.
DEFAULT_RUNNING_ACTIONS: tuple[CardButton, ...] = (
    CardButton(text="⏹ 中断", value={"action": "cancel"}, style="danger"),
    CardButton(text="⎋ Esc", value={"action": "key", "key": "Escape"}),
    CardButton(text="⇧⭾ Mode", value={"action": "key", "key": "BTab"}),
    CardButton(text="📜 内容", value={"action": "show_pane"}),
)
