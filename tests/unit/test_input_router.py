"""Unit tests for relay/input.py — message + card-action routing.

Uses FakeLarkClient for Lark and a hand-rolled FakeTmuxManager that records
calls and lets tests pre-seed `new_window` return values + force failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — used in test signatures
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from pocket_cc.app.config import Config, User
from pocket_cc.app.persistence import ChatBinding, Registry
from pocket_cc.lark.client import FakeLarkClient
from pocket_cc.lark.event_loop import CardAction, IncomingMessage
from pocket_cc.relay.input import InputRouter
from pocket_cc.relay.turn_controller import TurnController
from pocket_cc.relay.waiting import (
    KeysResponse,
    TextResponse,
    WaitingFor,
    WaitingOption,
)
from pocket_cc.tmux import TmuxError, WindowInfo

if TYPE_CHECKING:
    import pytest

# ----------------------------------------------------------------- fixtures


@dataclass
class _Call:
    method: str
    kwargs: dict[str, Any]


@dataclass
class FakeTmuxManager:
    new_window_return: WindowInfo = field(
        default_factory=lambda: WindowInfo(
            session="pocket-cc",
            window_id="@1",
            name="chat-x",
            cwd="/tmp",
            pane_id="%1",
        )
    )
    new_window_raises: TmuxError | None = None
    send_text_raises: TmuxError | None = None
    capture_pane_text: str = "fake pane output"
    calls: list[_Call] = field(default_factory=list)

    def ensure_session(self) -> None:
        self.calls.append(_Call("ensure_session", {}))

    def new_window(
        self, *, name: str, cwd: str | None = None, command: str | None = None
    ) -> WindowInfo:
        self.calls.append(_Call("new_window", {"name": name, "cwd": cwd, "command": command}))
        if self.new_window_raises:
            raise self.new_window_raises
        return self.new_window_return

    def send_text(self, window_id: str, text: str, *, with_enter: bool = True) -> None:
        self.calls.append(_Call("send_text", {"window_id": window_id, "text": text}))
        if self.send_text_raises:
            raise self.send_text_raises

    def send_key(self, window_id: str, key: str) -> None:
        self.calls.append(_Call("send_key", {"window_id": window_id, "key": key}))

    def capture_pane(
        self, window_id: str, *, lines: int | None = None, include_history: bool = False
    ) -> str:
        self.calls.append(_Call("capture_pane", {"window_id": window_id}))
        return self.capture_pane_text


def _make_config(
    *,
    workspace: Path,
    open_id: str = "ou_user1",
    extra_users: dict[str, Path] | None = None,
) -> Config:
    users: dict[str, User] = {
        open_id: User(open_id=open_id, workspace=workspace, display_name="test"),
    }
    if extra_users:
        for oid, ws in extra_users.items():
            users[oid] = User(open_id=oid, workspace=ws, display_name=oid)
    return Config(
        app_id="cli_x",
        app_secret="secret",
        lark_domain="https://open.feishu.cn",
        users=MappingProxyType(users),
        claude_command="claude --permission-mode bypassPermissions",
        tmux_session="pocket-cc-test",
        patch_interval_s=10.0,  # so the bg stream thread doesn't actually patch during tests
        transcript_poll_s=0.5,
        events_poll_s=0.5,
        pane_poll_s=1.0,
    )


def _make_router(
    *,
    tmux: FakeTmuxManager,
    lark: FakeLarkClient,
    registry: Registry,
    config: Config,
    controller_for: Any = None,
) -> InputRouter:
    # The controller is mandatory now; default to a real (caching) provider so
    # tests exercise the genuine turn-lifecycle paths unless they pass a spy.
    if controller_for is None:
        controller_for = _real_controller_for(lark, config)
    return InputRouter(
        config=config,
        tmux=cast("Any", tmux),
        lark=lark,
        registry=registry,
        controller_for=controller_for,
    )


def _real_controller_for(lark: FakeLarkClient, config: Config) -> Any:
    """Provider that hands the router a real TurnController per binding —
    exercises the rotation-aware clear-waiting / mode-update paths.

    Caches by chat_id (like bootstrap) so generation state (begin_turn /
    is_current_gen) is stable across calls within a test."""
    cache: dict[str, TurnController] = {}

    def provider(binding: ChatBinding) -> TurnController:
        controller = cache.get(binding.chat_id)
        if controller is None or controller.binding is not binding:
            controller = TurnController(binding=binding, lark=cast("Any", lark), config=config)
            cache[binding.chat_id] = controller
        return controller

    return provider


class _RecordingController:
    """Spy that wraps a real TurnController, recording the lifecycle methods
    the router calls while delegating everything to the real one (so turn
    creation / generation / sealing actually happen)."""

    def __init__(self, real: TurnController, calls: list[tuple[str, str]]) -> None:
        self._real = real
        self._calls = calls

    def __getattr__(self, name: str) -> Any:  # delegate anything not overridden
        return getattr(self._real, name)

    def open_turn(self, user_text: str) -> int | None:
        self._calls.append((self._real.binding.chat_id, "open_turn"))
        return self._real.open_turn(user_text)

    def clear_waiting_and_rerender(self) -> None:
        self._calls.append((self._real.binding.chat_id, "clear_waiting"))
        self._real.clear_waiting_and_rerender()

    def clear_waiting_and_build_card(self) -> dict[str, Any] | None:
        # WS-return variant: distinct method but the spy uses the same tag so
        # existing call-order assertions keep working regardless of which path
        # the router takes (text continuation → patch; button → WS return).
        self._calls.append((self._real.binding.chat_id, "clear_waiting"))
        return self._real.clear_waiting_and_build_card()

    def update_mode(self, mode: str) -> None:
        self._calls.append((self._real.binding.chat_id, f"mode:{mode}"))
        self._real.update_mode(mode)

    def seal(self, state: str = "done", error: str = "") -> None:
        self._calls.append((self._real.binding.chat_id, f"seal:{state}"))
        self._real.seal(state=cast("Any", state), error=error)


def _recording_controller_for(
    calls: list[tuple[str, str]], lark: FakeLarkClient, config: Config
) -> Any:
    """Caching provider of recording spies wrapping real controllers."""
    cache: dict[str, _RecordingController] = {}

    def provider(binding: ChatBinding) -> _RecordingController:
        spy = cache.get(binding.chat_id)
        if spy is None or spy.binding is not binding:
            real = TurnController(binding=binding, lark=cast("Any", lark), config=config)
            spy = _RecordingController(real, calls)
            cache[binding.chat_id] = spy
        return spy

    return provider


def _message(
    *,
    chat_id: str = "oc_chat1",
    text: str = "hello",
    sender_open_id: str = "ou_user1",
    message_type: str = "text",
    message_id: str | None = None,
    create_time_ms: int | None = None,
) -> IncomingMessage:
    # Default to a text-derived id so distinct messages in a single test
    # don't collide with the dedupe cache.
    if message_id is None:
        message_id = f"om_msg_{abs(hash(text)) % 10_000_000:07d}"
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_type="p2p",
        sender_open_id=sender_open_id,
        message_type=message_type,
        text=text,
        raw_content=f'{{"text":"{text}"}}',
        create_time_ms=create_time_ms,
    )


# ----------------------------------------------------------------- whitelist


def test_whitelisted_user_creates_binding(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path, open_id="ou_user1")
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(sender_open_id="ou_user1"))

    assert registry.get("oc_chat1") is not None
    assert any(c.method == "send_text" for c in tmux.calls)


def test_non_whitelisted_user_denied_includes_open_id(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path, open_id="ou_allowed")
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(sender_open_id="ou_evil"))

    # No tmux side effects
    assert all(c.method != "new_window" for c in tmux.calls)
    # User got a denial text with their open_id embedded, so they can hand it
    # straight to the admin.
    assert len(lark.sent) == 1
    last = lark.last_sent()
    assert last.kind == "text"
    assert "白名单" in last.text
    assert "ou_evil" in last.text


def test_unauth_reply_throttled_within_window(tmp_path: Path) -> None:
    """A user keeps hammering the bot — only one denial text is sent until the
    throttle window elapses."""
    cfg = _make_config(workspace=tmp_path, open_id="ou_allowed")
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    for i in range(4):
        router.handle_message(_message(sender_open_id="ou_evil", text=f"hi-{i}"))

    assert sum(1 for s in lark.sent if s.kind == "text") == 1


def test_unauth_reply_resumes_after_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the throttle window elapses, another denial *is* sent — the user
    might have forgotten the first reply or be on a new device."""
    import pocket_cc.relay.input as input_mod

    class _Clock:
        def __init__(self) -> None:
            self.t = 1000.0

        def __call__(self) -> float:
            return self.t

    clock = _Clock()
    # String-target form: ``input_mod.time`` is a submodule reference that
    # mypy --strict refuses to read off `input_mod` (time isn't re-exported
    # from there). The string form patches the same attribute by path.
    monkeypatch.setattr("pocket_cc.relay.input.time.monotonic", clock)

    cfg = _make_config(workspace=tmp_path, open_id="ou_allowed")
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(sender_open_id="ou_evil", text="hi-1"))
    clock.t += input_mod._UNAUTH_NOTICE_THROTTLE_S + 1.0
    router.handle_message(_message(sender_open_id="ou_evil", text="hi-2"))

    assert sum(1 for s in lark.sent if s.kind == "text") == 2


