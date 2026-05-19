"""In-memory binding registry — chat_id → tmux window + active turn.

M1 keeps this purely in memory; restarting the bot drops all bindings (and
the user can just re-message the bot to re-create them on demand). M2 will
add a JSON dump for persistence across restarts, when there's a real
"multi-day continuity" story.

Concepts:
  - ChatBinding   — long-lived state per Lark chat (one tmux window, one cwd,
                    the live transcript reader).
  - TurnState     — short-lived state per "running card" (= per user message);
                    owns the CardStream + Accumulator.
  - Registry      — thread-safe dict of ChatBindings.

Locking model: the Registry has a coarse lock around the mapping itself.
Per-binding state mutations are not lock-protected here — callers (input
router, output poller) must coordinate their accesses. In M1 there are only
two writers (the WS thread for input, the poller thread for output) and
they touch different fields, so this is fine without finer locks.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — used as dataclass field type, must be eager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pocket_cc.claude.transcript import TranscriptReader
    from pocket_cc.relay.card_renderer import TurnAccumulator
    from pocket_cc.relay.card_stream import CardStream
    from pocket_cc.relay.waiting import WaitingFor
    from pocket_cc.tmux import WindowInfo


@dataclass
class TurnState:
    """One user message → (possibly several) cards → one accumulator + stream.

    Spans from the moment the user sends a message until either:
      - the next user message arrives (we close this turn, start a new one), or
      - shutdown.

    The `waiting_for` field (None by default) flips this turn into a
    "waiting on user response" state when Claude TUI shows a prompt
    (Permission / AskUserQuestion / Plan). While non-None, an incoming Lark
    text message is treated as a *continuation* of this turn (= user
    answering the prompt) rather than a new turn. Cleared back to None
    when the user responds, or when the detector notices the prompt is
    gone. See `relay.waiting`.

    Card rotation (M2-F): when an active card's body grows past the
    rotation threshold, the bootstrap seals it (with a "⏬ 续下条" footer)
    and opens a fresh card. ``card_message_id`` and ``card_stream`` are
    replaced in place. ``is_continuation`` flips True after the first
    rotation so subsequent cards get the "(续)" title prefix. The
    accumulator's commit cursor tracks what's already been sealed.
    """

    card_message_id: str
    card_stream: CardStream
    accumulator: TurnAccumulator
    waiting_for: WaitingFor | None = None
    is_continuation: bool = False
    # Set when the user clicks ⏹ 中断 *before* `_open_turn`'s deferred Enter
    # has fired. The Enter worker (in `relay.input._open_turn`) checks this
    # flag right before sending Enter — if set, the prompt is dropped so it
    # never reaches Claude. Without this, an early cancel races with the
    # send_text Enter and the prompt gets submitted anyway (visible as
    # "leftover text in input" after C-c + Escape ran but couldn't clean
    # what hadn't yet been submitted).
    cancel_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class ChatBinding:
    """Per-Lark-chat persistent state."""

    chat_id: str
    window: WindowInfo
    cwd: Path
    transcript_path: Path | None = None
    transcript_reader: TranscriptReader | None = None
    current_turn: TurnState | None = None
    # UNIX timestamp captured when the binding was created. The transcript
    # poller uses this as a `after_ts` cutoff so historical jsonls from
    # previous Claude sessions in the same cwd (e.g. desktop use, prior runs)
    # are ignored — only files modified after the bot spawned its Claude
    # process can be the active transcript. See M1-D-14.
    created_at: float = field(default_factory=time.time)
    # Set of `.jsonl` paths that already existed under this cwd when the
    # binding was created. Excluded from the active-transcript search so a
    # concurrent Claude session in the same cwd (e.g. user's desktop Claude
    # working on the same project) can keep mtime-bumping its transcript
    # without us mistakenly adopting it. The pocket-cc-spawned Claude will
    # create a brand-new jsonl with a fresh uuid, which won't be in this set.
    # See M1-D-15.
    excluded_transcripts: frozenset[Path] = field(default_factory=frozenset)
    # We expose a per-binding sentinel so future hooks can serialize updates
    # without forcing all callers through the registry lock.
    lock: threading.Lock = field(default_factory=threading.Lock)


class Registry:
    """Thread-safe registry of `chat_id → ChatBinding`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bindings: dict[str, ChatBinding] = {}

    def get(self, chat_id: str) -> ChatBinding | None:
        with self._lock:
            return self._bindings.get(chat_id)

    def set(self, binding: ChatBinding) -> None:
        with self._lock:
            self._bindings[binding.chat_id] = binding

    def remove(self, chat_id: str) -> ChatBinding | None:
        with self._lock:
            return self._bindings.pop(chat_id, None)

    def find_by_card_message_id(self, message_id: str) -> ChatBinding | None:
        """Reverse lookup for card-action callbacks."""
        with self._lock:
            for b in self._bindings.values():
                if b.current_turn and b.current_turn.card_message_id == message_id:
                    return b
            return None

    def all(self) -> list[ChatBinding]:
        """Snapshot of current bindings — safe to iterate without holding lock."""
        with self._lock:
            return list(self._bindings.values())

    def __iter__(self) -> Iterator[ChatBinding]:
        return iter(self.all())

    def __len__(self) -> int:
        with self._lock:
            return len(self._bindings)
