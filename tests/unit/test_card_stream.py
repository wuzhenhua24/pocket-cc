"""Unit tests for relay/card_stream.py — cardkit fast/slow paths + close."""

from __future__ import annotations

import time
from typing import Any

from pocket_cc.lark.card import ELEMENT_ID_BODY
from pocket_cc.lark.client import FakeLarkClient, LarkApiError
from pocket_cc.relay.card_stream import CardStream, open_card_stream


def _wait_until(predicate: object, timeout_s: float = 2.0, interval_s: float = 0.02) -> bool:
    """Spin until predicate() is truthy or timeout. Returns True if predicate satisfied."""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(interval_s)
    return False


def _v2_card(body: str, *, template: str = "blue", actions: bool = True) -> dict[str, Any]:
    """Build a minimal Schema 2.0 card that satisfies card_stream's diffing.

    The exact element layout matches what build_status_card_v2 emits — body
    markdown carries ``element_id == ELEMENT_ID_BODY`` so the fast path can
    target it. ``actions=False`` drops the column_set so a state-change
    diff vs. a card with actions is detectable as a skeleton diff.
    """
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "element_id": ELEMENT_ID_BODY, "content": body},
    ]
    if actions:
        elements.append(
            {
                "tag": "column_set",
                "element_id": "actions",
                "columns": [
                    {"tag": "column", "elements": [{"tag": "button", "value": {"a": "x"}}]}
                ],
            }
        )
    return {
        "schema": "2.0",
        "config": {"streaming_mode": True, "summary": {"content": "title"}},
        "header": {"template": template, "title": {"tag": "plain_text", "content": "title"}},
        "body": {"elements": elements},
    }


# ============================================================== fast path


def test_update_before_start_is_safe_and_first_tick_takes_slow_path() -> None:
    """No initial_card primed → first update is necessarily a skeleton
    diff (everything is "new"), so it routes through update_card_entity."""
    fake = FakeLarkClient()
    stream = CardStream(fake, "card_abc", interval_s=0.05)
    stream.update(_v2_card("hello"))
    stream.start()
    assert _wait_until(lambda: len(fake.card_entity_updates) >= 1)
    update = fake.last_card_entity_update()
    assert update.card_id == "card_abc"
    assert update.sequence == 1
    assert fake.element_content_updates == []
    stream.close(flush=False)


def test_body_only_change_takes_fast_path() -> None:
    """When skeleton (header/buttons/etc.) stays put and only body content
    grows, the next tick streams via card_element.content — no full PUT."""
    fake = FakeLarkClient()
    initial = _v2_card("hi")
    stream = CardStream(fake, "card_abc", initial_card=initial, interval_s=0.05)
    stream.start()
    # Initial card already primed; same dict = no-op.
    stream.update(dict(initial))
    time.sleep(0.1)
    assert fake.card_entity_updates == []
    assert fake.element_content_updates == []
    # Now extend just the body text.
    stream.update(_v2_card("hi there"))
    assert _wait_until(lambda: len(fake.element_content_updates) >= 1)
    fast = fake.last_element_content_update()
    assert fast.card_id == "card_abc"
    assert fast.element_id == ELEMENT_ID_BODY
    assert fast.content == "hi there"
    assert fast.sequence == 1
    # No skeleton change ⇒ no slow-path call.
    assert fake.card_entity_updates == []
    stream.close(flush=False)


def test_skeleton_change_takes_slow_path() -> None:
    """A header-template flip (e.g. running → waiting) is a skeleton change
    and must go through update_card_entity, not the body stream."""
    fake = FakeLarkClient()
    initial = _v2_card("body", template="blue")
    stream = CardStream(fake, "card_abc", initial_card=initial, interval_s=0.05)
    stream.start()
    # Same body, different header template = skeleton change.
    stream.update(_v2_card("body", template="orange"))
    assert _wait_until(lambda: len(fake.card_entity_updates) >= 1)
    assert fake.element_content_updates == []
    assert fake.last_card_entity_update().card["header"]["template"] == "orange"
    stream.close(flush=False)


