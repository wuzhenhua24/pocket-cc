"""Unit tests for bootstrap.restore_bindings — the M2-持久化 step2 entrypoint.

Exercises the three-way restore decision (window gone / window alive / orphan
card patched) using a real StateStore on disk and small in-memory fakes for
tmux and Lark. We test the free function (not Pocketcc) so the test never
has to construct a real LarkOapiClient / event loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import ANY

from pocket_cc.app.bootstrap import restore_bindings
from pocket_cc.app.persistence import Registry, StateStore
from pocket_cc.lark.client import FakeLarkClient, LarkApiError
from pocket_cc.tmux import TmuxError, WindowInfo

# ----------------------------------------------------------------- fakes


@dataclass
class _FakeTmux:
    """Just enough TmuxManager surface for restore_bindings — find_window_by_id."""

    windows: dict[str, WindowInfo] = field(default_factory=dict)
    raise_on_lookup: TmuxError | None = None

    def find_window_by_id(self, window_id: str) -> WindowInfo | None:
        if self.raise_on_lookup is not None:
            raise self.raise_on_lookup
        return self.windows.get(window_id)


def _win(window_id: str, name: str = "chat-x", cwd: str = "/tmp/wsp") -> WindowInfo:
    return WindowInfo(
        session="pocket-cc",
        window_id=window_id,
        name=name,
        cwd=cwd,
        pane_id="%1",
    )


# Common payload helper — writing JSON by hand keeps the test honest about
# schema shape (assertions can't pass by accident if the StateStore writer
# drifts; the read side is independently exercised).
def _write_state(path: Path, *, bindings: dict[str, Any]) -> None:
    path.write_text(json.dumps({"version": 2, "bindings": bindings}), encoding="utf-8")


def _entry(
    *,
    open_id: str = "ou_user1",
    window_id: str = "@5",
    window_name: str = "chat-abc",
    cwd: str = "/tmp/wsp",
    created_at: float = 1700000000.0,
    excluded: list[str] | None = None,
    mode: str = "default",
    active_card: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "open_id": open_id,
        "window_id": window_id,
        "window_name": window_name,
        "cwd": cwd,
        "created_at": created_at,
        "excluded_transcripts": excluded or [],
        "current_mode": mode,
        "active_card": active_card,
    }


# =================================================================== tests


def test_restore_noop_when_state_file_missing(tmp_path: Path) -> None:
    registry = Registry()
    store = StateStore(tmp_path / "state.json", registry)
    tmux = _FakeTmux()
    lark = FakeLarkClient()

    # Should not raise, should not register anything, should not create state file.
    restore_bindings(tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    assert len(registry) == 0
    assert lark.patches == []
    assert not (tmp_path / "state.json").exists()


def test_restore_attaches_binding_when_window_alive(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        bindings={"chat-1": _entry(window_id="@5", cwd="/tmp/wsp", mode="acceptEdits")},
    )
    registry = Registry()
    store = StateStore(state_path, registry)
    tmux = _FakeTmux(windows={"@5": _win("@5", name="chat-restored", cwd="/tmp/wsp")})
    lark = FakeLarkClient()

    restore_bindings(tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    assert len(registry) == 1
    binding = registry.get("chat-1")
    assert binding is not None
    assert binding.open_id == "ou_user1"
    assert binding.window.window_id == "@5"
    assert binding.cwd == Path("/tmp/wsp")
    assert binding.current_mode == "acceptEdits"
    # No active_card → no orphan patch.
    assert lark.patches == []


def test_restore_drops_binding_when_window_gone(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(state_path, bindings={"chat-1": _entry(window_id="@99")})
    registry = Registry()
    store = StateStore(state_path, registry)
    tmux = _FakeTmux(windows={})  # @99 not present
    lark = FakeLarkClient()

    restore_bindings(tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    assert len(registry) == 0
    # Snapshot must be rewritten to reflect the drop — a subsequent restart
    # mustn't try to re-attach the same dead window.
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["bindings"] == {}


def test_restore_drops_binding_when_tmux_lookup_raises(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(state_path, bindings={"chat-1": _entry(window_id="@5")})
    registry = Registry()
    store = StateStore(state_path, registry)
    tmux = _FakeTmux(raise_on_lookup=TmuxError("tmux not running"))
    lark = FakeLarkClient()

    # Restore swallows tmux failures; one bad lookup must not crash startup.
    restore_bindings(tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    assert len(registry) == 0


def test_restore_patches_orphan_card_and_clears_pointer(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        bindings={
            "chat-1": _entry(
                window_id="@5",
                active_card={"message_id": "om_orphan", "is_continuation": True},
            )
        },
    )
    registry = Registry()
    store = StateStore(state_path, registry)
    tmux = _FakeTmux(windows={"@5": _win("@5")})
    lark = FakeLarkClient()

    restore_bindings(tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    # Exactly one patch, against the orphaned message_id, with a cancelled card.
    assert len(lark.patches) == 1
    patch = lark.patches[0]
    assert patch.message_id == "om_orphan"
    assert patch.card["header"]["template"] == "grey"
    assert "已重启" in patch.card["header"]["title"]["content"]

    # State file was rewritten with active_card cleared (current_turn is None
    # on the restored binding, so StateStore's snapshot drops the pointer).
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["bindings"]["chat-1"]["active_card"] is None


def test_restore_keeps_binding_even_when_orphan_patch_fails(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        bindings={
            "chat-1": _entry(
                window_id="@5", active_card={"message_id": "om_dead", "is_continuation": False}
            )
        },
    )
    registry = Registry()
    store = StateStore(state_path, registry)
    tmux = _FakeTmux(windows={"@5": _win("@5")})
    lark = _RaisingPatchLark()  # simulates Lark API rejecting the patch

    restore_bindings(tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    # Binding is still attached — orphan patch is best-effort, the user still
    # gets a working chat.
    assert registry.get("chat-1") is not None


def test_restore_drops_bindings_individually_on_malformed_entries(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    bad_no_open_id = _entry(window_id="@6")
    del bad_no_open_id["open_id"]
    _write_state(
        state_path,
        bindings={
            "chat-ok": _entry(window_id="@5"),
            "chat-no-window-id": {"cwd": "/tmp"},  # missing window_id
            "chat-no-open-id": bad_no_open_id,  # missing open_id → can't route
            "chat-not-a-dict": "string instead of object",  # type: ignore[dict-item]
        },
    )
    registry = Registry()
    store = StateStore(state_path, registry)
    tmux = _FakeTmux(windows={"@5": _win("@5"), "@6": _win("@6")})
    lark = FakeLarkClient()

    restore_bindings(tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    # Only the good entry survives; bad ones are skipped with a warning, not
    # aborted.
    assert {b.chat_id for b in registry.all()} == {"chat-ok"}


def test_restore_handles_multiple_bindings_partial_alive(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        bindings={
            "chat-a": _entry(window_id="@1"),
            "chat-b": _entry(window_id="@2"),
            "chat-c": _entry(window_id="@3"),
        },
    )
    registry = Registry()
    store = StateStore(state_path, registry)
    # Only @1 and @3 survive in tmux; @2's window is dead.
    tmux = _FakeTmux(windows={"@1": _win("@1"), "@3": _win("@3")})
    lark = FakeLarkClient()

    restore_bindings(tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    assert set(b.chat_id for b in registry.all()) == {"chat-a", "chat-c"}
    # State file pruned to reflect the drop.
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(data["bindings"]) == {"chat-a", "chat-c"}


def test_restore_skips_on_corrupt_state_file(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json", encoding="utf-8")
    registry = Registry()
    store = StateStore(state_path, registry)
    tmux = _FakeTmux()
    lark = FakeLarkClient()

    # StateStore.load returns None → restore is a no-op (no patch, no save).
    restore_bindings(tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    assert len(registry) == 0
    assert lark.patches == []


# ----------------------------------------------------------------- helpers


class _RaisingPatchLark(FakeLarkClient):
    """FakeLarkClient variant whose patch_card always raises — for testing the
    orphan-patch failure path."""

    def patch_card(self, message_id: str, card: dict[str, Any]) -> None:
        raise LarkApiError(code=1234, msg="patch denied")


# Touch ANY so the import is used (mypy --strict otherwise complains; we
# keep the import handy for future expansion of these tests).
_ = ANY
