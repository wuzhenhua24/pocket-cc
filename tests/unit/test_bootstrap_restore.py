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
from types import MappingProxyType
from typing import Any
from unittest.mock import ANY

from pocket_cc.app.bootstrap import restore_bindings
from pocket_cc.app.config import Config, User
from pocket_cc.app.persistence import Registry, StateStore
from pocket_cc.lark.client import FakeLarkClient, LarkApiError
from pocket_cc.tmux import TmuxError, WindowInfo

# ----------------------------------------------------------------- fakes


@dataclass
class _FakeTmux:
    """Just enough TmuxManager surface for restore_bindings — find_window_by_id
    and kill_window (used by the reconcile-drop cleanup path)."""

    windows: dict[str, WindowInfo] = field(default_factory=dict)
    raise_on_lookup: TmuxError | None = None
    raise_on_kill: TmuxError | None = None
    killed: list[str] = field(default_factory=list)

    def find_window_by_id(self, window_id: str) -> WindowInfo | None:
        if self.raise_on_lookup is not None:
            raise self.raise_on_lookup
        return self.windows.get(window_id)

    def kill_window(self, window_id: str) -> None:
        self.killed.append(window_id)
        if self.raise_on_kill is not None:
            raise self.raise_on_kill


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
    # Schema v3: active_card carries `card_id` for cardkit-driven orphan
    # restart-notice updates (see persistence._SCHEMA_VERSION).
    path.write_text(json.dumps({"version": 3, "bindings": bindings}), encoding="utf-8")


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


def _config(
    *,
    open_id: str = "ou_user1",
    workspace: str = "/tmp/wsp",
    display_name: str = "test",
    extra_users: dict[str, str] | None = None,
) -> Config:
    """Build a Config directly (bypasses _load_users + its fs validation).

    Default matches `_entry`'s defaults so existing tests reconcile cleanly.
    Tests exercising the drop-on-mismatch paths pass a different open_id /
    workspace.
    """
    users: dict[str, User] = {
        open_id: User(open_id=open_id, workspace=Path(workspace), display_name=display_name),
    }
    if extra_users:
        for oid, ws in extra_users.items():
            users[oid] = User(open_id=oid, workspace=Path(ws), display_name=oid)
    return Config(
        app_id="cli_x",
        app_secret="secret",
        lark_domain="https://open.feishu.cn",
        users=MappingProxyType(users),
        claude_command="claude",
        tmux_session="pocket-cc-test",
        patch_interval_s=10.0,
        transcript_poll_s=0.5,
        events_poll_s=0.5,
        pane_poll_s=1.0,
    )


# =================================================================== tests