# --------------------------------------------------------------- chat scope


def test_group_message_silently_ignored(tmp_path: Path) -> None:
    """Group chats are out of scope for the DM-only Phase 2 design — we drop
    them silently rather than replying (which would spam every group member)."""
    cfg = _make_config(workspace=tmp_path, open_id="ou_user1")
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    msg = _message(sender_open_id="ou_user1")
    msg = IncomingMessage(
        message_id=msg.message_id,
        chat_id="oc_group1",
        chat_type="group",
        sender_open_id=msg.sender_open_id,
        message_type=msg.message_type,
        text=msg.text,
        raw_content=msg.raw_content,
    )

    router.handle_message(msg)

    # No tmux side effects, no Lark reply at all.
    assert all(c.method != "new_window" for c in tmux.calls)
    assert lark.sent == []
    assert registry.get("oc_group1") is None


# ----------------------------------------------------------- binding creation


def test_first_message_creates_binding_and_sends_initial_card(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(text="实现一个 hello world"))

    # tmux: new_window called with the configured claude command + workspace cwd
    # + window named after the user's display_name (chat-{display_name}).
    new_window_calls = [c for c in tmux.calls if c.method == "new_window"]
    assert len(new_window_calls) == 1
    assert new_window_calls[0].kwargs["command"] == cfg.claude_command
    assert new_window_calls[0].kwargs["cwd"] == str(tmp_path)
    assert new_window_calls[0].kwargs["name"] == "chat-test"

    # tmux: send_text called with user's text and the right window
    send_text_calls = [c for c in tmux.calls if c.method == "send_text"]
    assert len(send_text_calls) == 1
    assert send_text_calls[0].kwargs["text"] == "实现一个 hello world"
    assert send_text_calls[0].kwargs["window_id"] == "@1"

    # Lark: one initial card sent
    assert len(lark.sent) == 1
    assert lark.last_sent().kind == "card_id"

    # Registry: binding stored, with current_turn
    binding = registry.get("oc_chat1")
    assert binding is not None
    assert binding.current_turn is not None
    assert binding.current_turn.card_message_id == lark.last_sent().message_id


