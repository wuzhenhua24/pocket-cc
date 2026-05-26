"""Log-level discrimination for Lark send failures in input.py.

Best-effort send sites (unauth notice, busy notice, pane dump) don't
retry — but the *log level* must reflect the error taxonomy so operators
can tell "chat exited (normal)" from "bot lost access (fix me)".
"""

from __future__ import annotations

import logging
from pathlib import Path  # noqa: TC003 — used in test signatures
from types import MappingProxyType
from typing import Any, cast

import pytest

from pocket_cc.app.config import Config, User
from pocket_cc.app.persistence import Registry
from pocket_cc.lark.client import FakeLarkClient, LarkApiError
from pocket_cc.lark.event_loop import IncomingMessage
from pocket_cc.relay.input import InputRouter
from pocket_cc.relay.turn_controller import TurnController
from pocket_cc.tmux import WindowInfo


# ----------------------------------------------------------------- minimal fixtures
# Re-implemented locally (instead of imported from test_input_router) so a
# refactor of that file's internal helpers doesn't drag this suite along.


class _FailingLark(FakeLarkClient):
    """FakeLarkClient that raises a chosen LarkApiError from send_text."""

    def __init__(self, err: LarkApiError) -> None:
        super().__init__()
        self._err = err

    def send_text(self, chat_id: str, text: str) -> str:  # type: ignore[override]
        raise self._err


class _NoopTmux:
    """Tmux stub — not exercised on the unauth path (no window creation)."""

    def ensure_session(self) -> None: ...
    def new_window(self, *, name: str, cwd: str | None = None, command: str | None = None) -> WindowInfo:  # noqa: ARG002
        return WindowInfo(session="s", window_id="@1", name=name, cwd=cwd or "/", pane_id="%1")
    def send_text(self, window_id: str, text: str, *, with_enter: bool = True) -> None: ...
    def send_key(self, window_id: str, key: str) -> None: ...
    def capture_pane(self, window_id: str, *, lines: int | None = None, include_history: bool = False) -> str:  # noqa: ARG002
        return ""


def _make_config(workspace: Path) -> Config:
    users = {"ou_allowed": User(open_id="ou_allowed", workspace=workspace, display_name="t")}
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


def _make_router(lark: FakeLarkClient, config: Config) -> InputRouter:
    registry = Registry()
    tmux = _NoopTmux()

    def controller_for(binding: Any) -> TurnController:
        return TurnController(binding=binding, lark=cast("Any", lark), config=config)

    return InputRouter(
        config=config,
        tmux=cast("Any", tmux),
        lark=lark,
        registry=registry,
        controller_for=controller_for,
    )


def _unauth_message() -> IncomingMessage:
    return IncomingMessage(
        message_id="om_msg_test",
        chat_id="oc_chat1",
        chat_type="p2p",
        sender_open_id="ou_evil",  # not in whitelist → unauth-notice path
        message_type="text",
        text="hi",
        raw_content='{"text":"hi"}',
    )


# ----------------------------------------------------------------- tests


@pytest.mark.parametrize(
    ("raw_code", "expected_level"),
    [
        (230002, logging.INFO),     # TARGET_REVOKED — chat is gone, normal
        (99991663, logging.ERROR),  # PERMISSION_DENIED — bot scope broken
        (230001, logging.ERROR),    # FORMAT_ERROR — malformed body, real bug
        (11020, logging.WARNING),   # RATE_LIMITED — transient
        (424242, logging.WARNING),  # UNKNOWN — fall through
    ],
)
def test_unauth_notice_logs_at_kind_appropriate_level(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    raw_code: int,
    expected_level: int,
) -> None:
    err = LarkApiError(code=raw_code, msg="boom", log_id="lg_xyz")
    lark = _FailingLark(err)
    router = _make_router(lark, _make_config(tmp_path))

    caplog.set_level(logging.DEBUG, logger="pocket_cc.relay.input")
    router.handle_message(_unauth_message())

    matches = [
        r for r in caplog.records
        if r.name == "pocket_cc.relay.input"
        and "denial" in r.message
    ]
    assert matches, "expected a denial-failure log record"
    rec = matches[-1]
    assert rec.levelno == expected_level, (
        f"code={raw_code}: expected level {expected_level}, got {rec.levelno}"
    )
    # Structured fields the helper attaches — log_id makes Lark-side debugging
    # possible without grepping the exception text.
    assert getattr(rec, "code", None) == raw_code
    assert getattr(rec, "log_id", None) == "lg_xyz"
