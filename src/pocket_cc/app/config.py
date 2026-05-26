"""Runtime configuration: Lark creds from env, users from `users.toml`.

Split:
  - **Lark creds + runtime knobs** come from environment / `.env`
    (LARK_APP_ID, LARK_APP_SECRET, LARK_DOMAIN, POCKET_CC_CLAUDE_COMMAND,
    POCKET_CC_TMUX_SESSION, POCKET_CC_PATCH_INTERVAL_S, ...).
  - **Users + their workspaces** come from a TOML file
    (`~/.pocket-cc/users.toml`, overridable via $POCKET_CC_USERS_FILE).

The user table is the whitelist: only `open_id`s listed there can talk to the
bot, and each new turn opens a tmux window with that user's configured
workspace as `cwd`. There is no global "default workspace" — every binding
resolves its cwd through the user table.

`users.toml` shape:

```toml
[users.ou_abc123]
workspace = "/home/haierops/workspace/alice"
display_name = "alice"

[users.ou_def456]
workspace = "/home/haierops/workspace/bob"
display_name = "bob"
```

Validation (raises :class:`ConfigError` at :func:`load`):
  - file must exist and parse as TOML
  - ``[users]`` must be a non-empty table
  - each entry needs non-empty ``open_id`` (key), ``workspace``, ``display_name``
  - each resolved ``workspace`` must exist, be a directory, and be writable
  - no two users may share a workspace, or have workspaces in a parent/child
    relationship (two Claudes in the same tree would race on `.claude/`
    transcripts and caches)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from collections.abc import Mapping

_DEFAULT_CLAUDE_COMMAND = "claude"
_DEFAULT_TMUX_SESSION = "pocket-cc"
_DEFAULT_PATCH_INTERVAL_S = 1.5
_DEFAULT_TRANSCRIPT_POLL_S = 0.5
_DEFAULT_EVENTS_POLL_S = 0.5
_DEFAULT_PANE_POLL_S = 1.0
_DEFAULT_LARK_DOMAIN = "https://open.feishu.cn"


def pocket_cc_dir() -> Path:
    """Runtime state directory: ~/.pocket-cc/ (or $POCKET_CC_DIR override).

    Used by everything that persists across process restarts — currently
    `events.jsonl` (Claude hook event log) and `users.toml`. Created on first
    access.
    """
    raw = os.environ.get("POCKET_CC_DIR")
    base = Path(raw).expanduser() if raw else Path.home() / ".pocket-cc"
    base.mkdir(parents=True, exist_ok=True)
    return base


def events_jsonl_path() -> Path:
    """Path to the append-only Claude-hooks event log."""
    return pocket_cc_dir() / "events.jsonl"


def users_toml_path() -> Path:
    """Path to the users config (default ~/.pocket-cc/users.toml).

    Override via $POCKET_CC_USERS_FILE so tests / per-deploy setups can point
    at a different location without symlinking.
    """
    raw = os.environ.get("POCKET_CC_USERS_FILE")
    if raw:
        return Path(raw).expanduser()
    return pocket_cc_dir() / "users.toml"


class ConfigError(RuntimeError):
    """Required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class User:
    """One authorized user + their dedicated workspace.

    `workspace` is the absolute, resolved path of the directory the bot will
    launch Claude in for this user's chats. `display_name` is a short,
    human-readable label used for tmux window names and logs (not for any
    routing decision).
    """

    open_id: str
    workspace: Path
    display_name: str


@dataclass(frozen=True, slots=True)
class Config:
    """All runtime configuration. Treat as immutable after construction."""

    app_id: str
    app_secret: str
    lark_domain: str
    users: Mapping[str, User]
    claude_command: str
    tmux_session: str
    patch_interval_s: float
    transcript_poll_s: float
    events_poll_s: float
    pane_poll_s: float
    # When False (default), the Lark card omits the "💭 思考链" detail section
    # so the projection stays close to what the tmux terminal shows (which
    # collapses thinking). The transcript still carries it — this is purely a
    # render toggle, flip POCKET_CC_SHOW_THINKING=1 to surface it.
    show_thinking: bool = False
    claude_projects_dir: Path = field(default_factory=lambda: Path.home() / ".claude" / "projects")