def test_tmux_new_window_failure_surfaces_to_user(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager(new_window_raises=TmuxError("no tmux"))
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message())

    assert registry.get("oc_chat1") is None
    # User got a text error reply
    assert any(s.kind == "text" and "失败" in s.text for s in lark.sent)


# ------------------------------------------------------------ turn transitions


def test_second_message_while_busy_is_rejected(tmp_path: Path) -> None:
    """A new message arriving while the prior turn is still in flight
    (OPENED/RUNNING) must NOT open a new turn or seal the old one — it would
    mis-ingest the running task's output and lie about completion. Instead the
    router rejects with a busy notice and leaves the current turn intact."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(text="first"))
    first_turn_id = registry.get("oc_chat1").current_turn.card_message_id  # type: ignore[union-attr]

    router.handle_message(_message(text="second"))

    binding = registry.get("oc_chat1")
    assert binding is not None
    # Same turn still current — not superseded, not sealed.
    assert binding.current_turn is not None
    assert binding.current_turn.card_message_id == first_turn_id
    # No second card was opened; exactly one busy notice (text) was sent.
    assert sum(1 for s in lark.sent if s.kind == "card_id") == 1
    assert sum(1 for s in lark.sent if s.kind == "text") == 1
    # The second prompt was never injected into tmux.
    assert [c.kwargs["text"] for c in tmux.calls if c.method == "send_text"] == ["first"]
    assert sum(1 for c in tmux.calls if c.method == "new_window") == 1


def test_busy_notices_are_throttled(tmp_path: Path) -> None:
    """A burst of messages while Claude is busy yields at most one notice per
    throttle window, so the user isn't spammed."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(text="first"))  # opens the turn (busy)
    router.handle_message(_message(text="busy-1"))
    router.handle_message(_message(text="busy-2"))
    router.handle_message(_message(text="busy-3"))

    # Three rejected messages within the window → exactly one busy notice.
    assert sum(1 for s in lark.sent if s.kind == "text") == 1


def test_non_text_message_is_not_opened_as_turn(tmp_path: Path) -> None:
    """Non-text messages must not create a binding or spawn a window —
    that contract is unchanged. What's new is they no longer silently
    drop (see the notice tests below)."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(message_type="image", text=""))

    assert registry.get("oc_chat1") is None
    assert all(c.method != "new_window" for c in tmux.calls)


def test_post_message_replies_with_formatting_hint(tmp_path: Path) -> None:
    """The most common trigger: mobile composer upgraded formatted text
    to ``msg_type=post``. The notice must tell the user *what to do*
    (cancel formatting), not just "unsupported"."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(message_type="post", text=""))

    assert len(lark.sent) == 1
    body = lark.last_sent().text
    assert "post" in body
    assert "格式" in body  # actionable hint for the user


def test_image_message_replies_with_media_hint(tmp_path: Path) -> None:
    """Media types get a distinct hint — "not yet supported" rather
    than "cancel formatting", which would be misleading."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(message_type="image", text=""))

    assert len(lark.sent) == 1
    body = lark.last_sent().text
    assert "image" in body
    assert "格式" not in body  # image-vs-post hints must not collide


def test_unsupported_notice_throttled_within_window(tmp_path: Path) -> None:
    """User forwards 4 images in a row — only one reply, not four."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    for i in range(4):
        router.handle_message(
            _message(message_type="image", text="", message_id=f"om_img_{i}")
        )

    assert sum(1 for s in lark.sent if s.kind == "text") == 1