def test_repeated_identical_update_dedupes() -> None:
    fake = FakeLarkClient()
    initial = _v2_card("hi")
    stream = CardStream(fake, "card_abc", initial_card=initial, interval_s=0.05)
    stream.start()
    for _ in range(10):
        stream.update(dict(initial))
        time.sleep(0.06)
    assert fake.card_entity_updates == []
    assert fake.element_content_updates == []
    stream.close(flush=False)


def test_sequence_strictly_increases_across_mixed_paths() -> None:
    """Each wire call (slow or fast) increments sequence by 1 — cardkit
    rejects non-monotonic sequences, so the test guards the invariant
    across mixed slow/fast traffic."""
    fake = FakeLarkClient()
    initial = _v2_card("a")
    stream = CardStream(fake, "card_abc", initial_card=initial, interval_s=0.05)
    stream.start()
    # Fast: body grows
    stream.update(_v2_card("ab"))
    assert _wait_until(lambda: len(fake.element_content_updates) >= 1)
    # Slow: header template change
    stream.update(_v2_card("ab", template="green"))
    assert _wait_until(lambda: len(fake.card_entity_updates) >= 1)
    # Fast again
    stream.update(_v2_card("abc", template="green"))
    assert _wait_until(lambda: len(fake.element_content_updates) >= 2)
    # Collect the sequence numbers in call order
    sequences = sorted(
        [u.sequence for u in fake.element_content_updates]
        + [u.sequence for u in fake.card_entity_updates]
    )
    assert sequences == [1, 2, 3], "sequences must be strictly monotonic"
    stream.close(flush=False)


# ============================================================== close


def test_close_with_final_card_forces_one_last_slow_patch() -> None:
    """Terminal cards always go slow-path (state changes streaming_mode
    off, drops actions, etc.) — even if only the body content changed."""
    fake = FakeLarkClient()
    initial = _v2_card("running")
    stream = CardStream(fake, "card_abc", initial_card=initial, interval_s=10.0)
    stream.start()
    # close with a body-only diff would, mid-stream, take the fast path —
    # but close() forces slow-path because terminal state has skeleton
    # changes (template flip, action drop). Build that here.
    final = _v2_card("done text", template="green", actions=False)
    assert stream.close(final) is True
    assert len(fake.card_entity_updates) == 1
    assert fake.element_content_updates == []
    assert fake.last_card_entity_update().card["header"]["template"] == "green"


def test_close_terminal_always_slow_path_even_for_body_only_diff() -> None:
    """Even if final_card differs from primed only in body content,
    close() routes through update_card_entity — terminal must replace
    the whole card so streaming_mode=false / action drop / etc. apply."""
    fake = FakeLarkClient()
    initial = _v2_card("body")
    stream = CardStream(fake, "card_abc", initial_card=initial, interval_s=10.0)
    stream.start()
    # Same skeleton, just body text changed.
    final = _v2_card("body final")
    assert stream.close(final) is True
    assert len(fake.card_entity_updates) == 1
    assert fake.element_content_updates == []


def test_close_without_flush_does_not_patch() -> None:
    fake = FakeLarkClient()
    stream = CardStream(fake, "card_abc", interval_s=10.0)
    stream.start()
    stream.update(_v2_card("x"))
    stream.close(flush=False)
    assert fake.card_entity_updates == []
    assert fake.element_content_updates == []


def test_update_after_close_is_noop() -> None:
    fake = FakeLarkClient()
    stream = CardStream(fake, "card_abc", interval_s=10.0)
    stream.start()
    stream.close(flush=False)
    stream.update(_v2_card("x"))
    time.sleep(0.05)
    assert fake.card_entity_updates == []
    assert fake.element_content_updates == []


