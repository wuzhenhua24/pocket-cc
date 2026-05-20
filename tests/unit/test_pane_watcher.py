"""Unit tests for relay/pane_watcher.py — tick → waiting_for transitions."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pocket_cc.app.persistence import ChatBinding, Registry, TurnState
from pocket_cc.relay.pane_watcher import PaneWatcher, _build_waiting_for, _compose_question
from pocket_cc.relay.waiting import TextResponse, WaitingFor, WaitingOption
from pocket_cc.tmux import TmuxError, WindowInfo

_PROMPT_YES_NO = textwrap.dedent(
    """\
    Bash command
      cmd-A

    Do you want to proceed?
    ❯ 1. Yes
      2. No

    Esc to cancel
    """
)

_PROMPT_DIFFERENT = textwrap.dedent(
    """\
    Bash command
      cmd-B

    Do you want to proceed?
    ❯ 1. Yes
      2. No

    Esc to cancel
    """
)

_NO_PROMPT = "$ all done\n$ "


# ----------------------------------------------------------------- fakes


@dataclass
class _FakeTmux:
    """Pluggable pane source — `pane_text` controls what inspect sees next tick."""

    pane_text: str = ""
    raise_on_capture: TmuxError | None = None
    calls: list[str] = field(default_factory=list)

    def capture_pane(
        self,
        window_id: str,
        *,
        lines: int | None = None,
        include_history: bool = False,
    ) -> str:
        self.calls.append(window_id)
        if self.raise_on_capture:
            raise self.raise_on_capture
        return self.pane_text


@dataclass
class _StubWindow:
    session: str = "stub"
    window_id: str = "@0"
    name: str = "stub"
    cwd: str = "/tmp"
    pane_id: str = "%0"


def _make_binding_with_turn(chat_id: str = "oc_x") -> ChatBinding:
    binding = ChatBinding(
        chat_id=chat_id,
        window=cast("WindowInfo", _StubWindow()),
        cwd=Path("/tmp"),
    )
    binding.current_turn = TurnState(
        card_message_id="om_x",
        card_stream=cast("Any", _FakeAny()),
        accumulator=cast("Any", _FakeAny()),
    )
    return binding


class _FakeAny:
    """Stand-in for typed dependencies the watcher doesn't actually touch."""

    def __getattr__(self, name: str) -> Any:
        return self


def _make_watcher(*, tmux: _FakeTmux, registry: Registry, on_change_log: list[str]) -> PaneWatcher:
    return PaneWatcher(
        registry=registry,
        tmux=cast("Any", tmux),
        on_change=lambda b: on_change_log.append(b.chat_id),
        interval_s=10.0,  # huge — we drive ticks manually
    )


# ===================================================== helpers (unit funcs)


def test_compose_question_with_context() -> None:
    from pocket_cc.claude.pane_inspector import ParsedOption, ParsedPrompt

    p = ParsedPrompt(
        kind="permission",
        question="Do you want to proceed?",
        context="Bash command\n  rm -rf /",
        options=(ParsedOption(number=1, label="Yes", selected=True),),
        fingerprint="fp",
    )
    assert _compose_question(p) == "Bash command\n  rm -rf /\n\nDo you want to proceed?"


def test_compose_question_without_context() -> None:
    from pocket_cc.claude.pane_inspector import ParsedPrompt

    p = ParsedPrompt(
        kind="permission",
        question="Do you want to proceed?",
        context="",
        options=(),
        fingerprint="fp",
    )
    assert _compose_question(p) == "Do you want to proceed?"


def test_build_waiting_for_maps_numbered_options_to_text_response() -> None:
    from pocket_cc.claude.pane_inspector import ParsedOption, ParsedPrompt

    p = ParsedPrompt(
        kind="permission",
        question="Do you want to proceed?",
        context="ctx",
        options=(
            ParsedOption(number=1, label="Yes", selected=True),
            ParsedOption(number=2, label="No", selected=False),
            ParsedOption(number=3, label="Always", selected=False),
        ),
        fingerprint="fp-xyz",
    )
    waiting = _build_waiting_for(p)
    assert isinstance(waiting, WaitingFor)
    assert waiting.source == "permission"
    assert waiting.fingerprint == "fp-xyz"
    assert "Do you want to proceed?" in waiting.question
    assert "ctx" in waiting.question
    assert len(waiting.options) == 3
    for i, opt in enumerate(waiting.options, start=1):
        assert isinstance(opt, WaitingOption)
        assert isinstance(opt.response, TextResponse)
        assert opt.response.text == str(i)


# ============================================================ watcher ticks


def test_tick_no_active_turn_is_noop() -> None:
    tmux = _FakeTmux(pane_text=_PROMPT_YES_NO)
    registry = Registry()
    # Binding without an active turn
    registry.set(
        ChatBinding(
            chat_id="x",
            window=cast("WindowInfo", _StubWindow()),
            cwd=Path("/tmp"),
        )
    )
    log: list[str] = []
    watcher = _make_watcher(tmux=tmux, registry=registry, on_change_log=log)
    binding = registry.get("x")
    assert binding is not None
    watcher._tick_binding(binding)
    assert log == []
    # capture_pane shouldn't have been called either
    assert tmux.calls == []


def test_tick_sets_waiting_when_prompt_appears() -> None:
    tmux = _FakeTmux(pane_text=_PROMPT_YES_NO)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    log: list[str] = []
    watcher = _make_watcher(tmux=tmux, registry=registry, on_change_log=log)

    watcher._tick_binding(binding)

    assert binding.current_turn is not None
    assert binding.current_turn.waiting_for is not None
    assert binding.current_turn.waiting_for.source == "permission"
    assert len(binding.current_turn.waiting_for.options) == 2
    assert log == ["oc_x"]