def test_unsupported_notice_resumes_after_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Throttle is per-chat and time-based — after the quiet window a
    fresh non-text message gets a fresh notice."""
    import pocket_cc.relay.input as input_mod

    class _Clock:
        def __init__(self) -> None:
            self.t = 1000.0

        def __call__(self) -> float:
            return self.t

    clock = _Clock()
    # String-target form: ``input_mod.time`` is a submodule reference that
    # mypy --strict refuses to read off `input_mod` (time isn't re-exported
    # from there). The string form patches the same attribute by path.
    monkeypatch.setattr("pocket_cc.relay.input.time.monotonic", clock)

    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(message_type="post", text="", message_id="om1"))
    clock.t += input_mod._UNSUPPORTED_NOTICE_THROTTLE_S + 1.0
    router.handle_message(_message(message_type="post", text="", message_id="om2"))

    assert sum(1 for s in lark.sent if s.kind == "text") == 2


def test_stale_message_is_dropped_silently(tmp_path: Path) -> None:
    """A WS-replayed event older than the stale window must not open a
    turn or notify — the user has moved on and a reply now would lie
    about being live."""
    import pocket_cc.relay.input as input_mod

    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    import time as _time

    now_ms = int(_time.time() * 1000)
    stale = now_ms - input_mod._STALE_MESSAGE_WINDOW_MS - 5_000  # 5s past window
    router.handle_message(_message(text="old prompt", create_time_ms=stale))

    assert registry.get("oc_chat1") is None
    assert all(c.method != "new_window" for c in tmux.calls)
    assert lark.sent == []  # no notice, silent drop


def test_fresh_message_at_window_edge_is_processed(tmp_path: Path) -> None:
    """Just-inside-the-window messages still go through — the threshold
    is "older than", not "as old as"."""
    import pocket_cc.relay.input as input_mod

    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    import time as _time

    now_ms = int(_time.time() * 1000)
    just_fresh = now_ms - input_mod._STALE_MESSAGE_WINDOW_MS + 1_000  # 1s inside
    router.handle_message(_message(text="recent prompt", create_time_ms=just_fresh))

    assert registry.get("oc_chat1") is not None  # binding created → turn opened


def test_message_without_create_time_is_processed(tmp_path: Path) -> None:
    """Back-compat: events that don't carry ``create_time_ms`` (older
    fakes, malformed payloads) must not be accidentally dropped — we'd
    rather process a message we can't time-check than silently lose it."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(text="hi", create_time_ms=None))

    assert registry.get("oc_chat1") is not None


def test_empty_text_message_stays_silent(tmp_path: Path) -> None:
    """A text-typed message with empty body (pure @mention etc.) is
    not actionable and has no useful advice — must not trigger the
    unsupported-type notice."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(message_type="text", text=""))

    assert lark.sent == []
    assert registry.get("oc_chat1") is None


def test_slash_commands_are_passed_through_verbatim(tmp_path: Path) -> None:
    """The 'zero command' contract — `/clear` goes to Claude, not to us."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(text="/clear"))

    send_text = next(c for c in tmux.calls if c.method == "send_text")
    assert send_text.kwargs["text"] == "/clear"


# ----------------------------------------------------------- card actions


