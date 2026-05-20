"""Background pane watcher — detects Claude TUI prompts and drives waiting state.

The transcript log (M1-B) doesn't carry TUI dialogs (Permission prompts,
AskUserQuestion menus). Those live only in the terminal display. This module
runs a thread that tails each active binding's tmux pane, parses out any
prompt via :func:`pocket_cc.claude.inspect_pane`, and pushes the result onto
the binding's `TurnState.waiting_for`.

The waiting state itself is consumed by :func:`render_card` (M2-0): when
present, the card flips to ❓ orange with option buttons. The
:class:`InputRouter` (also M2-0) reads `waiting_for` to decide whether the
next Lark message is a *reply to the prompt* (continuation) or a *new turn*.

Bootstrap wires :attr:`on_change` to re-render and patch the Lark card via
the existing :class:`CardStream`. We only fire `on_change` on actual
transitions (set / replaced / cleared); same-prompt ticks are no-ops.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from pocket_cc.claude.pane_inspector import ParsedPrompt, detect_mode, inspect_pane
from pocket_cc.relay.waiting import TextResponse, WaitingFor, WaitingOption
from pocket_cc.tmux import TmuxError

if TYPE_CHECKING:
    from pocket_cc.app.persistence import ChatBinding, Registry
    from pocket_cc.relay.turn_controller import TurnController
    from pocket_cc.tmux import TmuxManager

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 1.0
# How many lines from the bottom of the pane to capture. Claude's TUI fits
# any single prompt in <30 lines, so 60 leaves plenty of headroom while
# keeping capture cheap.
_CAPTURE_LINES = 60

ControllerProvider = Callable[["ChatBinding"], "TurnController"]


class PaneWatcher:
    """One thread, all bindings — ticks pane → hands prompt/mode to the controller.

    The watcher only does the *observation* (capture + parse). Applying the
    result to turn state (waiting_for / mode) and re-rendering is the
    controller's job, so all turn-state mutation stays serialized there.
    """

    def __init__(
        self,
        registry: Registry,
        tmux: TmuxManager,
        *,
        controller_for: ControllerProvider,
        interval_s: float = _DEFAULT_INTERVAL_S,
    ) -> None:
        self._registry = registry
        self._tmux = tmux
        self._controller_for = controller_for
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pane-watcher", daemon=True)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._started:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------- internal

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for binding in self._registry.all():
                    self._tick_binding(binding)
            except Exception:
                logger.warning("pane watcher tick raised", exc_info=True)
            self._stop.wait(self._interval_s)

    def _tick_binding(self, binding: ChatBinding) -> None:
        if binding.current_turn is None:
            return

        try:
            pane = self._tmux.capture_pane(binding.window.window_id, lines=_CAPTURE_LINES)
        except TmuxError:
            logger.debug(
                "pane capture failed (window probably gone)",
                extra={"chat_id": binding.chat_id},
            )
            return

        prompt = inspect_pane(pane)
        new_waiting = _build_waiting_for(prompt) if prompt is not None else None
        # Permission mode is read from the same capture. detect_mode returns
        # None when no banner is shown — the controller *ignores* None rather
        # than forcing "default", so a transient capture miss (or a mode banner
        # hidden mid-run) can't flap the label. A switch back to default is
        # reconciled by the post-click readback or the next transcript record.
        detected_mode = detect_mode(pane)
        try:
            self._controller_for(binding).apply_pane_state(new_waiting, detected_mode)
        except Exception:
            logger.warning(
                "pane watcher apply_pane_state raised",
                extra={"chat_id": binding.chat_id},
                exc_info=True,
            )


def _build_waiting_for(prompt: ParsedPrompt) -> WaitingFor:
    """Convert a :class:`ParsedPrompt` into the relay-side waiting model.

    Each numbered option becomes a WaitingOption whose response is the
    number digit (Claude TUI accepts `1` / `2` as shortcuts — empirically
    verified). Cursor `selected` is NOT propagated: pocket-cc users see
    the same option list regardless of where Claude's TUI cursor sits.
    """
    options = tuple(
        WaitingOption(
            label=o.label,
            response=TextResponse(text=str(o.number)),
        )
        for o in prompt.options
    )
    question = _compose_question(prompt)
    return WaitingFor(
        source="permission",
        question=question,
        options=options,
        fingerprint=prompt.fingerprint,
    )


def _compose_question(prompt: ParsedPrompt) -> str:
    """Render context above the question for the Lark card body.

    e.g.::

        Bash command
          cd /Users/...; for d in app claude...; do echo ...; done
          List contents of each subdirectory
        Contains shell syntax (string) that cannot be statically analyzed

        Do you want to proceed?
    """
    if prompt.context:
        return f"{prompt.context}\n\n{prompt.question}"
    return prompt.question