def test_restore_noop_when_state_file_missing(tmp_path: Path) -> None:
    registry = Registry()
    store = StateStore(tmp_path / "state.json", registry)
    tmux = _FakeTmux()
    lark = FakeLarkClient()

    # Should not raise, should not register anything, should not create state file.
    restore_bindings(config=_config(), tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

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

    restore_bindings(config=_config(), tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

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

    restore_bindings(config=_config(), tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

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
    restore_bindings(config=_config(), tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    assert len(registry) == 0


def test_restore_patches_orphan_card_and_clears_pointer(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        bindings={
            "chat-1": _entry(
                window_id="@5",
                active_card={
                    "card_id": "card_orphan",
                    "message_id": "om_orphan",
                    "is_continuation": True,
                },
            )
        },
    )
    registry = Registry()
    store = StateStore(state_path, registry)
    tmux = _FakeTmux(windows={"@5": _win("@5")})
    lark = FakeLarkClient()

    restore_bindings(config=_config(), tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    # Exactly one cardkit update, against the orphaned card_id, carrying the
    # restart-notice v2 card (grey header, ⏹ cancelled state, no streaming).
    assert len(lark.card_entity_updates) == 1
    update = lark.card_entity_updates[0]
    assert update.card_id == "card_orphan"
    assert update.card["header"]["template"] == "grey"
    assert "已重启" in update.card["header"]["title"]["content"]
    # Legacy IM PATCH should not be touched.
    assert lark.patches == []

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
                window_id="@5",
                active_card={
                    "card_id": "card_dead",
                    "message_id": "om_dead",
                    "is_continuation": False,
                },
            )
        },
    )
    registry = Registry()
    store = StateStore(state_path, registry)
    tmux = _FakeTmux(windows={"@5": _win("@5")})
    lark = _RaisingPatchLark()  # simulates Lark API rejecting the patch

    restore_bindings(config=_config(), tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

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

    restore_bindings(config=_config(), tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

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

    restore_bindings(config=_config(), tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

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
    restore_bindings(config=_config(), tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    assert len(registry) == 0
    assert lark.patches == []


# =========================================================== reconciliation


def test_restore_drops_when_open_id_no_longer_in_users(tmp_path: Path) -> None:
    """User was removed from users.toml between runs — privilege revoked.
    The binding is dropped, its tmux window killed (so the stale Claude stops),
    and any active card is patched with the restart notice."""
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        bindings={
            "chat-1": _entry(
                open_id="ou_evicted",
                window_id="@5",
                active_card={"card_id": "card_orphan", "message_id": "om_orphan", "is_continuation": False},
            ),
        },
    )
    registry = Registry()
    store = StateStore(state_path, registry)
    tmux = _FakeTmux(windows={"@5": _win("@5")})
    lark = FakeLarkClient()

    # Config knows about a *different* user, not ou_evicted.
    cfg = _config(open_id="ou_user1", workspace="/tmp/wsp")

    restore_bindings(config=cfg, tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    assert len(registry) == 0
    assert tmux.killed == ["@5"]
    assert len(lark.card_entity_updates) == 1
    assert lark.card_entity_updates[0].card_id == "card_orphan"
    # Disk snapshot now reflects the drop, so a second restart won't re-process it.
    assert json.loads(state_path.read_text(encoding="utf-8"))["bindings"] == {}


def test_restore_drops_when_workspace_changed_in_users_config(tmp_path: Path) -> None:
    """users.toml moved this user's workspace elsewhere — fresh restart in new
    cwd. Binding dropped, window killed, orphan card patched."""
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        bindings={
            "chat-1": _entry(
                open_id="ou_user1",
                window_id="@7",
                cwd="/tmp/old-workspace",
                active_card={"card_id": "card_orphan", "message_id": "om_orphan", "is_continuation": False},
            ),
        },
    )
    registry = Registry()
    store = StateStore(state_path, registry)
    tmux = _FakeTmux(windows={"@7": _win("@7", cwd="/tmp/old-workspace")})
    lark = FakeLarkClient()

    cfg = _config(open_id="ou_user1", workspace="/tmp/new-workspace")

    restore_bindings(config=cfg, tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    assert len(registry) == 0
    assert tmux.killed == ["@7"]
    assert len(lark.card_entity_updates) == 1
    assert lark.card_entity_updates[0].card_id == "card_orphan"


def test_restore_reconcile_drop_survives_tmux_kill_failure(tmp_path: Path) -> None:
    """If kill_window fails (tmux gone, window already dead, etc.) we still
    drop the binding and patch the orphan card — cleanup is best-effort."""
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        bindings={
            "chat-1": _entry(
                open_id="ou_evicted",
                window_id="@5",
                active_card={"card_id": "card_orphan", "message_id": "om_orphan", "is_continuation": False},
            ),
        },
    )
    registry = Registry()
    store = StateStore(state_path, registry)
    tmux = _FakeTmux(
        windows={"@5": _win("@5")},
        raise_on_kill=TmuxError("window already gone"),
    )
    lark = FakeLarkClient()

    cfg = _config(open_id="ou_user1", workspace="/tmp/wsp")
    restore_bindings(config=cfg, tmux=tmux, lark=lark, registry=registry, state_store=store)  # type: ignore[arg-type]

    # Binding is still dropped; orphan card is still patched despite kill failing.
    assert len(registry) == 0
    assert tmux.killed == ["@5"]  # we attempted
    assert len(lark.card_entity_updates) == 1


# ----------------------------------------------------------------- helpers


class _RaisingPatchLark(FakeLarkClient):
    """FakeLarkClient variant whose update_card_entity always raises —
    exercises the orphan-update failure path. (Named "Patch" historically;
    cardkit's whole-card replacement is conceptually the same operation
    that ``patch_card`` was doing in the legacy path.)"""

    def update_card_entity(
        self,
        card_id: str,
        card: dict[str, Any],
        *,
        sequence: int,
        uuid: str | None = None,
    ) -> None:
        raise LarkApiError(code=1234, msg="update denied")


# Touch ANY so the import is used (mypy --strict otherwise complains; we
# keep the import handy for future expansion of these tests).
_ = ANY