def test_card_action_cancel_sends_ctrl_c_then_double_escape(tmp_path: Path) -> None:
    """⏹ 中断 fires C-c then *two* Escapes to cover all Claude TUI states:
    running (C-c → redirect, Esc → exits redirect / clears input), or idle
    with leftover input (double-Esc clears the input box). Sending just
    C-c + one Esc misses the leftover-input case, where the next user
    message would silently concatenate to the unsent prompt."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    card_id = lark.last_sent().message_id
    pre_calls = len(tmux.calls)

    router.handle_card_action(
        CardAction(
            message_id=card_id,
            chat_id="oc_chat1",
            sender_open_id="ou_user1",
            token="tok",
            tag="button",
            value={"action": "cancel"},
        )
    )

    cancel_keys = [c for c in tmux.calls[pre_calls:] if c.method == "send_key"]
    # C-c → interrupt running task (or clear pending text); Escape ×2 →
    # exit "Interrupted · redirect" mode AND clear any leftover input.
    assert [k.kwargs["key"] for k in cancel_keys] == ["C-c", "Escape", "Escape"]
    assert all(k.kwargs["window_id"] == "@1" for k in cancel_keys)


def test_cancel_marks_submission_cancelled_so_deferred_enter_is_dropped(
    tmp_path: Path,
) -> None:
    """Early ⏹ 中断 must abort the deferred Enter before it submits the prompt.

    The cancel handler tells the controller to cancel the active turn's
    submission; the deferred-Enter worker checks
    ``should_submit_deferred_enter`` and drops the Enter. Asserted
    deterministically via the controller (no thread-timing race).
    """
    import pocket_cc.relay.input as input_module

    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    provider = _real_controller_for(lark, cfg)
    router = _make_router(
        tmux=tmux, lark=lark, registry=registry, config=cfg, controller_for=provider
    )

    # Stretch the grace window so the auto-spawned worker can't fire during
    # the test (we assert the cancel fence directly, not via the worker).
    orig_delay = input_module._DEFERRED_ENTER_DELAY_S
    input_module._DEFERRED_ENTER_DELAY_S = 60.0
    try:
        router.handle_message(_message())
        card_id = lark.last_sent().message_id
        binding = registry.get("oc_chat1")
        assert binding is not None
        controller = provider(binding)
        assert controller.should_submit_deferred_enter(1) is True  # gen 1, not yet cancelled

        router.handle_card_action(
            CardAction(
                message_id=card_id,
                chat_id="oc_chat1",
                sender_open_id="ou_user1",
                token="tok",
                tag="button",
                value={"action": "cancel"},
            )
        )

        # Cancel marked the active generation → the worker must drop the Enter.
        assert controller.should_submit_deferred_enter(1) is False
    finally:
        input_module._DEFERRED_ENTER_DELAY_S = orig_delay


def test_card_action_key_sends_named_key(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    card_id = lark.last_sent().message_id

    router.handle_card_action(
        CardAction(
            message_id=card_id,
            chat_id="oc_chat1",
            sender_open_id="ou_user1",
            token="tok",
            tag="button",
            value={"action": "key", "key": "Escape"},
        )
    )

    key_calls = [c for c in tmux.calls if c.method == "send_key"]
    assert key_calls and key_calls[-1].kwargs == {"window_id": "@1", "key": "Escape"}


def test_card_action_mode_btab_reads_mode_back_and_rerenders(tmp_path: Path) -> None:
    """⇧⭾ Mode sends BTab, then scrapes the pane mode-line and refreshes the
    card. The transcript doesn't carry mid-turn mode changes, so this pane
    readback is what makes the new mode echo in Lark."""
    import pocket_cc.relay.input as input_module

    cfg = _make_config(workspace=tmp_path)
    # Pane shows the acceptEdits banner after the toggle.
    tmux = FakeTmuxManager(capture_pane_text="working...\n⏵⏵ accept edits on (shift+tab to cycle)")
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(
        tmux=tmux,
        lark=lark,
        registry=registry,
        config=cfg,
        controller_for=_real_controller_for(lark, cfg),
    )
    router.handle_message(_message())
    card_id = lark.last_sent().message_id

    orig_delay = input_module._MODE_READBACK_DELAY_S
    input_module._MODE_READBACK_DELAY_S = 0.0
    try:
        router.handle_card_action(
            CardAction(
                message_id=card_id,
                chat_id="oc_chat1",
                sender_open_id="ou_user1",
                token="tok",
                tag="button",
                value={"action": "key", "key": "BTab"},
            )
        )
    finally:
        input_module._MODE_READBACK_DELAY_S = orig_delay

    # BTab was sent to tmux
    assert any(c.method == "send_key" and c.kwargs["key"] == "BTab" for c in tmux.calls)
    # The pane was captured back, mode parsed, binding updated, card re-rendered.
    binding = registry.get("oc_chat1")
    assert binding is not None
    assert binding.current_mode == "acceptEdits"
    assert binding.current_turn is not None
    assert binding.current_turn.accumulator.current_mode == "acceptEdits"


def test_card_action_show_pane_dumps_capture_into_new_card(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager(capture_pane_text="line1\nline2\nline3")
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    initial_card_count = len(lark.sent)

    router.handle_card_action(
        CardAction(
            message_id=lark.last_sent().message_id,
            chat_id="oc_chat1",
            sender_open_id="ou_user1",
            token="tok",
            tag="button",
            value={"action": "show_pane"},
        )
    )

    assert len(lark.sent) == initial_card_count + 1
    # Pane-dump is now a cardkit one-shot: create_card_entity + send_card_id.
    # The body lives on the most recent card entity, not inlined on the IM
    # message record (which only references the card_id).
    body = lark.card_entities[-1].card["body"]["elements"][0]["content"]
    assert "line3" in body  # tail wins


# ------------------------------------------------------------- dedupe (D-13)


def test_duplicate_message_id_is_dropped(tmp_path: Path) -> None:
    """Lark WS re-delivers the same event with the same message_id."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    msg = _message(text="just once", message_id="om_fixed_id")
    router.handle_message(msg)
    router.handle_message(msg)  # exact same message_id → should be a no-op

    new_windows = [c for c in tmux.calls if c.method == "new_window"]
    send_texts = [c for c in tmux.calls if c.method == "send_text"]
    assert len(new_windows) == 1
    assert len(send_texts) == 1
    # Only one initial card sent — not two
    cards_sent = [s for s in lark.sent if s.kind == "card_id"]
    assert len(cards_sent) == 1


