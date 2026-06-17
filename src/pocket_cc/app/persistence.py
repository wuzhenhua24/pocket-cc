"""Binding registry + on-disk state snapshot.

Concepts:
  - ChatBinding   — long-lived state per Lark chat (one tmux window, one cwd,
                    the live transcript reader).
  - TurnState     — short-lived state per "running card" (= per user message);
                    owns the CardStream + Accumulator.
  - Registry      — thread-safe dict of ChatBindings (in-memory).
  - StateStore    — writes the registry's "hard" + "card pointer" fields to a
                    JSON file so a restarted process can recover bindings
                    instead of orphaning users + cards. See M2 §持久化.

Locking model: the Registry has a coarse lock around the mapping itself.
:class:`TurnState` and :class:`ChatBinding` are plain data — they carry no
lock. All concurrent access to a binding's turn state is serialized by its
:class:`pocket_cc.relay.turn_controller.TurnController`, which is the single
owner of those mutations (see that module for the threading model).

The StateStore writes opportunistically at fixed turn-lifecycle events
(binding created / mode changed / turn opened / card rotated / turn sealed).
It never reads back what it wrote — restore is Step 2's job; Step 1 only
establishes the write path.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pocket_cc.claude.transcript import TranscriptReader
    from pocket_cc.relay.card_renderer import TurnAccumulator
    from pocket_cc.relay.card_stream import CardStream
    from pocket_cc.relay.waiting import WaitingFor
    from pocket_cc.tmux import WindowInfo

logger = logging.getLogger(__name__)


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
    and opens a fresh card. ``card_id``, ``card_message_id`` and
    ``card_stream`` are all replaced in place. ``is_continuation`` flips
    True after the first rotation so subsequent cards get the "(续)" title
    prefix. The accumulator's commit cursor tracks what's already been
    sealed.

    Two ids are kept side-by-side because they index two different things:

    * ``card_id`` is the cardkit handle — the streaming PUT target and
      the persisted pointer used to flip an orphan card to "restart"
      state on the next process boot.
    * ``card_message_id`` is the IM message id — what card_action
      callbacks come back on (the event carries message_id, not card_id),
      and what the registry's ``find_by_card_message_id`` reverse lookup
      keys on.
    """

    card_id: str
    card_message_id: str
    card_stream: CardStream
    accumulator: TurnAccumulator
    waiting_for: WaitingFor | None = None
    is_continuation: bool = False


@dataclass
class ChatBinding:
    """Per-Lark-chat persistent state."""

    chat_id: str
    # The Lark `open_id` of the user this binding was created for. In DM
    # (the only supported chat_type) chat_id ↔ open_id is 1:1, but storing
    # it explicitly lets restore look up the user's current config workspace
    # without a reverse-lookup over `Config.users` keyed by chat_id (which
    # wouldn't work anyway — chat_id isn't a user identity).
    open_id: str
    window: WindowInfo
    cwd: Path
    transcript_path: Path | None = None
    transcript_reader: TranscriptReader | None = None
    # Claude session id (the `<uuid>` in `<uuid>.jsonl`) once a SessionStart
    # hook has bound this chat to a concrete session. Non-None means the hook
    # channel is authoritative for (re)binding — the transcript poller then
    # stops guessing the active file by mtime, so a second Claude writing in
    # the same cwd can neither hijack the mirror nor override the hook lock.
    # Stays None in degraded mode (hooks not installed), where the poller's
    # mtime adoption remains the only binding mechanism. See M-④.
    session_id: str | None = None
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
    # Latest permission mode seen on this binding's transcript (session-
    # scoped). The bootstrap updates this on every `permission-mode`
    # record (including ones that arrive between turns, when no
    # accumulator is around to absorb them). New turns inherit it on
    # open so the Mode button label doesn't briefly flash "默认" when a
    # session is already in (e.g.) acceptEdits.
    current_mode: str = "default"


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


# ---------------------------------------------------------------- StateStore

