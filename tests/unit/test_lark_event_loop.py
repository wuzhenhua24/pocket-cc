"""Unit tests for lark/event_loop.py — field extraction from fake SDK events.

We don't drive the real `lark.ws.Client` here; instead we feed `_dispatch_*`
fake objects that quack like the SDK's P2* types. This verifies the field
plumbing without network I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pocket_cc.lark.event_loop import (
    CardAction,
    IncomingMessage,
    LarkEventLoop,
    _to_card_action,
    _to_incoming_message,
)

# ---------------------------------------------------------- fake SDK shapes


@dataclass
class FakeSenderId:
    open_id: str | None


@dataclass
class FakeSender:
    sender_id: FakeSenderId | None


@dataclass
class FakeMessage:
    message_id: str | None
    chat_id: str | None
    chat_type: str | None
    message_type: str | None
    content: str | None


@dataclass
class FakeMsgEvent:
    message: FakeMessage | None
    sender: FakeSender | None


@dataclass
class FakeMsgEnvelope:
    event: FakeMsgEvent | None


@dataclass
class FakeOperator:
    open_id: str | None


@dataclass
class FakeAction:
    value: dict[str, Any] | None
    tag: str | None


@dataclass
class FakeContext:
    open_message_id: str | None
    open_chat_id: str | None


@dataclass
class FakeCardEvent:
    operator: FakeOperator | None
    action: FakeAction | None
    context: FakeContext | None
    token: str | None


@dataclass
class FakeCardEnvelope:
    event: FakeCardEvent | None


# ---------------------------------------------------------- IncomingMessage


def _msg_envelope(
    *,
    message_id: str = "om_x",
    chat_id: str = "oc_y",
    chat_type: str = "p2p",
    message_type: str = "text",
    content: str | None = None,
    sender_open_id: str | None = "ou_z",
) -> FakeMsgEnvelope:
    sender = FakeSender(sender_id=FakeSenderId(open_id=sender_open_id))
    return FakeMsgEnvelope(
        event=FakeMsgEvent(
            message=FakeMessage(
                message_id=message_id,
                chat_id=chat_id,
                chat_type=chat_type,
                message_type=message_type,
                content=content if content is not None else json.dumps({"text": "hi"}),
            ),
            sender=sender,
        )
    )


def test_text_content_extraction() -> None:
    env = _msg_envelope(content=json.dumps({"text": "你好"}))
    msg = _to_incoming_message(env)
    assert isinstance(msg, IncomingMessage)
    assert msg.text == "你好"
    assert msg.message_type == "text"
    assert msg.chat_id == "oc_y"
    assert msg.sender_open_id == "ou_z"


def test_non_text_message_keeps_raw_content_and_blank_text() -> None:
    env = _msg_envelope(message_type="image", content='{"image_key": "img_xxx"}')
    msg = _to_incoming_message(env)
    assert isinstance(msg, IncomingMessage)
    assert msg.text == ""
    assert "image_key" in msg.raw_content


def test_text_with_invalid_json_yields_empty_text() -> None:
    env = _msg_envelope(content="not json at all")
    msg = _to_incoming_message(env)
    assert isinstance(msg, IncomingMessage)
    assert msg.text == ""
    assert msg.raw_content == "not json at all"


def test_text_with_json_array_not_dict_yields_empty_text() -> None:
    env = _msg_envelope(content="[1,2,3]")
    msg = _to_incoming_message(env)
    assert isinstance(msg, IncomingMessage)
    assert msg.text == ""


def test_missing_sender_does_not_crash() -> None:
    env = FakeMsgEnvelope(
        event=FakeMsgEvent(
            message=FakeMessage(
                message_id="m",
                chat_id="c",
                chat_type="p2p",
                message_type="text",
                content='{"text":"hi"}',
            ),
            sender=None,
        )
    )
    msg = _to_incoming_message(env)
    assert isinstance(msg, IncomingMessage)
    assert msg.sender_open_id == ""


def test_missing_event_returns_none() -> None:
    assert _to_incoming_message(FakeMsgEnvelope(event=None)) is None


def test_missing_message_returns_none() -> None:
    env = FakeMsgEnvelope(event=FakeMsgEvent(message=None, sender=None))
    assert _to_incoming_message(env) is None


# ---------------------------------------------------------- CardAction


def _card_envelope(
    *,
    open_message_id: str = "om_card",
    open_chat_id: str = "oc_card",
    operator_open_id: str | None = "ou_user",
    value: dict[str, Any] | None = None,
    tag: str = "button",
    token: str = "tok_xxx",
) -> FakeCardEnvelope:
    return FakeCardEnvelope(
        event=FakeCardEvent(
            operator=FakeOperator(open_id=operator_open_id),
            action=FakeAction(value=value if value is not None else {"action": "ping"}, tag=tag),
            context=FakeContext(open_message_id=open_message_id, open_chat_id=open_chat_id),
            token=token,
        )
    )


def test_card_action_extracts_all_fields() -> None:
    env = _card_envelope(value={"action": "cancel", "key": "C-c"})
    action = _to_card_action(env)
    assert isinstance(action, CardAction)
    assert action.message_id == "om_card"
    assert action.chat_id == "oc_card"
    assert action.sender_open_id == "ou_user"
    assert action.token == "tok_xxx"
    assert action.tag == "button"
    assert action.value == {"action": "cancel", "key": "C-c"}


def test_card_action_non_dict_value_becomes_empty_dict() -> None:
    env = FakeCardEnvelope(
        event=FakeCardEvent(
            operator=FakeOperator(open_id="o"),
            action=FakeAction(value="not_a_dict", tag="button"),  # type: ignore[arg-type]
            context=FakeContext(open_message_id="m", open_chat_id="c"),
            token="t",
        )
    )
    action = _to_card_action(env)
    assert isinstance(action, CardAction)
    assert action.value == {}


def test_card_action_missing_event_returns_none() -> None:
    assert _to_card_action(FakeCardEnvelope(event=None)) is None


def test_card_action_missing_context_uses_empty_strings() -> None:
    env = FakeCardEnvelope(
        event=FakeCardEvent(
            operator=FakeOperator(open_id="o"),
            action=FakeAction(value={"a": 1}, tag="button"),
            context=None,
            token="t",
        )
    )
    action = _to_card_action(env)
    assert isinstance(action, CardAction)
    assert action.message_id == ""
    assert action.chat_id == ""


# ---------------------------------------------------------- handler wiring


def test_dispatch_message_routes_to_registered_handler() -> None:
    loop = LarkEventLoop(app_id="x", app_secret="y")
    received: list[IncomingMessage] = []
    loop.on_message(received.append)
    loop._dispatch_message(_msg_envelope(content=json.dumps({"text": "go"})))
    assert len(received) == 1
    assert received[0].text == "go"


def test_dispatch_card_action_routes_to_registered_handler() -> None:
    loop = LarkEventLoop(app_id="x", app_secret="y")
    received: list[CardAction] = []

    def handler(a: CardAction) -> None:
        received.append(a)
        return None

    loop.on_card_action(handler)
    response = loop._dispatch_card_action(_card_envelope(value={"action": "ack"}))
    assert len(received) == 1
    assert received[0].value == {"action": "ack"}
    # Handler returned None → dispatcher returns None so the SDK ships its
    # default no-update response.
    assert response is None


def test_dispatch_card_action_handler_returning_dict_wraps_into_ws_response() -> None:
    """Handler that returns a card dict gets wrapped in
    P2CardActionTriggerResponse with ``card.type="raw"`` + ``card.data=<dict>``.
    This is the WS-return optimization: Lark renders the patched card from the
    same trigger frame, no follow-up REST PATCH. Verifies our wrapper shape
    matches the SDK schema so the JSON marshalled back to Lark is well-formed.
    """
    loop = LarkEventLoop(app_id="x", app_secret="y")
    sample_card = {"header": {"template": "blue"}, "elements": []}
    loop.on_card_action(lambda _a: sample_card)
    response = loop._dispatch_card_action(_card_envelope(value={"action": "waiting_response"}))
    assert response is not None
    assert response.card is not None
    assert response.card.type == "raw"
    assert response.card.data == sample_card


def test_dispatch_without_handler_does_not_crash() -> None:
    loop = LarkEventLoop(app_id="x", app_secret="y")
    # neither handler registered — calls should be silent no-ops
    loop._dispatch_message(_msg_envelope())
    loop._dispatch_card_action(_card_envelope())


def test_event_loop_construction_does_not_open_ws() -> None:
    loop = LarkEventLoop(app_id="x", app_secret="y")
    loop.on_message(lambda _m: None)
    loop.on_card_action(lambda _a: None)
    assert loop._ws is None
