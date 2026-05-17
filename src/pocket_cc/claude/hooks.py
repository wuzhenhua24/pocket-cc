"""Manage Claude Code hook registration in `~/.claude/settings.json`.

Three CLI-facing operations + one runtime-facing operation:

  - :func:`install_hooks`   — write our hook entries into settings.json (idempotent)
  - :func:`uninstall_hooks` — remove our hook entries
  - :func:`hook_status`     — report which events have our hook attached
  - :func:`receive_event`   — runtime receiver: read stdin JSON, append to events.jsonl

Marker convention:
  Each hook entry we install carries a `pocket_cc: true` flag so we can
  detect *our* entries without ambiguity (the user may have other hooks
  configured by hand). install/uninstall only touch entries with this flag.

Hooks installed:
  - SessionStart — lets pocket-cc bind the new session_id / transcript_path
                   to its waiting ChatBinding before any Stop arrives
  - Stop         — flips the active turn's card to ✅ done immediately
  - StopFailure  — flips it to ❌ failed
  - Notification — for future M2 use (waiting state)
  - SessionEnd   — for future cleanup

settings.json schema we touch:
    {
      "hooks": {
        "<EventName>": [
          { "matcher": "", "hooks": [{"type": "command", "command": "..."}] },
          ...
        ]
      }
    }

CLAUDE_CONFIG_DIR env var is respected for non-default Claude config locations.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pocket_cc.app.config import events_jsonl_path
from pocket_cc.claude.events import KNOWN_EVENTS, write_event

logger = logging.getLogger(__name__)

# Events we register hook entries for. Subset of KNOWN_EVENTS — SessionEnd is
# included because we want cleanup hooks fired even on graceful Claude exit.
HOOK_EVENTS: tuple[str, ...] = tuple(sorted(KNOWN_EVENTS))

# Marker baked into each hook entry we install. Used to find / remove only
# our entries, leaving any hand-configured user hooks alone.
_MARKER_KEY = "pocket_cc"


def claude_settings_path() -> Path:
    """Resolve `~/.claude/settings.json` honoring `CLAUDE_CONFIG_DIR`."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".claude"
    return base / "settings.json"


def hook_command_string() -> str:
    """Command Claude executes for each hook event.

    Uses the active Python interpreter + `pocket_cc.cli` so it works with both
    `uv run` development setups and properly-installed `pocket-cc` consoles.
    Adds `hook receive <event>` where Claude will substitute "$CLAUDE_HOOK_NAME"
    later? — No, the event name is fixed per-entry in settings.json, so we
    bake it in literally.
    """
    # Use sys.executable + -m so we don't depend on the `pocket-cc` console
    # script being on PATH (which it isn't under `uv run`).
    python = shutil.which("python") or sys.executable or "python"
    return f'"{python}" -m pocket_cc.cli hook receive'


@dataclass(frozen=True, slots=True)
class HookStatus:
    """Per-event installation state."""

    event: str
    installed: bool
    other_entries: int  # foreign (non-pocket-cc) entries on the same event


# --------------------------------------------------------------------- writes


def install_hooks(*, settings_path: Path | None = None) -> dict[str, bool]:
    """Install (or refresh) our hook entries for every event in HOOK_EVENTS.

    Idempotent — re-running replaces any prior pocket-cc entry on each event
    with the current command string (so version bumps automatically refresh).
    Other hooks (matcher / hooks the user configured by hand) are untouched.

    Returns a dict `{event: installed_now}` — True if we just added/updated
    an entry, False if it was already up-to-date.
    """
    path = settings_path or claude_settings_path()
    settings = _read_settings(path)
    hooks = settings.setdefault("hooks", {})
    command = hook_command_string()

    result: dict[str, bool] = {}
    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        new_entry = _build_entry(event, command)
        changed = _replace_pocket_entry(entries, new_entry)
        result[event] = changed

    _atomic_write(path, settings)
    return result


def uninstall_hooks(*, settings_path: Path | None = None) -> dict[str, bool]:
    """Remove our hook entries from every event. Returns `{event: removed_anything}`."""
    path = settings_path or claude_settings_path()
    if not path.exists():
        return {event: False for event in HOOK_EVENTS}
    settings = _read_settings(path)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return {event: False for event in HOOK_EVENTS}

    result: dict[str, bool] = {}
    for event in HOOK_EVENTS:
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            result[event] = False
            continue
        before = len(entries)
        hooks[event] = [e for e in entries if not (isinstance(e, dict) and e.get(_MARKER_KEY))]
        result[event] = len(hooks[event]) != before
        # Tidy: drop the event key if no entries remain (keeps settings.json clean)
        if not hooks[event]:
            hooks.pop(event, None)

    # Tidy: drop hooks key if empty
    if not hooks:
        settings.pop("hooks", None)

    _atomic_write(path, settings)
    return result


def hook_status(*, settings_path: Path | None = None) -> list[HookStatus]:
    """Report install state of each event we manage."""
    path = settings_path or claude_settings_path()
    if not path.exists():
        return [HookStatus(event=e, installed=False, other_entries=0) for e in HOOK_EVENTS]
    settings = _read_settings(path)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return [HookStatus(event=e, installed=False, other_entries=0) for e in HOOK_EVENTS]

    out: list[HookStatus] = []
    for event in HOOK_EVENTS:
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            out.append(HookStatus(event=event, installed=False, other_entries=0))
            continue
        ours = sum(1 for e in entries if isinstance(e, dict) and e.get(_MARKER_KEY))
        others = sum(1 for e in entries if isinstance(e, dict) and not e.get(_MARKER_KEY))
        out.append(HookStatus(event=event, installed=ours > 0, other_entries=others))
    return out


def all_installed(*, settings_path: Path | None = None) -> bool:
    """Convenience: True if every event we manage has our hook attached."""
    return all(s.installed for s in hook_status(settings_path=settings_path))


# -------------------------------------------------------------------- runtime


def receive_event(event_name: str) -> int:
    """Hook receiver — read JSON from stdin, append to events.jsonl. Return 0 always.

    Called by Claude Code with the hook payload on stdin. Must be fast and
    must not raise — any error gets logged and swallowed, since a non-zero
    exit can interfere with Claude's flow (depending on hook type).
    """
    try:
        raw = sys.stdin.read()
    except OSError:
        return 0
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("hook receiver: bad JSON on stdin", extra={"event": event_name})
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        write_event(events_jsonl_path(), event_name, payload)
    except OSError:
        # Disk full, perms, etc. Log and continue — never block Claude.
        logger.exception("hook receiver: failed to write events.jsonl")
    return 0


# -------------------------------------------------------------------- helpers


def _build_entry(event: str, command: str) -> dict[str, Any]:
    return {
        _MARKER_KEY: True,
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": f"{command} {event}",
            }
        ],
    }


def _replace_pocket_entry(entries: list[Any], new_entry: dict[str, Any]) -> bool:
    """Replace the first pocket-cc entry in ``entries`` (or append if absent).

    Returns True if entries was modified (added or content-changed).
    """
    for i, e in enumerate(entries):
        if isinstance(e, dict) and e.get(_MARKER_KEY):
            if e == new_entry:
                return False
            entries[i] = new_entry
            return True
    entries.append(new_entry)
    return True


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("settings.json unreadable, treating as empty", extra={"path": str(path)})
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
