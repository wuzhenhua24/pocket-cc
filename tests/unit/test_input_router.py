"""Unit tests for relay/input.py — message + card-action routing.

Uses FakeLarkClient for Lark and a hand-rolled FakeTmuxManager that records
calls and lets tests pre-seed `new_window` return values + force failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — used in test signatures
from typing import Any, cast

from pocket_cc.app.config import Config
from pocket_cc.app.persistence import Registry
from pocket_cc.lark.client import FakeLarkClient
from pocket_cc.lark.event_loop import CardAction, IncomingMessage
from pocket_cc.relay.input import InputRouter
from pocket_cc.relay.waiting import (
    KeysResponse,
    TextResponse,
    WaitingFor,
    WaitingOption,
)
from pocket_cc.tmux import TmuxError, WindowInfo

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


def _make_config(*, workspace: Path, whitelist: frozenset[str] = frozenset()) -> Config:
    return Config(
        app_id="cli_x",
        app_secret="secret",
        lark_domain="https://open.feishu.cn",
        workspace_root=workspace,
        claude_command="claude --permission-mode bypassPermissions",
        tmux_session="pocket-cc-test",
        user_whitelist=whitelist,
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
    rerender_active: Any = None,
) -> InputRouter:
    return InputRouter(
        config=config,
        tmux=cast("Any", tmux),
        lark=lark,
        registry=registry,
        rerender_active=rerender_active,
    )


def _message(
    *,
    chat_id: str = "oc_chat1",
    text: str = "hello",
    sender_open_id: str = "ou_user1",
    message_type: str = "text",
    message_id: str | None = None,
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
    )


# ----------------------------------------------------------------- whitelist


def test_open_whitelist_lets_anyone_in(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(sender_open_id="ou_anyone"))

    assert registry.get("oc_chat1") is not None
    assert any(c.method == "send_text" for c in tmux.calls)


def test_closed_whitelist_denies_outsider(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path, whitelist=frozenset({"ou_allowed"}))
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(sender_open_id="ou_evil"))

    # No tmux side effects
    assert all(c.method != "new_window" for c in tmux.calls)
    # User got a denial text
    assert len(lark.sent) == 1
    last = lark.last_sent()
    assert last.kind == "text"
    assert "白名单" in last.text


# ----------------------------------------------------------- binding creation


def test_first_message_creates_binding_and_sends_initial_card(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(text="实现一个 hello world"))

    # tmux: new_window called with the configured claude command + workspace cwd
    new_window_calls = [c for c in tmux.calls if c.method == "new_window"]
    assert len(new_window_calls) == 1
    assert new_window_calls[0].kwargs["command"] == cfg.claude_command
    assert new_window_calls[0].kwargs["cwd"] == str(tmp_path)

    # tmux: send_text called with user's text and the right window
    send_text_calls = [c for c in tmux.calls if c.method == "send_text"]
    assert len(send_text_calls) == 1
    assert send_text_calls[0].kwargs["text"] == "实现一个 hello world"
    assert send_text_calls[0].kwargs["window_id"] == "@1"

    # Lark: one initial card sent
    assert len(lark.sent) == 1
    assert lark.last_sent().kind == "card"

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


def test_second_message_closes_prior_turn_and_opens_new_one(tmp_path: Path) -> None:
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
    assert binding.current_turn is not None
    second_turn_id = binding.current_turn.card_message_id
    assert second_turn_id != first_turn_id

    # Second call should NOT have created a second tmux window — same binding.
    assert sum(1 for c in tmux.calls if c.method == "new_window") == 1


def test_non_text_message_is_ignored(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    router.handle_message(_message(message_type="image", text=""))

    assert registry.get("oc_chat1") is None
    assert lark.sent == []


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


def test_cancel_skips_deferred_enter_so_prompt_is_not_submitted(
    tmp_path: Path,
) -> None:
    """Early ⏹ 中断 must abort the deferred Enter before it submits the prompt.

    Regression: send_text (text-only) injects the user's text into the
    pane, then a worker thread sleeps `_DEFERRED_ENTER_DELAY_S` and fires
    Enter. If cancel runs during that grace window, the cancel handler
    sets `turn.cancel_event` — the worker wakes early on the event and
    skips Enter, so Claude never receives the prompt. Without this, the
    Enter fires after C-c/Escape ran, leaving Claude actually running.
    """
    import pocket_cc.relay.input as input_module

    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)

    # Stretch the grace window so the test can race cancel against it
    # without flakiness; the production value (0.5s) is way too long for
    # a unit test, but we need it non-zero to exercise the wait path.
    orig_delay = input_module._DEFERRED_ENTER_DELAY_S
    input_module._DEFERRED_ENTER_DELAY_S = 2.0
    try:
        router.handle_message(_message())
        card_id = lark.last_sent().message_id

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
    finally:
        input_module._DEFERRED_ENTER_DELAY_S = orig_delay

    # Wait briefly for the deferred-Enter worker to observe cancel_event
    # and exit. Without sleeping, the assertion could race the worker
    # thread; the cancel_event.set() in _handle_cancel wakes it almost
    # immediately, so 100ms is plenty.
    import time

    time.sleep(0.1)

    enter_keys = [
        c for c in tmux.calls if c.method == "send_key" and c.kwargs["key"] == "Enter"
    ]
    assert enter_keys == [], (
        "deferred Enter must NOT fire after cancel — would re-submit the user's "
        "prompt to Claude after the cancel handler already cleared the pane"
    )


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
    rerendered: list[str] = []
    router = _make_router(
        tmux=tmux,
        lark=lark,
        registry=registry,
        config=cfg,
        rerender_active=lambda b: rerendered.append(b.chat_id),
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
    assert any(
        c.method == "send_key" and c.kwargs["key"] == "BTab" for c in tmux.calls
    )
    # The pane was captured back, mode parsed, binding updated, card re-rendered.
    binding = registry.get("oc_chat1")
    assert binding is not None
    assert binding.current_mode == "acceptEdits"
    assert binding.current_turn is not None
    assert binding.current_turn.accumulator.current_mode == "acceptEdits"
    assert rerendered == ["oc_chat1"]


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
    body = lark.last_sent().card["elements"][0]["content"]
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
    cards_sent = [s for s in lark.sent if s.kind == "card"]
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


def test_distinct_message_ids_both_processed(tmp_path: Path) -> None:
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

    send_texts = [c for c in tmux.calls if c.method == "send_text"]
    assert {c.kwargs["text"] for c in send_texts} == {"first", "second"}


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
    cards_after_open = sum(1 for s in lark.sent if s.kind == "card")
    assert cards_after_open == 1

    # Pin a waiting state on the active turn (simulating M2-A/C detection).
    _set_waiting_on_active_turn(registry, "oc_chat1")

    # User replies in Lark — should be treated as continuation, no new card.
    router.handle_message(_message(text="1", message_id="om_reply"))

    cards_after_reply = sum(1 for s in lark.sent if s.kind == "card")
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


def test_message_after_continuation_opens_new_turn_again(tmp_path: Path) -> None:
    """Once continuation clears waiting_for, the next reply opens a new turn."""
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message(text="please run X"))
    _set_waiting_on_active_turn(registry, "oc_chat1")
    router.handle_message(_message(text="1", message_id="om_reply"))
    # Continuation done → waiting cleared. A *new* message now should open
    # a new turn (= new card).
    router.handle_message(_message(text="next request", message_id="om_next"))
    cards = [s for s in lark.sent if s.kind == "card"]
    assert len(cards) == 2  # original + new turn


def test_card_action_waiting_response_text_dispatches_send_text(tmp_path: Path) -> None:
    cfg = _make_config(workspace=tmp_path)
    tmux = FakeTmuxManager()
    lark = FakeLarkClient()
    registry = Registry()
    router = _make_router(tmux=tmux, lark=lark, registry=registry, config=cfg)
    router.handle_message(_message())
    card_id = lark.last_sent().message_id
    _set_waiting_on_active_turn(registry, "oc_chat1")

    router.handle_card_action(
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
    router.handle_card_action(
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
