"""Throttled cardkit card-update worker.

One :class:`CardStream` per Lark card (= per turn). The relay layer pushes
the latest desired card via :meth:`update`; a background thread flushes
that card to the cardkit endpoints at most once per ``interval_s`` and
skips flushes when nothing meaningful changed.

The stream picks one of two cardkit endpoints per tick:

* **Fast path** — only the body markdown changed (``element_id ==
  ELEMENT_ID_BODY``): push via
  :meth:`LarkClient.stream_element_content`. Cheap; the wire payload is
  just the delta text + sequence.
* **Slow path** — anything else changed (header / actions / detail / a
  state transition): push the whole card via
  :meth:`LarkClient.update_card_entity`. Same model as the legacy IM
  PATCH, just targeting cardkit's card_id.

Sequence numbers are strictly monotonic per ``card_id`` — required by the
cardkit endpoints. We start at 1 and ``+= 1`` on every wire call (gaps
are allowed but we don't introduce them).

Why a thread, not asyncio: the rest of pocket-cc is sync (Lark SDK's WS
callbacks fire on a worker thread, and our handlers are plain functions).
We don't want to drag an event loop into this just for one feature.

Closing semantics:
  - `close(final_card, *, flush=True, retries=, backoff_s=)` swaps in
    `final_card` (if given) and forces one last *slow-path* call — terminal
    state (done / failed / cancelled) always re-sends the whole card so
    the renderer can flip streaming_mode off, drop the action row, etc.
    Unlike the best-effort background tick, this final flush **retries**
    on failure and **returns a bool** (True = delivered) so the caller
    can react — the terminal call is the only signal to the user that the
    turn ended, so it can't be silently dropped.
  - After `close()`, further `update()` calls are silent no-ops.

Factory: :func:`open_card_stream` is the recommended entry point — it
bundles ``create_card_entity`` + ``send_card_id`` + ``CardStream(...)``
and primes the stream's hashes from the initial card so the first
``update(initial_card)`` is a true no-op instead of a redundant PUT.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from pocket_cc.lark.card import ELEMENT_ID_BODY
from pocket_cc.lark.client import LarkApiError

if TYPE_CHECKING:
    from pocket_cc.lark.client import LarkClient

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 1.5


class CardStream:
    """Throttled cardkit-update owner for a single card_id."""

    def __init__(
        self,
        client: LarkClient,
        card_id: str,
        *,
        initial_card: dict[str, Any] | None = None,
        interval_s: float = _DEFAULT_INTERVAL_S,
    ) -> None:
        self._client = client
        self._card_id = card_id
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._pending: dict[str, Any] | None = None
        # Hashes / values that describe what's currently on the server side.
        # `_skeleton_hash` excludes the ELEMENT_ID_BODY element's content
        # so a body-only diff doesn't bust the skeleton match. `_body_text`
        # is the exact string that body markdown element currently shows.
        # Both can be primed by `initial_card` (the dict used for the
        # initial cardkit create) so the first matching update() no-ops.
        self._skeleton_hash: str | None = None
        self._body_text: str | None = None
        if initial_card is not None:
            self._skeleton_hash = _skeleton_hash(initial_card)
            self._body_text = _extract_body_text(initial_card)
        self._sequence: int = 0
        self._closed = threading.Event()
        self._wakeup = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"card-stream-{card_id}", daemon=True
        )
        self._started = False

    @property
    def card_id(self) -> str:
        return self._card_id

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
        """Stop the worker, then (if ``flush``) send the terminal update.

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
        new_skeleton = _skeleton_hash(card)
        new_body = _extract_body_text(card)
        if new_skeleton == self._skeleton_hash and new_body == self._body_text:
            # No change since last successful write.
            return
        try:
            if new_skeleton != self._skeleton_hash:
                # Slow path: anything other than body content shifted (header
                # template, action buttons, detail section, mode label …) →
                # whole-card replace so cardkit re-renders everything.
                self._sequence += 1
                self._client.update_card_entity(
                    self._card_id, card, sequence=self._sequence
                )
                self._skeleton_hash = new_skeleton
                self._body_text = new_body
            else:
                # Fast path: only the body markdown grew. Push as element
                # content; skeleton (header/buttons/etc.) stays put.
                self._sequence += 1
                self._client.stream_element_content(
                    self._card_id,
                    ELEMENT_ID_BODY,
                    new_body or "",
                    sequence=self._sequence,
                )
                self._body_text = new_body
        except Exception:
            # Network / API failures: surface as warnings, leave the hashes
            # un-advanced so the next tick retries the same pending card.
            logger.warning(
                "card update failed",
                extra={"card_id": self._card_id, "sequence": self._sequence},
                exc_info=True,
            )

    def _flush_final(self, *, retries: int, backoff_s: float) -> bool:
        """Send the terminal card via slow path, retrying on failure.

        Terminal cards (done / failed / cancelled) always go through
        ``update_card_entity`` — they change state, header template, drop
        the action row, and flip streaming_mode off, none of which the
        body-only fast path can express.
        """
        with self._lock:
            if self._pending is None:
                return True
            card = self._pending
        new_skeleton = _skeleton_hash(card)
        new_body = _extract_body_text(card)
        if new_skeleton == self._skeleton_hash and new_body == self._body_text:
            # Background tick already delivered an identical card.
            return True
        for attempt in range(retries + 1):
            try:
                self._sequence += 1
                self._client.update_card_entity(
                    self._card_id, card, sequence=self._sequence
                )
                self._skeleton_hash = new_skeleton
                self._body_text = new_body
                return True
            except LarkApiError as e:
                logger.warning(
                    "final card update attempt failed",
                    extra={
                        "card_id": self._card_id,
                        "attempt": attempt + 1,
                        "sequence": self._sequence,
                        "kind": e.kind.value,
                        "code": e.code,
                    },
                    exc_info=True,
                )
                # Non-retryable kinds (format_error / target_revoked /
                # permission_denied) cannot succeed on retry — burning the
                # remaining attempts just delays the failure surface.
                if not e.retryable:
                    return False
                if attempt < retries and backoff_s > 0:
                    time.sleep(backoff_s)
            except Exception:
                # Network / SDK-level failures we couldn't classify — fall
                # through to the retry loop. These do tend to be transient.
                logger.warning(
                    "final card update attempt failed (unclassified)",
                    extra={
                        "card_id": self._card_id,
                        "attempt": attempt + 1,
                        "sequence": self._sequence,
                    },
                    exc_info=True,
                )
                if attempt < retries and backoff_s > 0:
                    time.sleep(backoff_s)
        return False