def test_tick_same_prompt_is_idempotent_no_callback() -> None:
    tmux = _FakeTmux(pane_text=_PROMPT_YES_NO)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    log: list[str] = []
    watcher = _make_watcher(tmux=tmux, registry=registry, on_change_log=log)

    watcher._tick_binding(binding)
    watcher._tick_binding(binding)
    watcher._tick_binding(binding)

    # Only the first transition fires the callback
    assert log == ["oc_x"]
    assert binding.current_turn is not None
    assert binding.current_turn.waiting_for is not None


def test_tick_different_prompt_updates_waiting() -> None:
    tmux = _FakeTmux(pane_text=_PROMPT_YES_NO)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    log: list[str] = []
    watcher = _make_watcher(tmux=tmux, registry=registry, on_change_log=log)

    watcher._tick_binding(binding)
    first_fp = (
        binding.current_turn.waiting_for.fingerprint
        if binding.current_turn and binding.current_turn.waiting_for
        else None
    )

    tmux.pane_text = _PROMPT_DIFFERENT
    watcher._tick_binding(binding)

    assert binding.current_turn is not None
    assert binding.current_turn.waiting_for is not None
    second_fp = binding.current_turn.waiting_for.fingerprint
    assert first_fp != second_fp
    assert log == ["oc_x", "oc_x"]


def test_tick_clears_waiting_when_prompt_disappears() -> None:
    tmux = _FakeTmux(pane_text=_PROMPT_YES_NO)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    log: list[str] = []
    watcher = _make_watcher(tmux=tmux, registry=registry, on_change_log=log)

    watcher._tick_binding(binding)
    assert binding.current_turn is not None
    assert binding.current_turn.waiting_for is not None

    tmux.pane_text = _NO_PROMPT
    watcher._tick_binding(binding)
    assert binding.current_turn.waiting_for is None
    assert log == ["oc_x", "oc_x"]


def test_tick_no_prompt_when_already_none_is_noop() -> None:
    tmux = _FakeTmux(pane_text=_NO_PROMPT)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    log: list[str] = []
    watcher = _make_watcher(tmux=tmux, registry=registry, on_change_log=log)

    watcher._tick_binding(binding)
    watcher._tick_binding(binding)
    assert log == []
    assert binding.current_turn is not None
    assert binding.current_turn.waiting_for is None


def test_tick_tmux_error_does_not_crash_or_call_callback() -> None:
    tmux = _FakeTmux(raise_on_capture=TmuxError("window gone"))
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    log: list[str] = []
    watcher = _make_watcher(tmux=tmux, registry=registry, on_change_log=log)

    watcher._tick_binding(binding)
    assert log == []


def test_tick_callback_exception_does_not_crash_watcher() -> None:
    tmux = _FakeTmux(pane_text=_PROMPT_YES_NO)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)

    def _bad(_b: ChatBinding) -> None:
        raise RuntimeError("simulated")

    watcher = PaneWatcher(
        registry=registry,
        tmux=cast("Any", tmux),
        on_change=_bad,
        interval_s=10.0,
    )
    # Must not raise
    watcher._tick_binding(binding)
    # Waiting state still got set
    assert binding.current_turn is not None
    assert binding.current_turn.waiting_for is not None


# ===================================================== mode reconciliation

_PANE_ACCEPT_EDITS = "doing work...\n⏵⏵ accept edits on (shift+tab to cycle)"
_PANE_PLAN = "thinking...\n⏸ plan mode on (shift+tab to cycle)"


def test_tick_syncs_mode_from_pane_and_fires_callback() -> None:
    tmux = _FakeTmux(pane_text=_PANE_ACCEPT_EDITS)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    log: list[str] = []
    watcher = _make_watcher(tmux=tmux, registry=registry, on_change_log=log)

    watcher._tick_binding(binding)

    assert binding.current_mode == "acceptEdits"
    assert binding.current_turn is not None
    assert binding.current_turn.accumulator.current_mode == "acceptEdits"
    assert log == ["oc_x"]


def test_tick_same_mode_is_idempotent() -> None:
    tmux = _FakeTmux(pane_text=_PANE_ACCEPT_EDITS)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    log: list[str] = []
    watcher = _make_watcher(tmux=tmux, registry=registry, on_change_log=log)

    watcher._tick_binding(binding)
    watcher._tick_binding(binding)

    # Only the first transition fires a callback
    assert log == ["oc_x"]
    assert binding.current_mode == "acceptEdits"


def test_tick_no_banner_does_not_force_default() -> None:
    """A capture with no mode-line must NOT flip a known mode back to default
    — it could be a transient miss or a banner hidden mid-run."""
    tmux = _FakeTmux(pane_text=_PANE_ACCEPT_EDITS)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    log: list[str] = []
    watcher = _make_watcher(tmux=tmux, registry=registry, on_change_log=log)

    watcher._tick_binding(binding)
    assert binding.current_mode == "acceptEdits"

    # Next tick: no banner in the pane.
    tmux.pane_text = _NO_PROMPT
    watcher._tick_binding(binding)
    # Mode is unchanged; the absent banner did not force "default".
    assert binding.current_mode == "acceptEdits"
    assert log == ["oc_x"]  # no second callback from the (non-)mode change


def test_tick_mode_change_updates_to_new_mode() -> None:
    tmux = _FakeTmux(pane_text=_PANE_ACCEPT_EDITS)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    log: list[str] = []
    watcher = _make_watcher(tmux=tmux, registry=registry, on_change_log=log)

    watcher._tick_binding(binding)
    assert binding.current_mode == "acceptEdits"

    tmux.pane_text = _PANE_PLAN
    watcher._tick_binding(binding)
    assert binding.current_mode == "plan"
    assert log == ["oc_x", "oc_x"]
