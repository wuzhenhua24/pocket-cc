"""Unit tests for claude/session_index.py — cwd → active transcript path."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from pocket_cc.claude.session_index import (
    encode_cwd_loose,
    encode_cwd_strict,
    find_active_transcript,
    find_project_dir,
    snapshot_existing_transcripts,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_encode_cwd_strict_replaces_slashes_and_underscores() -> None:
    assert encode_cwd_strict("/Users/foo/bar_baz") == "-Users-foo-bar-baz"


def test_encode_cwd_loose_only_replaces_slashes() -> None:
    assert encode_cwd_loose("/Users/foo/bar_baz") == "-Users-foo-bar_baz"


def test_find_project_dir_prefers_strict_encoding(tmp_path: Path) -> None:
    cwd = "/proj/has_underscore"
    (tmp_path / "-proj-has-underscore").mkdir()  # strict match
    (tmp_path / "-proj-has_underscore").mkdir()  # loose match too

    found = find_project_dir(cwd, projects_dir=tmp_path)
    assert found is not None
    assert found.name == "-proj-has-underscore"  # strict wins


def test_find_project_dir_falls_back_to_loose(tmp_path: Path) -> None:
    cwd = "/proj/has_underscore"
    (tmp_path / "-proj-has_underscore").mkdir()  # only loose match

    found = find_project_dir(cwd, projects_dir=tmp_path)
    assert found is not None
    assert found.name == "-proj-has_underscore"


def test_find_project_dir_missing(tmp_path: Path) -> None:
    assert find_project_dir("/nowhere", projects_dir=tmp_path) is None


def test_find_active_transcript_returns_newest(tmp_path: Path) -> None:
    cwd = "/x/y"
    project = tmp_path / "-x-y"
    project.mkdir()
    older = project / "old.jsonl"
    newer = project / "new.jsonl"
    older.write_text("{}\n")
    time.sleep(0.01)
    newer.write_text("{}\n")

    found = find_active_transcript(cwd, projects_dir=tmp_path)
    assert found == newer


def test_find_active_transcript_returns_none_when_no_jsonl(tmp_path: Path) -> None:
    cwd = "/x/y"
    project = tmp_path / "-x-y"
    project.mkdir()
    (project / "not-a-jsonl.txt").write_text("nope")

    assert find_active_transcript(cwd, projects_dir=tmp_path) is None


def test_find_active_transcript_after_ts_excludes_old_files(tmp_path: Path) -> None:
    """M1-D-14: historical jsonls (modified before the binding started) must
    be skipped, otherwise old sessions get dumped into the current card."""
    cwd = "/x/y"
    project = tmp_path / "-x-y"
    project.mkdir()
    historical = project / "old-session.jsonl"
    fresh = project / "current-session.jsonl"

    historical.write_text("{}\n")
    # force the historical file to look like it's from 5 minutes ago
    five_min_ago = time.time() - 300
    os.utime(historical, (five_min_ago, five_min_ago))

    # binding "started" 10 seconds ago
    binding_started_at = time.time() - 10

    # fresh file written right now (after binding start)
    fresh.write_text("{}\n")

    found = find_active_transcript(cwd, projects_dir=tmp_path, after_ts=binding_started_at)
    assert found == fresh


def test_find_active_transcript_after_ts_returns_none_when_only_old(tmp_path: Path) -> None:
    cwd = "/x/y"
    project = tmp_path / "-x-y"
    project.mkdir()
    historical = project / "old.jsonl"
    historical.write_text("{}\n")
    five_min_ago = time.time() - 300
    os.utime(historical, (five_min_ago, five_min_ago))

    # binding started after the only available file → no transcript yet
    binding_started_at = time.time() - 1
    assert find_active_transcript(cwd, projects_dir=tmp_path, after_ts=binding_started_at) is None


def test_find_active_transcript_after_ts_picks_newest_among_eligible(tmp_path: Path) -> None:
    cwd = "/x/y"
    project = tmp_path / "-x-y"
    project.mkdir()

    binding_started_at = time.time() - 30

    # 3 files: one historical (excluded), two fresh
    historical = project / "old.jsonl"
    historical.write_text("{}\n")
    os.utime(historical, (binding_started_at - 60, binding_started_at - 60))

    fresh1 = project / "fresh1.jsonl"
    fresh1.write_text("{}\n")
    os.utime(fresh1, (binding_started_at + 10, binding_started_at + 10))

    fresh2 = project / "fresh2.jsonl"
    fresh2.write_text("{}\n")
    os.utime(fresh2, (binding_started_at + 20, binding_started_at + 20))

    found = find_active_transcript(cwd, projects_dir=tmp_path, after_ts=binding_started_at)
    assert found == fresh2  # newest of the eligible files


def test_find_active_transcript_no_projects_dir(tmp_path: Path) -> None:
    nowhere = tmp_path / "does-not-exist"
    assert find_active_transcript("/anywhere", projects_dir=nowhere) is None


# ----------------------------------------------------- snapshot_exclude (D-15)


def test_snapshot_existing_transcripts_returns_current_jsonls(tmp_path: Path) -> None:
    cwd = "/x/y"
    project = tmp_path / "-x-y"
    project.mkdir()
    a = project / "a.jsonl"
    b = project / "b.jsonl"
    a.write_text("{}\n")
    b.write_text("{}\n")
    (project / "not-jsonl.txt").write_text("ignored")

    snap = snapshot_existing_transcripts(cwd, projects_dir=tmp_path)
    assert snap == frozenset({a, b})


def test_snapshot_existing_transcripts_missing_project_dir_is_empty(tmp_path: Path) -> None:
    snap = snapshot_existing_transcripts("/never/seen", projects_dir=tmp_path)
    assert snap == frozenset()


def test_find_active_transcript_exclude_skips_concurrent_session(tmp_path: Path) -> None:
    """The headline D-15 scenario: a desktop Claude is mid-session writing
    to the same cwd. Its file keeps mtime-bumping. Our snapshot at binding
    creation must exclude it so we wait for *our* Claude's new jsonl."""
    cwd = "/x/y"
    project = tmp_path / "-x-y"
    project.mkdir()

    # Concurrent session — exists *before* binding and keeps growing
    desktop = project / "desktop-session.jsonl"
    desktop.write_text("{}\n")

    # Snapshot at binding-creation time
    excluded = snapshot_existing_transcripts(cwd, projects_dir=tmp_path)
    assert desktop in excluded

    # Desktop Claude keeps writing — mtime now newer than anything else
    time.sleep(0.01)
    desktop.write_text("{}\n{}\n")

    # Polling should return None — no eligible transcript yet
    found = find_active_transcript(cwd, projects_dir=tmp_path, exclude=excluded)
    assert found is None

    # Pocket-cc's Claude finally creates its own transcript
    ours = project / "ours-fresh.jsonl"
    ours.write_text("{}\n")
    found = find_active_transcript(cwd, projects_dir=tmp_path, exclude=excluded)
    assert found == ours


def test_find_active_transcript_exclude_combined_with_after_ts(tmp_path: Path) -> None:
    cwd = "/x/y"
    project = tmp_path / "-x-y"
    project.mkdir()

    binding_started_at = time.time() - 30

    excluded_recent = project / "concurrent.jsonl"
    excluded_recent.write_text("{}\n")  # exists at snapshot time

    excluded = snapshot_existing_transcripts(cwd, projects_dir=tmp_path)

    # A "stale" new file that managed to slip in but is way too old
    stale = project / "stale-new.jsonl"
    stale.write_text("{}\n")
    long_ago = binding_started_at - 600
    os.utime(stale, (long_ago, long_ago))

    # Our fresh file
    ours = project / "ours.jsonl"
    ours.write_text("{}\n")

    found = find_active_transcript(
        cwd,
        projects_dir=tmp_path,
        after_ts=binding_started_at,
        exclude=excluded,
    )
    assert found == ours
