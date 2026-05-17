"""Unit tests for claude/hooks.py — install / uninstall / status / receive."""

from __future__ import annotations

import io
import json
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

from pocket_cc.claude.hooks import (
    HOOK_EVENTS,
    all_installed,
    hook_status,
    install_hooks,
    receive_event,
    uninstall_hooks,
)

if TYPE_CHECKING:
    from pathlib import Path


# ------------------------------------------------------------------- install


def test_install_into_empty_settings(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    result = install_hooks(settings_path=settings)

    assert settings.exists()
    data = json.loads(settings.read_text())
    assert "hooks" in data
    for event in HOOK_EVENTS:
        assert event in data["hooks"]
        entries = data["hooks"][event]
        assert len(entries) == 1
        assert entries[0]["pocket_cc"] is True
        assert "pocket_cc.cli hook receive" in entries[0]["hooks"][0]["command"]
        assert event in entries[0]["hooks"][0]["command"]
        assert result[event] is True


def test_install_is_idempotent(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    install_hooks(settings_path=settings)
    second = install_hooks(settings_path=settings)
    # Nothing should have changed the second time
    for event in HOOK_EVENTS:
        assert second[event] is False


def test_install_preserves_foreign_hook_entries(tmp_path: Path) -> None:
    """The user may have other hooks registered — we must not stomp them."""
    settings = tmp_path / "settings.json"
    foreign = {
        "matcher": "",
        "hooks": [{"type": "command", "command": "user-script.sh"}],
    }
    settings.write_text(json.dumps({"hooks": {"Stop": [foreign]}}))

    install_hooks(settings_path=settings)

    data = json.loads(settings.read_text())
    stop_entries = data["hooks"]["Stop"]
    # foreign entry still there + our entry
    assert foreign in stop_entries
    assert any(isinstance(e, dict) and e.get("pocket_cc") for e in stop_entries)


def test_install_preserves_other_top_level_keys(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark", "model": "claude-opus-4-7"}))

    install_hooks(settings_path=settings)

    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert data["model"] == "claude-opus-4-7"
    assert "hooks" in data


# ----------------------------------------------------------------- uninstall


def test_uninstall_removes_only_pocket_cc_entries(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    foreign = {"matcher": "", "hooks": [{"type": "command", "command": "user.sh"}]}
    settings.write_text(json.dumps({"hooks": {"Stop": [foreign]}}))

    install_hooks(settings_path=settings)
    uninstall_hooks(settings_path=settings)

    data = json.loads(settings.read_text())
    # Foreign entry preserved; our entry gone; Stop event still present because
    # foreign entry remains.
    assert data["hooks"]["Stop"] == [foreign]


def test_uninstall_tidies_empty_event_keys(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    install_hooks(settings_path=settings)
    uninstall_hooks(settings_path=settings)

    data = json.loads(settings.read_text())
    # No hooks at all now, so the hooks key itself should be gone
    assert "hooks" not in data


def test_uninstall_on_missing_settings_is_noop(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    result = uninstall_hooks(settings_path=settings)
    assert all(v is False for v in result.values())
    # No file created
    assert not settings.exists()


def test_uninstall_idempotent(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    install_hooks(settings_path=settings)
    uninstall_hooks(settings_path=settings)
    second = uninstall_hooks(settings_path=settings)
    assert all(v is False for v in second.values())


# -------------------------------------------------------------------- status


def test_status_on_missing_settings(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    statuses = hook_status(settings_path=settings)
    assert {s.event for s in statuses} == set(HOOK_EVENTS)
    assert all(not s.installed for s in statuses)
    assert all(s.other_entries == 0 for s in statuses)


def test_status_after_install(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    install_hooks(settings_path=settings)
    statuses = hook_status(settings_path=settings)
    assert all(s.installed for s in statuses)


def test_status_counts_foreign_entries(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    foreign = {"matcher": "", "hooks": [{"type": "command", "command": "x"}]}
    settings.write_text(json.dumps({"hooks": {"Stop": [foreign, foreign]}}))

    statuses = {s.event: s for s in hook_status(settings_path=settings)}
    assert statuses["Stop"].installed is False
    assert statuses["Stop"].other_entries == 2


def test_all_installed_helper(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    assert all_installed(settings_path=settings) is False
    install_hooks(settings_path=settings)
    assert all_installed(settings_path=settings) is True
    uninstall_hooks(settings_path=settings)
    assert all_installed(settings_path=settings) is False


# ----------------------------------------------------------------- receive


def test_receive_writes_event_to_log(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("POCKET_CC_DIR", str(tmp_path))

    payload = {
        "session_id": "uuid-x",
        "transcript_path": "/path.jsonl",
        "cwd": "/cwd",
        "hook_event_name": "Stop",
    }
    with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
        rc = receive_event("Stop")
    assert rc == 0
    assert log.exists()
    rec = json.loads(log.read_text())
    assert rec["event"] == "Stop"
    assert rec["session_id"] == "uuid-x"


def test_receive_empty_stdin_is_noop(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("POCKET_CC_DIR", str(tmp_path))
    with patch.object(sys, "stdin", io.StringIO("")):
        rc = receive_event("Stop")
    assert rc == 0
    assert not (tmp_path / "events.jsonl").exists()


def test_receive_invalid_json_returns_zero(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("POCKET_CC_DIR", str(tmp_path))
    with patch.object(sys, "stdin", io.StringIO("{not json")):
        rc = receive_event("Stop")
    assert rc == 0
    # Bad input → silent skip, nothing written
    assert not (tmp_path / "events.jsonl").exists()


def test_receive_non_dict_payload_returns_zero(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("POCKET_CC_DIR", str(tmp_path))
    with patch.object(sys, "stdin", io.StringIO("[1,2,3]")):
        rc = receive_event("Stop")
    assert rc == 0
    assert not (tmp_path / "events.jsonl").exists()
