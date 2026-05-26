"""TmuxManager — thin subprocess wrapper around the `tmux` CLI.

Design constraints (see DESIGN.md §4.2):
  - **Only place** that shells out to `tmux`. Higher layers must go through this.
  - subprocess, not libtmux — fewer deps, full control over arg list, easier to test.
  - Window IDs (`@0`, `@12`, …) are the canonical handle. Names are display-only.
  - send_text vs send_key are intentionally separate APIs:
      * send_text  — literal mode (`-l`), Claude TUI safe (text + 500ms + Enter)
      * send_key   — tmux key resolution (`C-c`, `Escape`, `Tab`, `BTab`, …)

Errors:
  - All non-zero tmux exits raise :class:`TmuxError` with the full stderr.
  - The "tmux binary missing" case is a separate `TmuxError` so callers can
    surface a clean install-tmux hint.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass

# Format string for list-windows / new-window. Pipe-delimited because
# tmux names cannot contain `|` so it's a safe field separator.
_WINDOW_FORMAT = "#{window_id}|#{window_name}|#{pane_current_path}|#{pane_id}"

# Delay between literal text send and the subsequent Enter, in seconds.
# Claude Code's TUI batches stdin reads — without this gap, the Enter arrives
# in the same read() as the text and is interpreted as a newline rather than
# "submit". 500ms is the value ccgram converged on after empirical tuning.
_ENTER_DELAY_S = 0.5


class TmuxError(RuntimeError):
    """A `tmux` subprocess call failed or the binary is missing."""


@dataclass(frozen=True)
class WindowInfo:
    """Snapshot of a tmux window's identity. Keyed by `window_id`."""

    session: str
    window_id: str  # "@0", "@12", ...
    name: str
    cwd: str
    pane_id: str  # "%0", "%5", ...

    @property
    def target(self) -> str:
        """tmux target string usable with `-t`."""
        return f"{self.session}:{self.window_id}"


