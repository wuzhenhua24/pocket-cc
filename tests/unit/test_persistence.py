"""Unit tests for app/persistence.py — in-memory Registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pocket_cc.app.persistence import ChatBinding, Registry, TurnState

# We don't need real tmux/lark/card_stream/accumulator here — Registry only
# stores references. A trivial stand-in is enough to assert the lookup paths.


@dataclass
class _StubWindow:
    session: str = "stub"
    window_id: str = "@0"
    name: str = "stub"
    cwd: str = "/tmp"
    pane_id: str = "%0"


def _stub_binding(chat_id: str) -> ChatBinding:
    return ChatBinding(chat_id=chat_id, window=_StubWindow(), cwd=Path("/tmp"))  # type: ignore[arg-type]


def _stub_turn(message_id: str) -> TurnState:
    return TurnState(
        card_message_id=message_id,
        card_stream=_FakeAny(),  # type: ignore[arg-type]
        accumulator=_FakeAny(),  # type: ignore[arg-type]
    )


class _FakeAny:
    """Stand-in for typed dependencies the Registry doesn't actually touch."""

    def __getattr__(self, name: str) -> Any:
        return self


def test_get_returns_none_for_unknown_chat() -> None:
    reg = Registry()
    assert reg.get("nope") is None


def test_set_then_get_roundtrips() -> None:
    reg = Registry()
    b = _stub_binding("chat-a")
    reg.set(b)
    assert reg.get("chat-a") is b


def test_set_overwrites_existing_binding() -> None:
    reg = Registry()
    first = _stub_binding("chat-a")
    second = _stub_binding("chat-a")
    reg.set(first)
    reg.set(second)
    assert reg.get("chat-a") is second


def test_remove_returns_removed_binding() -> None:
    reg = Registry()
    b = _stub_binding("chat-a")
    reg.set(b)
    assert reg.remove("chat-a") is b
    assert reg.get("chat-a") is None
    # idempotent
    assert reg.remove("chat-a") is None


def test_find_by_card_message_id_active_turn() -> None:
    reg = Registry()
    a = _stub_binding("chat-a")
    a.current_turn = _stub_turn("om_card_a")
    b = _stub_binding("chat-b")
    b.current_turn = _stub_turn("om_card_b")
    reg.set(a)
    reg.set(b)
    assert reg.find_by_card_message_id("om_card_a") is a
    assert reg.find_by_card_message_id("om_card_b") is b
    assert reg.find_by_card_message_id("om_other") is None


def test_find_by_card_message_id_ignores_bindings_without_active_turn() -> None:
    reg = Registry()
    binding = _stub_binding("chat-x")
    reg.set(binding)  # no current_turn
    assert reg.find_by_card_message_id("om_any") is None


def test_all_and_len() -> None:
    reg = Registry()
    assert len(reg) == 0
    assert reg.all() == []
    a = _stub_binding("a")
    b = _stub_binding("b")
    reg.set(a)
    reg.set(b)
    assert len(reg) == 2
    bindings = reg.all()
    assert {bind.chat_id for bind in bindings} == {"a", "b"}


def test_iter_works() -> None:
    reg = Registry()
    reg.set(_stub_binding("a"))
    reg.set(_stub_binding("b"))
    assert {b.chat_id for b in reg} == {"a", "b"}
