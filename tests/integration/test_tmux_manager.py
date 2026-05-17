"""Integration tests for TmuxManager — uses a real tmux server.

Each test gets its own uniquely-named session (uuid suffix) so parallel runs
and aborted runs don't collide. Sessions are torn down in fixture teardown.

Skipped automatically when `tmux` is not on PATH.
"""

from __future__ import annotations

import shutil
import time
import uuid
from typing import TYPE_CHECKING

import pytest

from pocket_cc.tmux import TmuxError, TmuxManager

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux binary not available")


@pytest.fixture
def tmux() -> Iterator[TmuxManager]:
    name = f"pcc-test-{uuid.uuid4().hex[:8]}"
    mgr = TmuxManager(name)
    try:
        yield mgr
    finally:
        try:
            mgr.kill_session()
        except TmuxError:
            pass


def test_session_lifecycle(tmux: TmuxManager) -> None:
    assert tmux.session_exists() is False
    tmux.ensure_session()
    assert tmux.session_exists() is True
    # idempotent
    tmux.ensure_session()
    assert tmux.session_exists() is True
    tmux.kill_session()
    assert tmux.session_exists() is False
    # idempotent
    tmux.kill_session()


def test_new_window_returns_well_formed_info(tmux: TmuxManager) -> None:
    win = tmux.new_window(name="alpha")
    assert win.session == tmux.session_name
    assert win.window_id.startswith("@")
    assert win.pane_id.startswith("%")
    assert win.name == "alpha"
    # tmux fills cwd from the launching process; just assert it's non-empty
    assert win.cwd != ""
    assert win.target == f"{tmux.session_name}:{win.window_id}"


def test_list_and_find_windows(tmux: TmuxManager) -> None:
    a = tmux.new_window(name="a")
    b = tmux.new_window(name="b")
    ids = {w.window_id for w in tmux.list_windows()}
    assert a.window_id in ids
    assert b.window_id in ids

    # Compare stable fields only — shell startup (rc files etc.) can chdir
    # the new pane after creation, so `cwd` can drift between new_window and
    # the next list_windows call. window_id / pane_id are immutable.
    found_a = tmux.find_window_by_id(a.window_id)
    assert found_a is not None
    assert found_a.window_id == a.window_id
    assert found_a.pane_id == a.pane_id
    assert found_a.name == a.name
    found_by_name = tmux.find_window_by_name("b")
    assert found_by_name is not None
    assert found_by_name.window_id == b.window_id

    assert tmux.find_window_by_id("@99999") is None
    assert tmux.find_window_by_name("does-not-exist") is None


def test_send_text_and_capture(tmux: TmuxManager) -> None:
    win = tmux.new_window(name="echoer")
    # let the shell settle so its prompt is rendered
    time.sleep(0.5)
    tmux.send_text(win.window_id, "echo POCKET-MARKER-XYZ")
    time.sleep(0.8)
    pane = tmux.capture_pane(win.window_id)
    assert "POCKET-MARKER-XYZ" in pane


def test_send_key_does_not_sleep(tmux: TmuxManager) -> None:
    win = tmux.new_window(name="keys")
    start = time.perf_counter()
    tmux.send_key(win.window_id, "C-c")  # no-op at shell prompt, fine
    elapsed = time.perf_counter() - start
    # send_text adds a 500ms sleep before Enter; send_key must not.
    assert elapsed < 0.3, f"send_key should be fast, took {elapsed:.2f}s"


def test_kill_window(tmux: TmuxManager) -> None:
    win = tmux.new_window(name="doomed")
    assert tmux.window_exists(win.window_id) is True
    tmux.kill_window(win.window_id)
    assert tmux.window_exists(win.window_id) is False
    # idempotent
    tmux.kill_window(win.window_id)


def test_rename_window(tmux: TmuxManager) -> None:
    win = tmux.new_window(name="oldname")
    tmux.rename_window(win.window_id, "newname")
    refreshed = tmux.find_window_by_id(win.window_id)
    assert refreshed is not None
    assert refreshed.name == "newname"


def test_capture_pane_on_missing_window_returns_empty(tmux: TmuxManager) -> None:
    tmux.ensure_session()
    assert tmux.capture_pane("@99999") == ""


def test_send_to_missing_window_raises(tmux: TmuxManager) -> None:
    tmux.ensure_session()
    with pytest.raises(TmuxError):
        tmux.send_text("@99999", "hello", with_enter=False)


def test_list_windows_empty_when_no_session() -> None:
    mgr = TmuxManager(f"pcc-never-{uuid.uuid4().hex[:8]}")
    assert mgr.list_windows() == []