class TmuxManager:
    """Manage one tmux server session — windows live, send keys, capture output.

    A manager is bound to exactly one tmux session name. Multiple managers in
    the same process pointing at different sessions are fine; tmux itself
    handles isolation.
    """

    def __init__(self, session_name: str, *, command_timeout_s: float = 10.0) -> None:
        self.session_name = session_name
        self._timeout_s = command_timeout_s

    # ------------------------------------------------------------------ helpers

    def _tmux(self, *args: str, input_text: str | None = None) -> str:
        """Run `tmux <args>` and return stdout. Raise :class:`TmuxError` on failure."""
        if shutil.which("tmux") is None:
            raise TmuxError("`tmux` not found in PATH — install tmux first.")
        cmd = ["tmux", *args]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                input=input_text,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise TmuxError(f"tmux command timed out: {' '.join(cmd)}") from e
        except OSError as e:
            raise TmuxError(f"tmux command failed to spawn: {e}") from e

        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise TmuxError(f"tmux {' '.join(args)} (exit {result.returncode}): {stderr}")
        return result.stdout

    # ------------------------------------------------------------------ session

    def server_running(self) -> bool:
        """Return True if a tmux server process is alive on this host."""
        if shutil.which("tmux") is None:
            return False
        result = subprocess.run(
            ["tmux", "list-sessions"],
            capture_output=True,
            text=True,
            timeout=self._timeout_s,
            check=False,
        )
        # exit 1 with "no server running" is the canonical "no server" signal
        return result.returncode == 0

    def session_exists(self) -> bool:
        """Return True if our `session_name` exists in the running tmux server."""
        if not self.server_running():
            return False
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name],
            capture_output=True,
            text=True,
            timeout=self._timeout_s,
            check=False,
        )
        return result.returncode == 0

    def ensure_session(self) -> None:
        """Create the session in detached mode if it doesn't exist yet.

        Idempotent. tmux requires every session to host at least one window,
        so we explicitly name that mandatory placeholder ``_idle`` — pocket-cc
        never touches it, but the prefix makes its purpose obvious when an
        operator runs ``tmux a -t pocket-cc`` and sees it next to the real
        ``chat-<display_name>`` windows. Real work goes through :meth:`new_window`.
        """
        if self.session_exists():
            return
        self._tmux(
            "new-session", "-d", "-s", self.session_name, "-n", "_idle", "-x", "200", "-y", "50"
        )

    def kill_session(self) -> None:
        """Kill the entire session (all windows). No-op if it doesn't exist."""
        if not self.session_exists():
            return
        self._tmux("kill-session", "-t", self.session_name)

    # ------------------------------------------------------------------ windows

    def new_window(
        self,
        *,
        name: str,
        cwd: str | None = None,
        command: str | None = None,
    ) -> WindowInfo:
        """Create a new window in this session and return its info.

        Args:
            name: Display name (also used as initial window_name; not a handle).
            cwd: Optional working directory. Falls back to tmux default.
            command: Optional shell command to run instead of the default shell.
        """
        self.ensure_session()
        args: list[str] = [
            "new-window",
            "-t",
            f"{self.session_name}:",
            "-n",
            name,
            "-P",
            "-F",
            _WINDOW_FORMAT,
        ]
        if cwd:
            args += ["-c", cwd]
        if command:
            args.append(command)
        out = self._tmux(*args).strip()
        return self._parse_window_line(out)

    def list_windows(self) -> list[WindowInfo]:
        """List all windows in this session. Empty list if session doesn't exist."""
        if not self.session_exists():
            return []
        out = self._tmux("list-windows", "-t", self.session_name, "-F", _WINDOW_FORMAT)
        return [self._parse_window_line(line) for line in out.splitlines() if line.strip()]

    def find_window_by_id(self, window_id: str) -> WindowInfo | None:
        """Lookup by tmux window_id (e.g. `@5`). Returns None if not found."""
        return next(
            (w for w in self.list_windows() if w.window_id == window_id),
            None,
        )

    def find_window_by_name(self, name: str) -> WindowInfo | None:
        """Lookup by display name. If multiple windows share a name, the first wins."""
        return next(
            (w for w in self.list_windows() if w.name == name),
            None,
        )

    def window_exists(self, window_id: str) -> bool:
        return self.find_window_by_id(window_id) is not None

    def kill_window(self, window_id: str) -> None:
        """Kill the window. No-op if it's already gone."""
        if not self.window_exists(window_id):
            return
        self._tmux("kill-window", "-t", f"{self.session_name}:{window_id}")

    def rename_window(self, window_id: str, new_name: str) -> None:
        self._tmux("rename-window", "-t", f"{self.session_name}:{window_id}", new_name)

    # ------------------------------------------------------------------ I/O

    def send_text(
        self,
        window_id: str,
        text: str,
        *,
        with_enter: bool = True,
        enter_delay_s: float = _ENTER_DELAY_S,
    ) -> None:
        """Send literal text to a window's active pane.

        Uses tmux's `-l` (literal) mode — special key names like `C-c` are
        sent as the characters "C-c", not interpreted. Use :meth:`send_key`
        for actual key sequences.

        When ``with_enter`` is True, sleeps ``enter_delay_s`` before sending
        Enter — required for Claude TUI to register the input as "submit".
        See module docstring _ENTER_DELAY_S note.
        """
        target = f"{self.session_name}:{window_id}"
        self._tmux("send-keys", "-t", target, "-l", text)
        if with_enter:
            if enter_delay_s > 0:
                time.sleep(enter_delay_s)
            self._tmux("send-keys", "-t", target, "Enter")

    def send_key(self, window_id: str, key: str) -> None:
        """Send a tmux-interpreted key sequence.

        Examples: `C-c`, `Escape`, `Tab`, `BTab`, `BSpace`, `Up`, `Down`, `Enter`.
        Does **not** sleep — special keys are not part of the Claude TUI batching trap.
        """
        target = f"{self.session_name}:{window_id}"
        self._tmux("send-keys", "-t", target, key)

    def capture_pane(
        self,
        window_id: str,
        *,
        lines: int | None = None,
        include_history: bool = False,
    ) -> str:
        """Capture the current visible pane content as plain text.

        Args:
            window_id: Window to capture from.
            lines: If set, capture only the last ``lines`` lines.
                When ``include_history=False`` (default), this is bounded by
                the visible viewport.
            include_history: If True, capture also scrollback history.

        Returns:
            Plain text (ANSI escapes stripped — tmux strips them by default
            unless `-e` is passed; we never pass `-e`). Empty string if the
            window doesn't exist.
        """
        if not self.window_exists(window_id):
            return ""
        target = f"{self.session_name}:{window_id}"
        args: list[str] = ["capture-pane", "-p", "-t", target]
        if include_history:
            args += ["-S", "-"]  # start from top of history
        elif lines is not None:
            args += ["-S", f"-{lines}"]
        return self._tmux(*args)

    # ------------------------------------------------------------------ internal

    def _parse_window_line(self, line: str) -> WindowInfo:
        parts = line.rstrip("\n").split("|", 3)
        if len(parts) != 4:
            raise TmuxError(f"unexpected list-windows row: {line!r}")
        window_id, name, cwd, pane_id = parts
        return WindowInfo(
            session=self.session_name,
            window_id=window_id,
            name=name,
            cwd=cwd,
            pane_id=pane_id,
        )
