"""Throttled card-patch worker.

One :class:`CardStream` per Lark card (= per turn). The relay layer pushes
the latest desired card via :meth:`update`; a background thread flushes
that card to Lark's PATCH endpoint at most once per ``interval_s`` and skips
flushes when the card hasn't actually changed (hash-based de-dup).

Why a thread, not asyncio: the rest of pocket-cc is sync (Lark SDK's WS
callbacks fire on a worker thread, and our handlers are plain functions).
We don't want to drag an event loop into this just for one feature.

Closing semantics:
  - `close(final_card, *, flush=True, retries=, backoff_s=)` swaps in
    `final_card` (if given) and forces one last patch. Unlike the best-effort
    background tick, this final flush **retries** on failure and **returns a
    bool** (True = delivered) so the caller can react — the terminal patch is
    the only signal to the user that the turn ended, so it can't be silently
    dropped. Used to render the "done" / "failed" / "cancelled" state.
  - After `close()`, further `update()` calls are silent no-ops.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pocket_cc.lark.client import LarkClient

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 1.5


class CardStream:
    """Throttled streaming-updates owner for a single Lark card."""

    def __init__(
        self,
        client: LarkClient,
        message_id: str,
        *,
        interval_s: float = _DEFAULT_INTERVAL_S,
    ) -> None:
        self._client = client
        self._message_id = message_id
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._pending: dict[str, Any] | None = None
        self._last_hash: str | None = None
        self._closed = threading.Event()
        self._wakeup = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"card-stream-{message_id}", daemon=True
        )
        self._started = False

    @property
    def message_id(self) -> str:
        return self._message_id

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def update(self, card: dict[str, Any]) -> None:
        """Replace the pending card with ``card``. Next tick will flush if changed."""
        if self._closed.is_set():
            return
        with self._lock:
            self._pending = card
        self._wakeup.set()

    def close(
        self,
        final_card: dict[str, Any] | None = None,
        *,
        flush: bool = True,
        retries: int = 0,
        backoff_s: float = 0.0,
    ) -> bool:
        """Stop the worker, then (if ``flush``) send the final PATCH.

        Returns True if the final card was delivered (or there was nothing to
        flush / ``flush`` is False), False if every attempt failed. The final
        flush retries up to ``retries`` extra times with ``backoff_s`` between
        attempts — it carries the turn's terminal state, so a transient failure
        must not silently leave the user on a "running" card. ``retries=0``
        (the default) preserves the old best-effort single attempt, used for
        rotation's intermediate sealing cards.
        """
        if final_card is not None:
            with self._lock:
                self._pending = final_card
        self._closed.set()
        self._wakeup.set()
        if self._started:
            self._thread.join(timeout=5)
        if not flush:
            return True
        return self._flush_final(retries=retries, backoff_s=backoff_s)

    # -------------------------------------------------------------- internal

    def _run(self) -> None:
        while not self._closed.is_set():
            # wait_for either the interval or an early-wakeup signal
            self._wakeup.wait(timeout=self._interval_s)
            self._wakeup.clear()
            if self._closed.is_set():
                break
            self._flush_once()

    def _flush_once(self) -> None:
        with self._lock:
            if self._pending is None:
                return
            card = self._pending
        h = _card_hash(card)
        if h == self._last_hash:
            return
        try:
            self._client.patch_card(self._message_id, card)
            self._last_hash = h
        except Exception:
            # Network / API failures are surfaced as warnings and retried on
            # the next tick (the hash isn't updated, so the same card stays
            # in the "to send" slot).
            logger.warning(
                "card patch failed", extra={"message_id": self._message_id}, exc_info=True
            )

    def _flush_final(self, *, retries: int, backoff_s: float) -> bool:
        """Send the terminal card, retrying on failure. Returns delivery success.

        Runs after the worker thread has stopped (no tick will retry for us),
        so this is the turn's last chance to show its done/failed state. The
        hash de-dupe still applies: if the worker already flushed an identical
        card, there's nothing left to send and we report success.
        """
        with self._lock:
            if self._pending is None:
                return True
            card = self._pending
        h = _card_hash(card)
        if h == self._last_hash:
            return True
        for attempt in range(retries + 1):
            try:
                self._client.patch_card(self._message_id, card)
                self._last_hash = h
                return True
            except Exception:
                logger.warning(
                    "final card patch attempt failed",
                    extra={"message_id": self._message_id, "attempt": attempt + 1},
                    exc_info=True,
                )
                if attempt < retries and backoff_s > 0:
                    time.sleep(backoff_s)
        return False


def _card_hash(card: dict[str, Any]) -> str:
    """Stable hash of a card dict — order-independent so equivalent dicts dedupe."""
    return hashlib.sha256(
        json.dumps(card, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
