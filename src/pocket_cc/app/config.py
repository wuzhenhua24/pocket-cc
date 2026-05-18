"""Runtime configuration loaded from `.env` + environment.

All values have sensible defaults except the Lark credentials (which must be
provided). Loading is split into `load()` (full config from env) and a few
small `_resolve_*` helpers that are independently testable.

Layout (see DESIGN.md §5):
  - Lark app credentials      → LARK_APP_ID / LARK_APP_SECRET / LARK_DOMAIN
  - Workspace root            → POCKET_CC_WORKSPACE (default ~/.pocket-cc/workspace)
  - Claude launch command     → POCKET_CC_CLAUDE_COMMAND
  - tmux session name         → POCKET_CC_TMUX_SESSION
  - User whitelist (open_ids) → POCKET_CC_USER_WHITELIST (comma separated)
  - Card patch throttle (s)   → POCKET_CC_PATCH_INTERVAL_S (default 1.5)
  - Transcript poll (s)       → POCKET_CC_TRANSCRIPT_POLL_S (default 0.5)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

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
    `events.jsonl` (Claude hook event log). Created on first access.
    """
    raw = os.environ.get("POCKET_CC_DIR")
    base = Path(raw).expanduser() if raw else Path.home() / ".pocket-cc"
    base.mkdir(parents=True, exist_ok=True)
    return base


def events_jsonl_path() -> Path:
    """Path to the append-only Claude-hooks event log."""
    return pocket_cc_dir() / "events.jsonl"


class ConfigError(RuntimeError):
    """Required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    """All runtime configuration. Treat as immutable after construction."""

    app_id: str
    app_secret: str
    lark_domain: str
    workspace_root: Path
    claude_command: str
    tmux_session: str
    user_whitelist: frozenset[str]
    patch_interval_s: float
    transcript_poll_s: float
    events_poll_s: float
    pane_poll_s: float
    claude_projects_dir: Path = field(default_factory=lambda: Path.home() / ".claude" / "projects")

    @property
    def is_whitelist_open(self) -> bool:
        """When the whitelist is empty, every user is allowed.

        Useful for first-run / self-use. Production deployments should set
        POCKET_CC_USER_WHITELIST explicitly.
        """
        return not self.user_whitelist


def load(env_path: str | os.PathLike[str] | None = ".env") -> Config:
    """Load Config from env + optional .env file.

    .env values are *not* overridden by existing process env (dotenv default).
    Raises ConfigError if LARK_APP_ID / LARK_APP_SECRET is missing.
    """
    if env_path is not None:
        load_dotenv(env_path)

    app_id = os.environ.get("LARK_APP_ID", "").strip()
    app_secret = os.environ.get("LARK_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise ConfigError("LARK_APP_ID and LARK_APP_SECRET must be set (in .env or environment).")

    workspace = _resolve_workspace(os.environ.get("POCKET_CC_WORKSPACE"))
    whitelist = _parse_whitelist(os.environ.get("POCKET_CC_USER_WHITELIST"))

    return Config(
        app_id=app_id,
        app_secret=app_secret,
        lark_domain=os.environ.get("LARK_DOMAIN", _DEFAULT_LARK_DOMAIN),
        workspace_root=workspace,
        claude_command=os.environ.get("POCKET_CC_CLAUDE_COMMAND", _DEFAULT_CLAUDE_COMMAND),
        tmux_session=os.environ.get("POCKET_CC_TMUX_SESSION", _DEFAULT_TMUX_SESSION),
        user_whitelist=whitelist,
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
    )


# -------------------------------------------------------------------- helpers


def _resolve_workspace(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".pocket-cc" / "workspace"


def _parse_whitelist(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def _parse_float(raw: str | None, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"invalid float in env: {raw!r}") from e
