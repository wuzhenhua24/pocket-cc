"""Waiting state — Claude is blocked on user input, pocket-cc needs to mirror it.

When Claude Code's TUI shows a prompt (Permission / AskUserQuestion / Plan
approval), the running turn doesn't simply continue — it sits in a *waiting*
state until the user responds. pocket-cc projects that waiting state into a
❓ Lark card with option buttons.

This module defines the data shapes. Detection (when to enter waiting, when
to leave) is owned by M2-A (transcript-driven for AskUserQuestion) and
M2-C (pane-driven for Permission). M2-0 just establishes the contract.

Three pieces:
  - :class:`WaitingResponse` — what to send to Claude when user picks an
    option. Either text (auto-Enter) or a literal key sequence.
  - :class:`WaitingOption`  — one option the user can pick (label + response).
  - :class:`WaitingFor`     — the whole prompt: source / question / options /
    whether free-form text from the user is also allowed.

Whoever sets `TurnState.waiting_for = WaitingFor(...)` is also responsible
for clearing it (back to None) when the prompt is satisfied. The InputRouter
clears it when the user responds; M2-A/C will also clear it if Claude's
state changes out of band (user answered in tmux directly, prompt timed out, …).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Where this waiting state was learned from. Kept as a string literal so
# logs and metrics stay readable; not a hard-encoded behavior switch.
WaitingSource = Literal[
    "permission",  # Claude TUI permission prompt (M2-C, via pane)
    "ask_user_question",  # AskUserQuestion tool_use (M2-A, via transcript)
    "plan",  # ExitPlanMode tool_use (M2-B, deferred)
    "unknown",  # detected something but couldn't classify
]


@dataclass(frozen=True, slots=True)
class TextResponse:
    """Send this text to Claude (with Enter auto-appended)."""

    text: str


@dataclass(frozen=True, slots=True)
class KeysResponse:
    """Send a sequence of named tmux keys (e.g. ('Down','Down','Enter')).

    Used when the prompt UI needs cursor navigation rather than a literal
    answer string — e.g. picking item N from a Claude AskUserQuestion menu.
    Each key is delivered via :meth:`TmuxManager.send_key` (no 500ms gap;
    these are control keys, not user-text).
    """

    keys: tuple[str, ...]


WaitingResponse = TextResponse | KeysResponse


@dataclass(frozen=True, slots=True)
class WaitingOption:
    """One choice the user can pick from a waiting prompt.

    The button label shown on the Lark card is :attr:`label`. The response
    that goes back to Claude when the user taps it is :attr:`response`.
    Optional :attr:`description` shows up beneath the label in the card
    (used by AskUserQuestion which carries rich per-option descriptions).
    """

    label: str
    response: WaitingResponse
    description: str = ""


@dataclass(frozen=True, slots=True)
class WaitingFor:
    """A prompt Claude is currently blocked on.

    Attributes:
        source: Classifier — where we learned about this prompt. Determines
            nothing on its own (the response path is identical for all
            sources), but useful for logging / metrics / debugging.
        question: Main prompt text shown at the top of the waiting card.
            For Permission prompts, this is the "Do you want to proceed?"
            line + relevant context (command being asked about).
        options: Buttons the user can pick. Order = render order.
        allow_freeform_text: When True, the user can also reply with plain
            text in Lark instead of tapping a button; pocket-cc sends it
            verbatim via send_text. False = button-only (rare; only for
            prompts where free text would confuse Claude's parser).
        fingerprint: Optional opaque token used by the *detector* to dedupe.
            For tool_use-based prompts this is the tool_use_id; for pane-
            based prompts this is a content hash. Empty string = no dedupe.
    """

    source: WaitingSource
    question: str
    options: tuple[WaitingOption, ...] = field(default_factory=tuple)
    allow_freeform_text: bool = True
    fingerprint: str = ""
