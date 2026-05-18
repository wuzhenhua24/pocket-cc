"""Parse Claude Code TUI pane text to detect interactive prompts.

The Permission prompt (and AskUserQuestion / ExitPlanMode in M2-A/B) does
**not** appear in the JSONL transcript — it's a TUI dialog rendered straight
to the terminal. To project it into a Lark card we have to peek at the tmux
pane and recognize it ourselves.

`tmux capture-pane -p` strips ANSI by default (no `-e`), so the input is
already plain text and we can match on it with regex. If we later need
cursor state (which option is highlighted by `❯`) we'll layer pyte on top;
M2-C uses the `❯` glyph directly since it's preserved in plain capture.

Single public entry point: :func:`inspect_pane`. Returns None when the pane
doesn't look like a known prompt — never raises. Detection is conservative
(prefer "no prompt detected" over a false positive that misroutes user input).

Currently detects:
  - **Permission prompt** — "Do you want to proceed?" followed by a numbered
    option list, ended by an "Esc to cancel" footer. Triggered when Claude
    is about to run a Bash/Edit/Write call that needs user approval.

Not yet detected (future M2-A/B work using this same module):
  - AskUserQuestion menus (multi-question, richer options)
  - ExitPlanMode confirmation
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final, Literal

PromptKind = Literal["permission"]

# Core sentinel for the Permission prompt. Match-exact (whole line) on
# the visible question text — Claude emits it on its own line.
_PERMISSION_QUESTION_RE: Final[re.Pattern[str]] = re.compile(r"^\s*Do you want to proceed\?\s*$")

# Numbered option: optional `❯` cursor marker, integer, ". ", label.
# The marker shows which option is currently highlighted (Claude's default
# is option 1). Label may contain spaces but no newlines.
_OPTION_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(❯)?\s*(\d+)\.\s+(.+?)\s*$")

# Footer that bounds the option list from below.
_FOOTER_HINT_RE: Final[re.Pattern[str]] = re.compile(r"Esc to cancel|Tab to amend")

# How many lines above the question to look for context (the command being
# asked about + any rationale lines).
_CONTEXT_LOOKBACK_LINES: Final[int] = 12

# Cap options so a malformed pane (e.g. "999. ..." far below) can't cause
# unbounded scanning.
_OPTIONS_LOOKAHEAD_LINES: Final[int] = 12


@dataclass(frozen=True, slots=True)
class ParsedOption:
    """One numbered option in a TUI prompt."""

    number: int  # 1, 2, …
    label: str  # "Yes" / "No" / etc.
    selected: bool  # True iff Claude's `❯` cursor is on this row


@dataclass(frozen=True, slots=True)
class ParsedPrompt:
    """A TUI prompt detected in the pane text."""

    kind: PromptKind
    question: str  # the line shown to the user (e.g. "Do you want to proceed?")
    context: str  # what's being asked about — Bash command, file path, etc.
    options: tuple[ParsedOption, ...]
    fingerprint: str  # stable hash, used to dedupe across tick reads


def inspect_pane(pane_text: str) -> ParsedPrompt | None:
    """Return the prompt currently shown in ``pane_text``, or None.

    Only the *latest* prompt is returned — earlier ones (scrolled up but
    still visible in the capture) are ignored, because the user can only
    interact with the bottom-most one. We do this by scanning from the
    bottom upward for the question sentinel.
    """
    if not pane_text:
        return None

    lines = pane_text.rstrip("\n").splitlines()
    if not lines:
        return None

    # Find the latest occurrence of the permission question.
    question_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if _PERMISSION_QUESTION_RE.match(lines[i]):
            question_idx = i
            break
    if question_idx is None:
        return None

    options = _extract_options(lines, start=question_idx + 1)
    if not options:
        # A "Do you want to proceed?" without options is suspicious —
        # likely a stale render. Refuse to treat it as an active prompt.
        return None

    context = _extract_context(lines, end=question_idx)
    question = lines[question_idx].strip()

    fingerprint = _fingerprint(question, context, options)

    return ParsedPrompt(
        kind="permission",
        question=question,
        context=context,
        options=options,
        fingerprint=fingerprint,
    )


# -------------------------------------------------------------------- helpers


def _extract_options(lines: list[str], *, start: int) -> tuple[ParsedOption, ...]:
    """Pull numbered options from ``lines[start:]`` until a terminator.

    Terminator: blank line, footer hint ("Esc to cancel"), or end of capture.
    Lines that don't match the option pattern are also a terminator (so we
    don't accidentally swallow command output that appears after the prompt
    on a stale screen).
    """
    out: list[ParsedOption] = []
    end = min(start + _OPTIONS_LOOKAHEAD_LINES, len(lines))
    for i in range(start, end):
        line = lines[i]
        if not line.strip():
            # Blank line — typically separates options from the footer
            if out:
                break
            continue
        if _FOOTER_HINT_RE.search(line):
            break
        match = _OPTION_RE.match(line)
        if not match:
            # Stop on the first non-option line after we've seen at least one
            # option — prevents accidentally absorbing unrelated text.
            if out:
                break
            continue
        cursor, number_str, label = match.groups()
        try:
            number = int(number_str)
        except ValueError:
            continue
        out.append(ParsedOption(number=number, label=label.strip(), selected=cursor == "❯"))
    return tuple(out)


def _extract_context(lines: list[str], *, end: int) -> str:
    """Lines above the question — the command/file being asked about.

    We grab up to ``_CONTEXT_LOOKBACK_LINES``, dropping leading/trailing
    blanks. The result is rendered into the Lark card body so the user
    knows what they're approving.
    """
    start = max(0, end - _CONTEXT_LOOKBACK_LINES)
    chunk = lines[start:end]
    # Trim leading/trailing blank lines without touching internal whitespace
    while chunk and not chunk[0].strip():
        chunk = chunk[1:]
    while chunk and not chunk[-1].strip():
        chunk = chunk[:-1]
    return "\n".join(chunk)


def _fingerprint(question: str, context: str, options: tuple[ParsedOption, ...]) -> str:
    """Stable hash so the watcher can detect same-prompt vs new-prompt across ticks.

    Intentionally **excludes** the cursor `selected` state — the user
    moving the cursor in tmux doesn't change which prompt is active.
    """
    parts = [
        question,
        context,
        "\n".join(f"{o.number}. {o.label}" for o in options),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
