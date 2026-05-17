"""Locate the active Claude Code transcript file for a given cwd.

Claude Code stores per-session transcripts at:

    ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl

The encoding of `<cwd>` to directory name is **not officially documented**.
Empirically:

    /Users/foo/Documents/gitpro/design_Notion
        → -Users-foo-Documents-gitpro-design-Notion

Both `/` and `_` are replaced with `-`. The result is *not* round-trippable
(two distinct cwds can collide), so Claude Code stores the real cwd inside
each JSONL header — but for our purposes (find which dir to read from)
the encoding suffices.

We probe a couple of encoding strategies in order, falling back as Claude's
rules may evolve:
  1. `/` and `_` both → `-`  (current empirical behavior)
  2. only `/` → `-`           (older ccgram-observed)

Use :func:`find_active_transcript` from the relay loop; it returns the path
of the most recently modified `.jsonl` under the matching directory, or
None if Claude has not started a session for this cwd yet.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_PROJECTS_DIR: Path = Path.home() / ".claude" / "projects"


def encode_cwd_strict(cwd: Path | str) -> str:
    """Encode cwd by replacing both `/` and `_` with `-` (current Claude behavior)."""
    return _encode(str(cwd), replace_underscore=True)


def encode_cwd_loose(cwd: Path | str) -> str:
    """Encode cwd by replacing only `/` with `-` (older Claude behavior)."""
    return _encode(str(cwd), replace_underscore=False)


def find_project_dir(cwd: Path | str, projects_dir: Path = DEFAULT_PROJECTS_DIR) -> Path | None:
    """Return the Claude projects subdirectory for this cwd, or None if missing.

    Tries the strict encoding first; falls back to loose to tolerate older
    Claude versions or future changes.
    """
    if not projects_dir.exists():
        return None
    for encoded in (encode_cwd_strict(cwd), encode_cwd_loose(cwd)):
        candidate = projects_dir / encoded
        if candidate.is_dir():
            return candidate
    return None


def snapshot_existing_transcripts(
    cwd: Path | str, projects_dir: Path = DEFAULT_PROJECTS_DIR
) -> frozenset[Path]:
    """Snapshot the set of `.jsonl` files that already exist under this cwd.

    Captured at binding-creation time. Passed back into
    :func:`find_active_transcript` as ``exclude`` so a concurrent Claude
    instance (e.g. user's desktop Claude) keeps writing its transcript in the
    same projects dir without us mistakenly adopting it as the active session.

    Returns an empty frozenset if the project dir doesn't exist yet — meaning
    no prior sessions exist for this cwd, so every future file is fair game.
    """
    project_dir = find_project_dir(cwd, projects_dir)
    if project_dir is None:
        return frozenset()
    return frozenset(project_dir.glob("*.jsonl"))


def find_active_transcript(
    cwd: Path | str,
    projects_dir: Path = DEFAULT_PROJECTS_DIR,
    *,
    after_ts: float | None = None,
    exclude: frozenset[Path] = frozenset(),
) -> Path | None:
    """Return the most recently modified `.jsonl` for this cwd, or None.

    "Most recently modified" is what we treat as the *active* session — Claude
    Code appends to the current session's transcript every time it emits a
    message, so its mtime is always the most recent under that directory.

    Args:
        cwd: Working directory Claude was launched with.
        projects_dir: Root of Claude's per-project transcript store.
        after_ts: If given, only `.jsonl` files modified strictly after this
            UNIX timestamp are considered. Defends against truly stale files
            (modified ages ago and never touched since).
        exclude: Files in this set are skipped unconditionally. The primary
            defense against a concurrent Claude session in the same cwd —
            snapshot the set of existing transcripts at binding creation
            (via :func:`snapshot_existing_transcripts`) so files written by
            the *other* Claude can keep mtime-bumping without confusing us.
            We only adopt files that didn't exist when we started.
    """
    project_dir = find_project_dir(cwd, projects_dir)
    if project_dir is None:
        return None
    candidates = [p for p in project_dir.glob("*.jsonl") if p not in exclude]
    if after_ts is not None:
        candidates = [p for p in candidates if p.stat().st_mtime > after_ts]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# -------------------------------------------------------------------- helpers


def _encode(s: str, *, replace_underscore: bool) -> str:
    out = s.replace("/", "-")
    if replace_underscore:
        out = out.replace("_", "-")
    return out