def test_close_flush_false_returns_true() -> None:
    fake = FakeLarkClient()
    stream = CardStream(fake, "card_abc", interval_s=10.0)
    stream.start()
    stream.update(_v2_card("x"))
    assert stream.close(flush=False) is True
    assert fake.card_entity_updates == []


def test_close_final_flush_returns_true_on_success() -> None:
    fake = FakeLarkClient()
    stream = CardStream(fake, "card_abc", interval_s=10.0)
    stream.start()
    assert stream.close(_v2_card("done", template="green", actions=False)) is True
    assert len(fake.card_entity_updates) == 1


# ============================================================== retries


def test_close_final_flush_retries_then_succeeds() -> None:
    class FlakyClient(FakeLarkClient):
        fails_left: int = 2

        def update_card_entity(
            self,
            card_id: str,
            card: dict[str, Any],
            *,
            sequence: int,
            uuid: str | None = None,
        ) -> None:
            if self.fails_left > 0:
                self.fails_left -= 1
                raise RuntimeError("transient blip")
            super().update_card_entity(card_id, card, sequence=sequence, uuid=uuid)

    fake = FlakyClient()
    stream = CardStream(fake, "card_abc", interval_s=10.0)
    stream.start()
    assert (
        stream.close(_v2_card("done", template="green", actions=False), retries=3, backoff_s=0.0)
        is True
    )
    assert len(fake.card_entity_updates) == 1


def test_close_final_flush_returns_false_when_all_attempts_fail() -> None:
    class BombClient(FakeLarkClient):
        def update_card_entity(
            self,
            card_id: str,
            card: dict[str, Any],
            *,
            sequence: int,
            uuid: str | None = None,
        ) -> None:
            raise RuntimeError("always down")

    fake = BombClient()
    stream = CardStream(fake, "card_abc", interval_s=10.0)
    stream.start()
    assert (
        stream.close(_v2_card("done", template="green", actions=False), retries=2, backoff_s=0.0)
        is False
    )


def test_close_final_flush_short_circuits_on_non_retryable_error() -> None:
    """Format / revoked-target / permission failures can't succeed on
    retry — _flush_final must surface failure on the first attempt
    instead of burning the retry budget."""

    class FormatErrorClient(FakeLarkClient):
        attempts: int = 0

        def update_card_entity(
            self,
            card_id: str,
            card: dict[str, Any],
            *,
            sequence: int,
            uuid: str | None = None,
        ) -> None:
            self.attempts += 1
            raise LarkApiError(code=230001, msg="invalid content")

    fake = FormatErrorClient()
    stream = CardStream(fake, "card_abc", interval_s=10.0)
    stream.start()
    assert (
        stream.close(_v2_card("done", template="green", actions=False), retries=5, backoff_s=0.0)
        is False
    )
    assert fake.attempts == 1


def test_close_final_flush_short_circuits_on_unknown_4xx_business_code() -> None:
    """Regression guard: unknown 4xx / business codes are classified
    non-retryable (matches SDK ``classify_error``). _flush_final must
    surface failure on the first attempt."""

    class UnknownBusinessClient(FakeLarkClient):
        attempts: int = 0

        def update_card_entity(
            self,
            card_id: str,
            card: dict[str, Any],
            *,
            sequence: int,
            uuid: str | None = None,
        ) -> None:
            self.attempts += 1
            raise LarkApiError(code=424242, msg="unknown business error")

    fake = UnknownBusinessClient()
    stream = CardStream(fake, "card_abc", interval_s=10.0)
    stream.start()
    assert (
        stream.close(_v2_card("done", template="green", actions=False), retries=5, backoff_s=0.0)
        is False
    )
    assert fake.attempts == 1


