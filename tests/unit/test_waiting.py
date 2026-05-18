"""Unit tests for relay/waiting.py — data shape sanity."""

from __future__ import annotations

from pocket_cc.relay.waiting import (
    KeysResponse,
    TextResponse,
    WaitingFor,
    WaitingOption,
)


def test_text_response_holds_text() -> None:
    r = TextResponse(text="1")
    assert r.text == "1"


def test_keys_response_holds_sequence() -> None:
    r = KeysResponse(keys=("Down", "Down", "Enter"))
    assert r.keys == ("Down", "Down", "Enter")


def test_waiting_option_minimum_fields() -> None:
    opt = WaitingOption(label="Yes", response=TextResponse(text="1"))
    assert opt.label == "Yes"
    assert opt.description == ""
    assert isinstance(opt.response, TextResponse)


def test_waiting_option_with_description() -> None:
    opt = WaitingOption(
        label="Accept",
        response=TextResponse(text="y"),
        description="Accept this plan and proceed",
    )
    assert opt.description == "Accept this plan and proceed"


def test_waiting_for_defaults() -> None:
    w = WaitingFor(source="permission", question="Do you want to proceed?")
    assert w.options == ()
    assert w.allow_freeform_text is True
    assert w.fingerprint == ""


def test_waiting_for_full() -> None:
    w = WaitingFor(
        source="ask_user_question",
        question="网络环境？",
        options=(
            WaitingOption(label="出公网", response=TextResponse(text="1")),
            WaitingOption(label="隔离", response=TextResponse(text="2")),
        ),
        allow_freeform_text=False,
        fingerprint="toolu_abc",
    )
    assert w.source == "ask_user_question"
    assert len(w.options) == 2
    assert w.options[0].label == "出公网"
    assert w.allow_freeform_text is False
    assert w.fingerprint == "toolu_abc"


def test_response_is_union_of_text_and_keys() -> None:
    """Sanity: a WaitingOption can carry either response shape."""
    a = WaitingOption(label="Yes", response=TextResponse(text="y"))
    b = WaitingOption(label="Up", response=KeysResponse(keys=("Up", "Enter")))
    assert isinstance(a.response, TextResponse)
    assert isinstance(b.response, KeysResponse)
