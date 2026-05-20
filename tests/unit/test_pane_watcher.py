"""Unit tests for relay/pane_watcher.py.

The watcher's job is now narrow: capture the pane, parse it, and hand the
parsed (waiting, mode) to the binding's controller. Applying that to turn
state lives in :class:`TurnController.apply_pane_state` (tested in
test_turn_controller.py), so here we cover the parse helpers and that the
watcher delegates with the right arguments.
"""

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

_NO_PROMPT = "$ all done\n$ "
_PANE_ACCEPT_EDITS = "doing work...\n⏵⏵ accept edits on (shift+tab to cycle)"


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


@dataclass
class _RecordingController:
    """Captures apply_pane_state(new_waiting, detected_mode) calls."""

    applied: list[tuple[WaitingFor | None, str | None]] = field(default_factory=list)

    def apply_pane_state(
        self, new_waiting: WaitingFor | None, detected_mode: str | None
    ) -> None:
        self.applied.append((new_waiting, detected_mode))


class _FakeAny:
    """Stand-in for typed dependencies the watcher doesn't actually touch."""

    def __getattr__(self, name: str) -> Any:
        return self


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


def _make_watcher(
    *, tmux: _FakeTmux, registry: Registry, controller: _RecordingController
) -> PaneWatcher:
    return PaneWatcher(
        registry=registry,
        tmux=cast("Any", tmux),
        controller_for=lambda _b: cast("Any", controller),
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


def test_tick_no_active_turn_skips_capture_and_controller() -> None:
    tmux = _FakeTmux(pane_text=_PROMPT_YES_NO)
    registry = Registry()
    registry.set(
        ChatBinding(
            chat_id="x",
            window=cast("WindowInfo", _StubWindow()),
            cwd=Path("/tmp"),
        )
    )
    controller = _RecordingController()
    watcher = _make_watcher(tmux=tmux, registry=registry, controller=controller)
    binding = registry.get("x")
    assert binding is not None

    watcher._tick_binding(binding)

    assert controller.applied == []
    assert tmux.calls == []  # no active turn → capture skipped


def test_tick_delegates_waiting_for_when_prompt_present() -> None:
    tmux = _FakeTmux(pane_text=_PROMPT_YES_NO)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    controller = _RecordingController()
    watcher = _make_watcher(tmux=tmux, registry=registry, controller=controller)

    watcher._tick_binding(binding)

    assert len(controller.applied) == 1
    new_waiting, detected_mode = controller.applied[0]
    assert isinstance(new_waiting, WaitingFor)
    assert new_waiting.source == "permission"
    assert len(new_waiting.options) == 2
    assert detected_mode is None  # no banner in this pane


def test_tick_delegates_none_when_no_prompt() -> None:
    tmux = _FakeTmux(pane_text=_NO_PROMPT)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    controller = _RecordingController()
    watcher = _make_watcher(tmux=tmux, registry=registry, controller=controller)

    watcher._tick_binding(binding)

    assert controller.applied == [(None, None)]


def test_tick_delegates_detected_mode() -> None:
    tmux = _FakeTmux(pane_text=_PANE_ACCEPT_EDITS)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    controller = _RecordingController()
    watcher = _make_watcher(tmux=tmux, registry=registry, controller=controller)

    watcher._tick_binding(binding)

    assert len(controller.applied) == 1
    _new_waiting, detected_mode = controller.applied[0]
    assert detected_mode == "acceptEdits"


def test_tick_tmux_error_does_not_delegate() -> None:
    tmux = _FakeTmux(raise_on_capture=TmuxError("window gone"))
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)
    controller = _RecordingController()
    watcher = _make_watcher(tmux=tmux, registry=registry, controller=controller)

    watcher._tick_binding(binding)

    assert controller.applied == []


def test_tick_controller_exception_does_not_crash_watcher() -> None:
    tmux = _FakeTmux(pane_text=_PROMPT_YES_NO)
    registry = Registry()
    binding = _make_binding_with_turn()
    registry.set(binding)

    class _Bad:
        def apply_pane_state(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("simulated")

    watcher = PaneWatcher(
        registry=registry,
        tmux=cast("Any", tmux),
        controller_for=lambda _b: cast("Any", _Bad()),
        interval_s=10.0,
    )
    # Must not raise.
    watcher._tick_binding(binding)