def test_duplicate_card_action_token_is_dropped(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    card_id = lark.last_sent().message_id

    action = CardAction(
        message_id=card_id,
        chat_id="oc_chat1",
        sender_open_id="ou_user1",
        token="tok_same",
        tag="button",
        value={"action": "cancel"},
    )
    router.handle_card_action(action)
    router.handle_card_action(action)  # re-delivered → no second Ctrl-C

    ctrl_c_calls = [c for c in tmux.calls if c.method == "send_key" and c.kwargs["key"] == "C-c"]
    assert len(ctrl_c_calls) == 1


def test_busy_message_does_not_seal_prior_turn(tmp_path: Path) -> None:
    """A new message while a turn is in flight must NOT seal it (the old
    premature-"done" behavior). An interrupt fires no Stop and completion does,
    so only the real signal — or an explicit ⏹ — may seal the turn."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    calls: list[tuple[str, str]] = []
    router = _make_router(
        tmux=tmux,
        lark=lark,
        registry=registry,
        config=cfg,
        controller_for=_recording_controller_for(calls, lark, cfg),
    )

    router.handle_message(_message(text="first"))
    # Second message arrives while the first turn is busy → rejected, not sealed.
    router.handle_message(_message(text="second", message_id="om_msg2"))

    assert not any(c[1].startswith("seal:") for c in calls)


def test_distinct_message_ids_both_processed_not_deduped(tmp_path: Path) -> None:
    """Two distinct message_ids must both pass the dedupe filter (only repeated
    *ids* are dropped). The first opens a turn; the second, arriving while busy,
    is processed into a busy notice — proving it wasn't silently deduped."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    m1 = _message(text="first")
    m2 = IncomingMessage(
        message_id="om_msg2",  # different id
        chat_id=m1.chat_id,
        chat_type="p2p",
        sender_open_id=m1.sender_open_id,
        message_type="text",
        text="second",
        raw_content='{"text":"second"}',
    )
    router.handle_message(m1)
    router.handle_message(m2)

    # First prompt injected; second rejected with a busy notice (not deduped).
    assert [c.kwargs["text"] for c in tmux.calls if c.method == "send_text"] == ["first"]
    assert sum(1 for s in lark.sent if s.kind == "text") == 1


def test_card_action_unknown_card_id_is_no_op(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_card_action(
        CardAction(
            message_id="om_unknown",
            chat_id="oc_chat1",
            sender_open_id="ou_user1",
            token="tok",
            tag="button",
            value={"action": "cancel"},
        )
    )

    assert tmux.calls == []


# ============================================================== waiting (M2-0)


def _set_waiting_on_active_turn(
    registry: Registry, chat_id: str, options: tuple[WaitingOption, ...] = ()
) -> WaitingFor:
    """Pin a WaitingFor onto the active turn. Used by waiting-state tests."""
    binding = registry.get(chat_id)
    assert binding is not None
    assert binding.current_turn is not None
    if not options:
        options = (
            WaitingOption(label="Yes", response=TextResponse(text="1")),
            WaitingOption(label="No", response=TextResponse(text="2")),
        )
    waiting = WaitingFor(
        source="permission",
        question="Do you want to proceed?",
        options=options,
    )
    binding.current_turn.waiting_for = waiting
    return waiting


def test_message_during_waiting_does_not_open_new_card(tmp_path: Path) -> None:
    """Continuation path: a Lark text reply while waiting must reuse the card."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    # First message: opens a turn and posts a card.
    router.handle_message(_message(text="please run X"))
    cards_after_open = sum(1 for s in lark.sent if s.kind == "card_id")
    assert cards_after_open == 1

    # Pin a waiting state on the active turn (simulating M2-A/C detection).
    _set_waiting_on_active_turn(registry, "oc_chat1")

    # User replies in Lark — should be treated as continuation, no new card.
    router.handle_message(_message(text="1", message_id="om_reply"))

    cards_after_reply = sum(1 for s in lark.sent if s.kind == "card_id")
    assert cards_after_reply == 1, "no new card should be sent during waiting continuation"


def test_message_during_waiting_clears_waiting_for_and_sends_text(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message(text="please run X"))
    _set_waiting_on_active_turn(registry, "oc_chat1")

    router.handle_message(_message(text="1", message_id="om_reply"))

    binding = registry.get("oc_chat1")
    assert binding is not None
    assert binding.current_turn is not None
    assert binding.current_turn.waiting_for is None

    # The last send_text should be the reply text, going to the bound window.
    send_texts = [c for c in tmux.calls if c.method == "send_text"]
    assert send_texts[-1].kwargs == {"window_id": "@1", "text": "1"}


def test_waiting_reply_text_uses_rotation_aware_rerender(tmp_path: Path) -> None:
    """Replying to a waiting prompt must flip the card back to running via the
    injected rotation-aware callback — not a direct full-history render that
    would re-dump (and tail-truncate) an already-rotated turn."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    calls: list[tuple[str, str]] = []
    router = _make_router(
        tmux=tmux,
        lark=lark,
        registry=registry,
        config=cfg,
        controller_for=_recording_controller_for(calls, lark, cfg),
    )
    router.handle_message(_message(text="please run X"))
    _set_waiting_on_active_turn(registry, "oc_chat1")

    router.handle_message(_message(text="1", message_id="om_reply"))

    assert calls == [("oc_chat1", "open_turn"), ("oc_chat1", "clear_waiting")]


def test_waiting_response_button_uses_rotation_aware_rerender(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    calls: list[tuple[str, str]] = []
    router = _make_router(
        tmux=tmux,
        lark=lark,
        registry=registry,
        config=cfg,
        controller_for=_recording_controller_for(calls, lark, cfg),
    )
    router.handle_message(_message(text="please run X"))
    card_id = lark.last_sent().message_id
    _set_waiting_on_active_turn(registry, "oc_chat1")

    router.handle_card_action(
        CardAction(
            message_id=card_id,
            chat_id="oc_chat1",
            sender_open_id="ou_user1",
            token="tok",
            tag="button",
            value={"action": "waiting_response", "index": 0},
        )
    )

    assert calls == [("oc_chat1", "open_turn"), ("oc_chat1", "clear_waiting")]


def test_message_after_continuation_is_rejected_until_turn_completes(tmp_path: Path) -> None:
    """Answering a waiting prompt (continuation) puts Claude back to work — it
    does NOT make the turn idle. A new request while it's running is rejected;
    only once the turn actually completes (seal → IDLE) does the next message
    open a fresh turn."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    provider = _real_controller_for(lark, cfg)
    router = _make_router(
        tmux=tmux, lark=lark, registry=registry, config=cfg, controller_for=provider
    )
    router.handle_message(_message(text="please run X"))
    _set_waiting_on_active_turn(registry, "oc_chat1")
    router.handle_message(_message(text="1", message_id="om_reply"))  # continuation

    # Turn is running again → a new request is rejected (no new card).
    router.handle_message(_message(text="next request", message_id="om_next"))
    assert sum(1 for s in lark.sent if s.kind == "card_id") == 1

    # Now the turn completes → IDLE. The next message opens a new turn.
    binding = registry.get("oc_chat1")
    assert binding is not None
    provider(binding).seal(state="done")
    router.handle_message(_message(text="after done", message_id="om_after"))
    assert sum(1 for s in lark.sent if s.kind == "card_id") == 2


def test_card_action_waiting_response_text_dispatches_send_text(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    card_id = lark.last_sent().message_id
    _set_waiting_on_active_turn(registry, "oc_chat1")
    patches_before = len(lark.patches)

    result = router.handle_card_action(
        CardAction(
            message_id=card_id,
            chat_id="oc_chat1",
            sender_open_id="ou_user1",
            token="tok_w1",
            tag="button",
            value={"action": "waiting_response", "index": 1},
        )
    )

    send_texts = [c for c in tmux.calls if c.method == "send_text"]
    # Option index 1 → response TextResponse(text="2")
    assert send_texts[-1].kwargs["text"] == "2"
    binding = registry.get("oc_chat1")
    assert binding is not None
    assert binding.current_turn is not None
    assert binding.current_turn.waiting_for is None
    # WS round-trip optimization: the handler returns the cleared-waiting
    # card so the dispatcher can ship it back in the same WS frame instead
    # of issuing a follow-up PATCH. Lark renders the running-state card
    # instantly when the user taps the option button.
    assert isinstance(result, dict)
    assert result["header"]["template"] == "blue"  # running state
    # And we did NOT push a separate PATCH for that same rerender — the WS
    # response carries it. (Future transcript ticks still PATCH normally.)
    assert len(lark.patches) == patches_before


def test_card_action_waiting_response_keys_dispatches_send_key_sequence(
    tmp_path: Path,
) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    card_id = lark.last_sent().message_id
    options = (WaitingOption(label="Top", response=KeysResponse(keys=("Up", "Up", "Enter"))),)
    _set_waiting_on_active_turn(registry, "oc_chat1", options=options)

    router.handle_card_action(
        CardAction(
            message_id=card_id,
            chat_id="oc_chat1",
            sender_open_id="ou_user1",
            token="tok_w2",
            tag="button",
            value={"action": "waiting_response", "index": 0},
        )
    )

    keys = [c.kwargs["key"] for c in tmux.calls if c.method == "send_key"]
    # The 3 keys we asked for, in order. send_text was not used.
    assert keys[-3:] == ["Up", "Up", "Enter"]
    assert all(c.method != "send_text" or c.kwargs["text"] != "Top" for c in tmux.calls)


def test_card_action_waiting_response_index_out_of_range_is_noop(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    card_id = lark.last_sent().message_id
    _set_waiting_on_active_turn(registry, "oc_chat1")
    tmux_calls_before = len(tmux.calls)

    router.handle_card_action(
        CardAction(
            message_id=card_id,
            chat_id="oc_chat1",
            sender_open_id="ou_user1",
            token="tok_oor",
            tag="button",
            value={"action": "waiting_response", "index": 99},
        )
    )

    # No new tmux activity (no send_text, no send_key)
    assert len(tmux.calls) == tmux_calls_before
    # Waiting state preserved (we didn't clear it)
    binding = registry.get("oc_chat1")
    assert binding is not None
    assert binding.current_turn is not None
    assert binding.current_turn.waiting_for is not None


def test_card_action_non_waiting_paths_return_none(tmp_path: Path) -> None:
    """Only the waiting-response button benefits from the WS-return path —
    every other action (cancel / key / key_sequence / show_pane / unknown)
    returns None so the dispatcher uses the SDK's default no-card-update
    response. Asserting this keeps the WS frame size small for the common
    case of buttons that only side-effect tmux."""
    import pocket_cc.relay.input as input_module

    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    card_id = lark.last_sent().message_id

    def _act(value: dict[str, Any], token: str) -> dict[str, Any] | None:
        return router.handle_card_action(
            CardAction(
                message_id=card_id,
                chat_id="oc_chat1",
                sender_open_id="ou_user1",
                token=token,
                tag="button",
                value=value,
            )
        )

    # Stretch the mode readback delay to 0 so the BTab-triggered pane scrape
    # doesn't sleep the whole 0.35s during this test.
    orig_mode_delay = input_module._MODE_READBACK_DELAY_S
    input_module._MODE_READBACK_DELAY_S = 0.0
    try:
        assert _act({"action": "key", "key": "Escape"}, "t1") is None
        assert _act({"action": "key", "key": "BTab"}, "t2") is None
        assert (
            _act({"action": "key_sequence", "keys": ["Up", "Enter"], "delay_ms": 0}, "t3") is None
        )
        assert _act({"action": "show_pane"}, "t4") is None
        assert _act({"action": "whatever_unknown"}, "t5") is None
    finally:
        input_module._MODE_READBACK_DELAY_S = orig_mode_delay


def test_card_action_waiting_response_no_active_waiting_is_noop(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    card_id = lark.last_sent().message_id
    tmux_calls_before = len(tmux.calls)

    # No waiting_for set — clicking the waiting button is a no-op
    result = router.handle_card_action(
        CardAction(
            message_id=card_id,
            chat_id="oc_chat1",
            sender_open_id="ou_user1",
            token="tok_no_w",
            tag="button",
            value={"action": "waiting_response", "index": 0},
        )
    )

    assert len(tmux.calls) == tmux_calls_before
    # No-op paths return None so the WS dispatcher doesn't ship a stale card.
    assert result is None


def test_card_action_waiting_response_bad_index_type_is_noop(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    card_id = lark.last_sent().message_id
    _set_waiting_on_active_turn(registry, "oc_chat1")
    tmux_calls_before = len(tmux.calls)

    router.handle_card_action(
        CardAction(
            message_id=card_id,
            chat_id="oc_chat1",
            sender_open_id="ou_user1",
            token="tok_bad",
            tag="button",
            value={"action": "waiting_response", "index": "not-an-int"},
        )
    )

    assert len(tmux.calls) == tmux_calls_before


# ============================================================ key_sequence


def test_card_action_key_sequence_sends_each_key_in_order(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    card_id = lark.last_sent().message_id
    pre = len(tmux.calls)

    router.handle_card_action(
        CardAction(
            message_id=card_id,
            chat_id="oc_chat1",
            sender_open_id="ou_user1",
            token="tok_seq",
            tag="button",
            value={
                "action": "key_sequence",
                "keys": ["Escape", "Escape"],
                "delay_ms": 1,  # tiny so tests stay fast
            },
        )
    )

    keys = [c.kwargs["key"] for c in tmux.calls[pre:] if c.method == "send_key"]
    assert keys == ["Escape", "Escape"]


def test_card_action_key_sequence_skips_non_string_keys(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    card_id = lark.last_sent().message_id
    pre = len(tmux.calls)

    router.handle_card_action(
        CardAction(
            message_id=card_id,
            chat_id="oc_chat1",
            sender_open_id="ou_user1",
            token="tok_skip",
            tag="button",
            value={
                "action": "key_sequence",
                "keys": ["Up", None, "", 42, "Enter"],  # mixed garbage
                "delay_ms": 0,
            },
        )
    )

    keys = [c.kwargs["key"] for c in tmux.calls[pre:] if c.method == "send_key"]
    assert keys == ["Up", "Enter"]


def test_card_action_key_sequence_with_non_list_keys_is_noop(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    card_id = lark.last_sent().message_id
    pre = len(tmux.calls)

    router.handle_card_action(
        CardAction(
            message_id=card_id,
            chat_id="oc_chat1",
            sender_open_id="ou_user1",
            token="tok_noseq",
            tag="button",
            value={"action": "key_sequence", "keys": "not-a-list"},
        )
    )

    new_calls = tmux.calls[pre:]
    assert all(c.method != "send_key" for c in new_calls)
