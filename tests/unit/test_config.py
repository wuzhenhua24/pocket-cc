"""Unit tests for app/config.py — focused on `users.toml` loading + validation.

The Lark-creds + env-var parsing paths are exercised indirectly through
`load()`; here we pin the new TOML schema's validation rules so a malformed
users config fails loudly at startup rather than silently routing all chats
into one workspace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pocket_cc.app.config import ConfigError, User, _load_users

if TYPE_CHECKING:
    from pathlib import Path

# ----------------------------------------------------------------- happy path


def test_load_users_happy_path(tmp_path: Path) -> None:
    ws_a = tmp_path / "alice"
    ws_b = tmp_path / "bob"
    ws_a.mkdir()
    ws_b.mkdir()
    users_toml = tmp_path / "users.toml"
    users_toml.write_text(
        f"""
[users.ou_alice]
workspace = "{ws_a}"
display_name = "alice"

[users.ou_bob]
workspace = "{ws_b}"
display_name = "bob"
""",
        encoding="utf-8",
    )

    users = _load_users(users_toml)

    assert set(users.keys()) == {"ou_alice", "ou_bob"}
    assert users["ou_alice"] == User(
        open_id="ou_alice", workspace=ws_a.resolve(), display_name="alice"
    )
    assert users["ou_bob"] == User(open_id="ou_bob", workspace=ws_b.resolve(), display_name="bob")


def test_load_users_strips_whitespace_in_display_name(tmp_path: Path) -> None:
    ws = tmp_path / "alice"
    ws.mkdir()
    users_toml = tmp_path / "users.toml"
    users_toml.write_text(
        f"""
[users.ou_alice]
workspace = "{ws}"
display_name = "  alice  "
""",
        encoding="utf-8",
    )

    users = _load_users(users_toml)

    assert users["ou_alice"].display_name == "alice"


def test_load_users_expanduser_and_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~` expansion + symlink resolution happens at load time so callers
    always see absolute paths."""
    home = tmp_path / "home"
    home.mkdir()
    ws = home / "alice"
    ws.mkdir()
    monkeypatch.setenv("HOME", str(home))
    users_toml = tmp_path / "users.toml"
    users_toml.write_text(
        """
[users.ou_alice]
workspace = "~/alice"
display_name = "alice"
""",
        encoding="utf-8",
    )

    users = _load_users(users_toml)

    assert users["ou_alice"].workspace == ws.resolve()


# ----------------------------------------------------------------- file-level


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="users config not found"):
        _load_users(tmp_path / "absent.toml")


def test_invalid_toml_raises(tmp_path: Path) -> None:
    users_toml = tmp_path / "users.toml"
    users_toml.write_text("this is = = not toml [[[", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        _load_users(users_toml)


def test_empty_users_table_raises(tmp_path: Path) -> None:
    users_toml = tmp_path / "users.toml"
    users_toml.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="at least one"):
        _load_users(users_toml)


def test_missing_users_section_raises(tmp_path: Path) -> None:
    users_toml = tmp_path / "users.toml"
    users_toml.write_text('[other]\nfoo = "bar"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="at least one"):
        _load_users(users_toml)


# --------------------------------------------------------------- entry-level


def test_missing_workspace_field_raises(tmp_path: Path) -> None:
    users_toml = tmp_path / "users.toml"
    users_toml.write_text(
        """
[users.ou_alice]
display_name = "alice"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="workspace is required"):
        _load_users(users_toml)


def test_empty_workspace_string_raises(tmp_path: Path) -> None:
    users_toml = tmp_path / "users.toml"
    users_toml.write_text(
        """
[users.ou_alice]
workspace = "  "
display_name = "alice"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="workspace is required"):
        _load_users(users_toml)


def test_missing_display_name_raises(tmp_path: Path) -> None:
    ws = tmp_path / "alice"
    ws.mkdir()
    users_toml = tmp_path / "users.toml"
    users_toml.write_text(
        f"""
[users.ou_alice]
workspace = "{ws}"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="display_name is required"):
        _load_users(users_toml)


def test_empty_display_name_raises(tmp_path: Path) -> None:
    ws = tmp_path / "alice"
    ws.mkdir()
    users_toml = tmp_path / "users.toml"
    users_toml.write_text(
        f"""
[users.ou_alice]
workspace = "{ws}"
display_name = "   "
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="display_name is required"):
        _load_users(users_toml)


# -------------------------------------------------------------- workspace fs


def test_workspace_must_exist(tmp_path: Path) -> None:
    users_toml = tmp_path / "users.toml"
    users_toml.write_text(
        f"""
[users.ou_alice]
workspace = "{tmp_path / "nonexistent"}"
display_name = "alice"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="does not exist"):
        _load_users(users_toml)


def test_workspace_must_be_directory(tmp_path: Path) -> None:
    ws_file = tmp_path / "not-a-dir"
    ws_file.write_text("oops")
    users_toml = tmp_path / "users.toml"
    users_toml.write_text(
        f"""
[users.ou_alice]
workspace = "{ws_file}"
display_name = "alice"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="not a directory"):
        _load_users(users_toml)


def test_workspace_must_be_writable(tmp_path: Path) -> None:
    ws = tmp_path / "readonly"
    ws.mkdir()
    ws.chmod(0o500)  # r-x, no write
    try:
        users_toml = tmp_path / "users.toml"
        users_toml.write_text(
            f"""
[users.ou_alice]
workspace = "{ws}"
display_name = "alice"
""",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="not writable"):
            _load_users(users_toml)
    finally:
        ws.chmod(0o700)  # restore so pytest cleanup can remove it


# ---------------------------------------------------------------- overlap


def test_duplicate_workspace_raises(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    users_toml = tmp_path / "users.toml"
    users_toml.write_text(
        f"""
[users.ou_alice]
workspace = "{shared}"
display_name = "alice"

[users.ou_bob]
workspace = "{shared}"
display_name = "bob"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="share workspace"):
        _load_users(users_toml)


def test_parent_child_overlap_raises(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    users_toml = tmp_path / "users.toml"
    users_toml.write_text(
        f"""
[users.ou_alice]
workspace = "{parent}"
display_name = "alice"

[users.ou_bob]
workspace = "{child}"
display_name = "bob"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="overlap"):
        _load_users(users_toml)