def load(env_path: str | os.PathLike[str] | None = ".env") -> Config:
    """Load Config from env + .env + users.toml.

    .env values are *not* overridden by existing process env (dotenv default).
    Raises ConfigError if LARK_APP_ID / LARK_APP_SECRET is missing or if
    users.toml is missing / invalid.
    """
    if env_path is not None:
        load_dotenv(env_path)

    app_id = os.environ.get("LARK_APP_ID", "").strip()
    app_secret = os.environ.get("LARK_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise ConfigError("LARK_APP_ID and LARK_APP_SECRET must be set (in .env or environment).")

    users = _load_users(users_toml_path())

    return Config(
        app_id=app_id,
        app_secret=app_secret,
        lark_domain=os.environ.get("LARK_DOMAIN", _DEFAULT_LARK_DOMAIN),
        users=users,
        claude_command=os.environ.get("POCKET_CC_CLAUDE_COMMAND", _DEFAULT_CLAUDE_COMMAND),
        tmux_session=os.environ.get("POCKET_CC_TMUX_SESSION", _DEFAULT_TMUX_SESSION),
        patch_interval_s=_parse_float(
            os.environ.get("POCKET_CC_PATCH_INTERVAL_S"), _DEFAULT_PATCH_INTERVAL_S
        ),
        transcript_poll_s=_parse_float(
            os.environ.get("POCKET_CC_TRANSCRIPT_POLL_S"), _DEFAULT_TRANSCRIPT_POLL_S
        ),
        events_poll_s=_parse_float(
            os.environ.get("POCKET_CC_EVENTS_POLL_S"), _DEFAULT_EVENTS_POLL_S
        ),
        pane_poll_s=_parse_float(os.environ.get("POCKET_CC_PANE_POLL_S"), _DEFAULT_PANE_POLL_S),
        show_thinking=_parse_bool(os.environ.get("POCKET_CC_SHOW_THINKING"), default=False),
    )


# -------------------------------------------------------------- users loading


def _load_users(path: Path) -> Mapping[str, User]:
    """Parse and validate users.toml. Returns an immutable view.

    All validation errors raise :class:`ConfigError` with a message that
    includes the offending open_id / path so the operator can fix the file.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise ConfigError(
            f"users config not found: {path}. Create it with at least one user "
            "entry (see DESIGN.md / .env.example for the schema)."
        ) from e
    except OSError as e:
        raise ConfigError(f"failed to read users config {path}: {e}") from e

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"users config {path} is not valid TOML: {e}") from e

    users_section = data.get("users")
    if not isinstance(users_section, dict) or not users_section:
        raise ConfigError(f"users config {path} must define at least one [users.<open_id>] entry.")

    parsed: dict[str, User] = {}
    for raw_open_id, entry in users_section.items():
        open_id = (raw_open_id or "").strip()
        if not open_id:
            raise ConfigError(f"users config {path}: empty open_id key.")
        if not isinstance(entry, dict):
            raise ConfigError(f"users config {path}: [users.{open_id}] must be a table.")
        workspace_raw = entry.get("workspace")
        if not isinstance(workspace_raw, str) or not workspace_raw.strip():
            raise ConfigError(
                f"users config {path}: [users.{open_id}].workspace is required and must be a non-empty string."
            )
        display_name_raw = entry.get("display_name")
        if not isinstance(display_name_raw, str) or not display_name_raw.strip():
            raise ConfigError(
                f"users config {path}: [users.{open_id}].display_name is required and must be a non-empty string."
            )

        workspace = Path(workspace_raw).expanduser().resolve()
        if not workspace.exists():
            raise ConfigError(
                f"users config {path}: workspace for {open_id} does not exist: {workspace}"
            )
        if not workspace.is_dir():
            raise ConfigError(
                f"users config {path}: workspace for {open_id} is not a directory: {workspace}"
            )
        if not os.access(workspace, os.W_OK):
            raise ConfigError(
                f"users config {path}: workspace for {open_id} is not writable: {workspace}"
            )

        parsed[open_id] = User(
            open_id=open_id,
            workspace=workspace,
            display_name=display_name_raw.strip(),
        )

    _check_workspace_overlap(parsed, path)

    return MappingProxyType(parsed)


def _check_workspace_overlap(users: dict[str, User], path: Path) -> None:
    """Reject duplicate workspaces and parent/child overlaps.

    Two Claude processes running in the same directory (or in nested
    directories) would race on `.claude/` transcripts/caches and accidentally
    pick up each other's session jsonls. The relationship is symmetric: A's
    workspace contains B's, or B's contains A's — either way it's banned.
    """
    items = list(users.items())
    for i, (a_id, a) in enumerate(items):
        for b_id, b in items[i + 1 :]:
            if a.workspace == b.workspace:
                raise ConfigError(
                    f"users config {path}: {a_id} and {b_id} share workspace "
                    f"{a.workspace}. Each user must have a dedicated directory."
                )
            if a.workspace.is_relative_to(b.workspace) or b.workspace.is_relative_to(a.workspace):
                raise ConfigError(
                    f"users config {path}: workspaces for {a_id} ({a.workspace}) "
                    f"and {b_id} ({b.workspace}) overlap (one contains the other). "
                    "Each user's workspace must be disjoint."
                )


# -------------------------------------------------------------------- helpers


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_float(raw: str | None, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"invalid float in env: {raw!r}") from e
