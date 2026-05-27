"""Unit tests for app/persistence.py — in-memory Registry + on-disk StateStore."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pocket_cc.app.persistence import ChatBinding, Registry, StateStore, TurnState

# We don't need real tmux/lark/card_stream/accumulator here — Registry only
# stores references. A trivial stand-in is enough to assert the lookup paths.


@dataclass
class _StubWindow:
    session: str = "stub"
    window_id: str = "@0"
    name: str = "stub"
    cwd: str = "/tmp"
    pane_id: str = "%0"


def _stub_binding(chat_id: str, *, open_id: str = "ou_user1") -> ChatBinding:
    return ChatBinding(
        chat_id=chat_id,
        open_id=open_id,
        window=_StubWindow(),  # type: ignore[arg-type]
        cwd=Path("/tmp"),
    )


def _stub_turn(message_id: str, *, card_id: str = "card_stub") -> TurnState:
    return TurnState(
        card_id=card_id,
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


# ============================================================== StateStore


def _make_binding(
    chat_id: str,
    *,
    open_id: str = "ou_user1",
    window_id: str = "@7",
    window_name: str = "chat-abcdef",
    cwd: str = "/tmp/wsp",
    created_at: float = 1716624000.0,
    excluded: tuple[str, ...] = (),
    mode: str = "default",
) -> ChatBinding:
    """Binding with full control over the fields StateStore serializes.

    Going through ChatBinding (not a stub) so the test catches schema drift
    if a real field gets renamed.
    """
    binding = ChatBinding(
        chat_id=chat_id,
        open_id=open_id,
        window=_StubWindow(window_id=window_id, name=window_name, cwd=cwd),  # type: ignore[arg-type]
        cwd=Path(cwd),
        created_at=created_at,
        excluded_transcripts=frozenset(Path(p) for p in excluded),
        current_mode=mode,
    )
    return binding


def test_state_store_save_creates_file_with_versioned_envelope(tmp_path: Path) -> None:
    reg = Registry()
    reg.set(_make_binding("chat-1"))
    store = StateStore(tmp_path / "state.json", reg)

    store.save()

    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert set(data["bindings"]) == {"chat-1"}


def test_state_store_save_serializes_all_l1_fields(tmp_path: Path) -> None:
    reg = Registry()
    reg.set(
        _make_binding(
            "chat-1",
            window_id="@9",
            window_name="chat-zz",
            cwd="/tmp/wsp",
            created_at=1700000000.5,
            excluded=("/tmp/a.jsonl", "/tmp/b.jsonl"),
            mode="plan",
        )
    )
    store = StateStore(tmp_path / "state.json", reg)

    store.save()
    entry = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["bindings"]["chat-1"]

    assert entry["open_id"] == "ou_user1"
    assert entry["window_id"] == "@9"
    assert entry["window_name"] == "chat-zz"
    assert entry["cwd"] == "/tmp/wsp"
    assert entry["created_at"] == 1700000000.5
    assert entry["excluded_transcripts"] == ["/tmp/a.jsonl", "/tmp/b.jsonl"]
    assert entry["current_mode"] == "plan"
    assert entry["active_card"] is None


def test_state_store_active_card_present_when_turn_open(tmp_path: Path) -> None:
    reg = Registry()
    b = _make_binding("chat-1")
    b.current_turn = _stub_turn("om_card_xyz")
    b.current_turn.is_continuation = True
    reg.set(b)
    store = StateStore(tmp_path / "state.json", reg)

    store.save()
    entry = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["bindings"]["chat-1"]

    assert entry["active_card"] == {"message_id": "om_card_xyz", "is_continuation": True}


def test_state_store_active_card_cleared_after_turn_seal(tmp_path: Path) -> None:
    reg = Registry()
    b = _make_binding("chat-1")
    b.current_turn = _stub_turn("om_card_xyz")
    reg.set(b)
    store = StateStore(tmp_path / "state.json", reg)
    store.save()

    # Simulate a seal: drop the turn and save again.
    b.current_turn = None
    store.save()

    entry = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["bindings"]["chat-1"]
    assert entry["active_card"] is None


def test_state_store_save_overwrites_previous_snapshot(tmp_path: Path) -> None:
    reg = Registry()
    reg.set(_make_binding("chat-1", mode="default"))
    store = StateStore(tmp_path / "state.json", reg)
    store.save()

    # Mutate, save again — the new file fully replaces the old one (no
    # accumulation, no append).
    reg.get("chat-1").current_mode = "acceptEdits"  # type: ignore[union-attr]
    store.save()

    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert data["bindings"]["chat-1"]["current_mode"] == "acceptEdits"


def test_state_store_save_handles_multiple_bindings(tmp_path: Path) -> None:
    reg = Registry()
    reg.set(_make_binding("chat-a", window_id="@1"))
    reg.set(_make_binding("chat-b", window_id="@2"))
    store = StateStore(tmp_path / "state.json", reg)

    store.save()
    bindings = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["bindings"]
    assert set(bindings) == {"chat-a", "chat-b"}
    assert bindings["chat-a"]["window_id"] == "@1"
    assert bindings["chat-b"]["window_id"] == "@2"


def test_state_store_save_is_atomic_no_intermediate_partial_file(tmp_path: Path) -> None:
    """The target file should never contain partial JSON.

    We verify by writing many times concurrently; every read of the final
    path must parse as valid JSON (the os.replace rename guarantees this).
    """
    reg = Registry()
    reg.set(_make_binding("chat-1"))
    store = StateStore(tmp_path / "state.json", reg)

    errors: list[str] = []

    def writer() -> None:
        for _ in range(40):
            store.save()

    def reader() -> None:
        for _ in range(40):
            try:
                raw = (tmp_path / "state.json").read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            try:
                json.loads(raw)
            except json.JSONDecodeError as e:
                errors.append(str(e))

    threads = [threading.Thread(target=writer) for _ in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


def test_state_store_load_returns_none_when_missing(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json", Registry())
    assert store.load() is None


def test_state_store_load_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = StateStore(path, Registry())
    assert store.load() is None


def test_state_store_load_returns_none_on_version_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 99, "bindings": {}}), encoding="utf-8")
    store = StateStore(path, Registry())
    assert store.load() is None


def test_state_store_load_returns_parsed_dict_round_trip(tmp_path: Path) -> None:
    reg = Registry()
    reg.set(_make_binding("chat-1", mode="bypassPermissions"))
    store = StateStore(tmp_path / "state.json", reg)
    store.save()

    data = store.load()
    assert data is not None
    assert data["version"] == 2
    assert data["bindings"]["chat-1"]["current_mode"] == "bypassPermissions"


def test_state_store_save_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "does" / "not" / "exist" / "state.json"
    store = StateStore(nested, Registry())
    store.save()
    assert nested.exists()