def test_close_final_flush_retries_unknown_5xx_error() -> None:
    """UNKNOWN with a 5xx-shaped code stays retryable (transient server
    failure) — _flush_final still consumes its retry budget."""

    class TransientServerClient(FakeLarkClient):
        attempts: int = 0

        def update_card_entity(
            self,
            card_id: str,
            card: dict[str, Any],
            *,
            sequence: int,
            uuid: str | None = None,
        ) -> None:
            self.attempts += 1
            if self.attempts < 3:
                raise LarkApiError(code=50001, msg="internal server error")
            super().update_card_entity(card_id, card, sequence=sequence, uuid=uuid)

    fake = TransientServerClient()
    stream = CardStream(fake, "card_abc", interval_s=10.0)
    stream.start()
    assert (
        stream.close(_v2_card("done", template="green", actions=False), retries=3, backoff_s=0.0)
        is True
    )
    assert fake.attempts == 3


def test_close_final_flush_retries_rate_limited_error() -> None:
    class RateLimitedClient(FakeLarkClient):
        attempts: int = 0

        def update_card_entity(
            self,
            card_id: str,
            card: dict[str, Any],
            *,
            sequence: int,
            uuid: str | None = None,
        ) -> None:
            self.attempts += 1
            if self.attempts < 3:
                raise LarkApiError(code=11020, msg="too many requests")
            super().update_card_entity(card_id, card, sequence=sequence, uuid=uuid)

    fake = RateLimitedClient()
    stream = CardStream(fake, "card_abc", interval_s=10.0)
    stream.start()
    assert (
        stream.close(_v2_card("done", template="green", actions=False), retries=3, backoff_s=0.0)
        is True
    )
    assert fake.attempts == 3


def test_update_failure_does_not_stop_the_stream() -> None:
    """A flaky tick must not advance the stored hashes — the next tick
    retries the same pending card."""

    class FlakyOnceClient(FakeLarkClient):
        first_blown: bool = False

        def update_card_entity(
            self,
            card_id: str,
            card: dict[str, Any],
            *,
            sequence: int,
            uuid: str | None = None,
        ) -> None:
            if not self.first_blown:
                self.first_blown = True
                raise RuntimeError("simulated blip")
            super().update_card_entity(card_id, card, sequence=sequence, uuid=uuid)

    fake = FlakyOnceClient()
    stream = CardStream(fake, "card_abc", interval_s=0.05)
    stream.start()
    stream.update(_v2_card("a"))
    # First tick raises. Push a *different* card so the next tick has a
    # fresh "pending" to deliver and verify the stream is still alive.
    time.sleep(0.15)
    stream.update(_v2_card("b"))
    assert _wait_until(lambda: len(fake.card_entity_updates) >= 1, timeout_s=2.0)
    stream.close(flush=False)


# ============================================================== open_card_stream factory


def test_open_card_stream_creates_card_sends_id_and_primes_hashes() -> None:
    """The factory wraps create + send + CardStream, and primes the
    stream's hashes so the first update with the same dict is a no-op."""
    fake = FakeLarkClient()
    initial = _v2_card("hello")
    card_id, message_id, stream = open_card_stream(fake, "oc_chat1", initial, interval_s=0.05)
    assert card_id.startswith("card_fake")
    assert message_id.startswith("om_fake")
    assert len(fake.card_entities) == 1
    assert fake.card_entities[0].card == initial
    assert fake.sent[-1].kind == "card_id"
    assert fake.sent[-1].card_id == card_id
    stream.start()
    # Push the same initial card → should be deduped (hashes primed).
    stream.update(dict(initial))
    time.sleep(0.1)
    assert fake.card_entity_updates == []
    assert fake.element_content_updates == []
    stream.close(flush=False)


def test_open_card_stream_returns_non_started_stream() -> None:
    """Factory leaves stream un-started so callers can decide when to
    spin up the worker thread (matches the legacy CardStream contract)."""
    fake = FakeLarkClient()
    _, _, stream = open_card_stream(fake, "c", _v2_card("x"))
    assert stream._started is False