# Bumped only when the on-disk shape changes incompatibly. A mismatch on
# load() is treated as "no state" so an old snapshot can't crash startup;
# we'd rather drop bindings and let users re-trigger them than die.
#
# v2: ChatBinding gained the required `open_id` field (M3 step3) — pre-v2
# snapshots can't be safely restored because the per-binding routing needs
# it. Old snapshots get dropped and users re-create bindings on next message.
#
# v3: active_card persists ``card_id`` alongside ``message_id``. The orphan
# patch on restart now goes through cardkit (update_card_entity targets
# card_id), so a snapshot without card_id can't be used. Phase 3.4 of the
# cardkit migration; pre-v3 snapshots get dropped — early-stage project,
# no compat requirement.
_SCHEMA_VERSION: Final[int] = 3


class StateStore:
    """JSON-on-disk snapshot of the live Registry.

    Holds a reference to the Registry it serves so callers (TurnController,
    InputRouter) only need a single ``store.save()`` invocation at each
    write point — they don't pass the registry through.

    Atomicity: writes go to a tempfile in the target dir, get fsync'd, then
    ``os.replace``'d onto the final path. Same-directory rename is atomic on
    POSIX so a crashed process either leaves the previous good file or the
    new good file — never a half-written one.

    Concurrency: an internal lock serializes concurrent saves. Snapshot
    reads of binding fields are pointer-atomic in Python, so a concurrent
    mutation in another binding shows up as the old or new value — never
    a torn read.

    Step-1 scope: write-only. ``load()`` parses the file (or returns None
    on missing / corrupt / version-mismatch) but no caller consumes it yet
    — Step 2 wires restoration into bootstrap.
    """

    def __init__(self, path: Path, registry: Registry) -> None:
        self._path = path
        self._registry = registry
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def save(self) -> None:
        """Snapshot the registry and atomically write it to disk.

        Logs and swallows IO / serialization errors — persistence is a
        best-effort observer of the live system; a failed save must never
        take down the running turn.
        """
        try:
            snapshot = self._snapshot()
            payload = json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False)
        except Exception:
            logger.warning("state save: snapshot/serialize failed", exc_info=True)
            return
        with self._lock:
            try:
                self._write_atomic(payload)
            except OSError:
                logger.warning("state save: atomic write failed", exc_info=True)

    def load(self) -> dict[str, Any] | None:
        """Read the snapshot back as a raw dict, or None when unusable.

        Returns None for: missing file, unreadable file, malformed JSON,
        non-dict top level, or schema-version mismatch. The intent is that
        startup degrades to "empty Registry" rather than crashes — a corrupt
        state file shouldn't keep the bot down.
        """
        with self._lock:
            try:
                raw = self._path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            except OSError:
                logger.warning("state load: read failed", exc_info=True)
                return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("state load: corrupt JSON — ignoring snapshot")
            return None
        if not isinstance(data, dict):
            logger.warning("state load: top-level is not an object — ignoring snapshot")
            return None
        if data.get("version") != _SCHEMA_VERSION:
            logger.warning(
                "state load: version mismatch — ignoring snapshot",
                extra={"got": data.get("version"), "want": _SCHEMA_VERSION},
            )
            return None
        return data

    # -------------------------------------------------------------- internal

    def _snapshot(self) -> dict[str, Any]:
        bindings: dict[str, dict[str, Any]] = {}
        for b in self._registry.all():
            entry: dict[str, Any] = {
                "open_id": b.open_id,
                "window_id": b.window.window_id,
                "window_name": b.window.name,
                "cwd": str(b.cwd),
                "created_at": b.created_at,
                "excluded_transcripts": sorted(str(p) for p in b.excluded_transcripts),
                "current_mode": b.current_mode,
                "active_card": None,
            }
            turn = b.current_turn
            if turn is not None:
                entry["active_card"] = {
                    "card_id": turn.card_id,
                    "message_id": turn.card_message_id,
                    "is_continuation": turn.is_continuation,
                }
            bindings[b.chat_id] = entry
        return {"version": _SCHEMA_VERSION, "bindings": bindings}

    def _write_atomic(self, payload: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=self._path.name + ".",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(self._path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
