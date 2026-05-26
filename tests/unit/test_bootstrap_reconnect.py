"""Unit tests for bootstrap's WS reconnect-lifecycle observers.

These callbacks fire from the SDK's asyncio loop thread when the WS drops
and again when it re-establishes. The bootstrap layer's job is just to log
the gap (with structured ``extra`` fields) so an operator can correlate "I
sent a message and nothing happened" with the disconnect window.

Constructing a real :class:`Pocketcc` is cheap (no network, no tmux is
touched until ``start()``) — we use a minimal Config so we can exercise the
two private callbacks directly. The methods themselves are tiny; the value
of these tests is asserting the *log payload* — that's the operator contract.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING

from pocket_cc.app.bootstrap import Pocketcc
from pocket_cc.app.config import Config, User

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _minimal_config(workspace: Path) -> Config:
    return Config(
        app_id="cli_test",
        app_secret="secret",
        lark_domain="https://open.feishu.cn",
        users=MappingProxyType(
            {
                "ou_user1": User(open_id="ou_user1", workspace=workspace, display_name="test"),
            }
        ),
        claude_command="claude",
        tmux_session="pocket-cc-test",
        patch_interval_s=10.0,
        transcript_poll_s=0.5,
        events_poll_s=0.5,
        pane_poll_s=1.0,
    )


def test_ws_reconnecting_logs_warning_and_starts_clock(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """on_reconnecting fires once when the SDK decides a connection is gone
    and begins retrying. We must log a WARNING (it's a real-but-recoverable
    outage signal an operator wants surfaced) and arm the downtime clock so
    the paired reconnected callback can compute the gap."""
    app = Pocketcc(_minimal_config(tmp_path))
    assert app._ws_disconnect_started_at is None

    with caplog.at_level(logging.WARNING, logger="pocket_cc.app.bootstrap"):
        app._handle_ws_reconnecting()

    assert app._ws_disconnect_started_at is not None
    records = [r for r in caplog.records if r.getMessage() == "ws reconnecting"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].event == "ws_reconnecting"  # type: ignore[attr-defined]


def test_ws_reconnected_logs_downtime_and_clears_clock(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """on_reconnected logs the downtime so operators can correlate user
    reports of dropped messages with WS outage windows. The clock is cleared
    after so a subsequent disconnect starts a fresh measurement."""
    import pocket_cc.app.bootstrap as bootstrap_module

    # Deterministic monotonic clock so the assertion on downtime_s is exact.
    clock = iter([1000.0, 1037.42])
    monkeypatch.setattr(bootstrap_module.time, "monotonic", lambda: next(clock))

    app = Pocketcc(_minimal_config(tmp_path))
    app._handle_ws_reconnecting()  # consumes 1000.0
    with caplog.at_level(logging.INFO, logger="pocket_cc.app.bootstrap"):
        app._handle_ws_reconnected()  # consumes 1037.42

    assert app._ws_disconnect_started_at is None  # cleared
    records = [r for r in caplog.records if r.getMessage() == "ws reconnected"]
    assert len(records) == 1
    assert records[0].event == "ws_reconnected"  # type: ignore[attr-defined]
    assert records[0].downtime_s == 37.42  # type: ignore[attr-defined]


def test_ws_reconnected_without_prior_reconnecting_reports_none_downtime(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Edge case: if the SDK fires reconnected without us seeing a prior
    reconnecting (test setup oddity, or the SDK fires the first reconnect
    callback before our handler is wired), we still log — just with
    ``downtime_s=None`` so dashboards can spot the anomaly instead of
    silently dropping the event."""
    app = Pocketcc(_minimal_config(tmp_path))
    assert app._ws_disconnect_started_at is None

    with caplog.at_level(logging.INFO, logger="pocket_cc.app.bootstrap"):
        app._handle_ws_reconnected()

    records = [r for r in caplog.records if r.getMessage() == "ws reconnected"]
    assert len(records) == 1
    assert records[0].downtime_s is None  # type: ignore[attr-defined]


def test_reconnect_handlers_registered_on_event_loop(tmp_path: Path) -> None:
    """Pocketcc.__init__ must wire its callbacks onto LarkEventLoop's
    reconnect setters — otherwise ``start()`` won't propagate them to
    ws.Client and the observability is dark in production."""
    app = Pocketcc(_minimal_config(tmp_path))
    # Bound methods compare equal (==) when wrapping the same function + same
    # instance, even though each attribute access creates a fresh wrapper, so
    # ``is`` would always be False here.
    assert app._loop._on_reconnecting == app._handle_ws_reconnecting
    assert app._loop._on_reconnected == app._handle_ws_reconnected