def open_card_stream(
    client: LarkClient,
    chat_id: str,
    initial_card: dict[str, Any],
    *,
    interval_s: float = _DEFAULT_INTERVAL_S,
) -> tuple[str, str, CardStream]:
    """Create a cardkit card, post its IM reference, and return a primed stream.

    Returns ``(card_id, message_id, stream)``. The caller is expected to
    keep both ids: ``message_id`` is what card-action callbacks come back on
    (the IM event carries the message_id, not the cardkit card_id), while
    ``card_id`` is the streaming target.

    The returned :class:`CardStream` has its hashes primed from
    ``initial_card``, so a subsequent ``stream.update(initial_card)`` is a
    no-op rather than a redundant first PUT. The stream is **not started**
    yet — the caller calls :meth:`CardStream.start` (so test code can
    decide whether to drive the worker or step it manually).
    """
    card_id = client.create_card_entity(initial_card)
    message_id = client.send_card_id(chat_id, card_id)
    stream = CardStream(
        client, card_id, initial_card=initial_card, interval_s=interval_s
    )
    return card_id, message_id, stream


# ------------------------------------------------------------------ helpers


def _extract_body_text(card: dict[str, Any]) -> str | None:
    """Return the content string of the ELEMENT_ID_BODY markdown element.

    Returns ``None`` if the element isn't present (e.g. a text-card variant
    that doesn't use the streaming layout) — callers treat that as
    "skeleton-only, no fast path available".
    """
    body_section = card.get("body")
    if not isinstance(body_section, dict):
        return None
    elements = body_section.get("elements")
    if not isinstance(elements, list):
        return None
    for el in elements:
        if isinstance(el, dict) and el.get("element_id") == ELEMENT_ID_BODY:
            content = el.get("content")
            return content if isinstance(content, str) else None
    return None


def _skeleton_hash(card: dict[str, Any]) -> str:
    """Hash of the card *minus* the ELEMENT_ID_BODY content field.

    Used to detect "skeleton" changes (anything other than the streaming
    body): header template flips, button row swap, detail section appearing
    or vanishing, state-transition rewrites. Deepcopy so we don't mutate
    the caller's dict.
    """
    skeleton = copy.deepcopy(card)
    body_section = skeleton.get("body")
    if isinstance(body_section, dict):
        elements = body_section.get("elements")
        if isinstance(elements, list):
            for el in elements:
                if isinstance(el, dict) and el.get("element_id") == ELEMENT_ID_BODY:
                    el.pop("content", None)
    return hashlib.sha256(
        json.dumps(skeleton, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
