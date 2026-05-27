"""Unit tests for relay/card_stream.py — throttle + hash dedupe + close."""

from __future__ import annotations

import time

from pocket_cc.lark.client import FakeLarkClient, LarkApiError
from pocket_cc.relay.card_stream import CardStream


def _wait_until(predicate: object, timeout_s: float = 2.0, interval_s: float = 0.02) -> bool:
    """Spin until predicate() is truthy or timeout. Returns True if predicate satisfied."""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(interval_s)
    return False


def test_update_before_start_is_safe_and_first_tick_flushes() -> None:
    fake = FakeLarkClient()
    stream = CardStream(fake, "om_abc", interval_s=0.05)
    stream.update({"version": 1})
    stream.start()
    assert _wait_until(lambda: len(fake.patches) >= 1)
    assert fake.patches[0].card == {"version": 1}
    stream.close(flush=False)


def test_repeated_identical_update_dedupes() -> None:
    fake = FakeLarkClient()
    stream = CardStream(fake, "om_abc", interval_s=0.05)
    stream.start()
    same = {"version": 1, "x": [1, 2, 3]}
    stream.update(same)
    assert _wait_until(lambda: len(fake.patches) >= 1)
    # Push the exact same card N more times; hash dedupe should keep patch count at 1.
    for _ in range(10):
        stream.update(dict(same))
        time.sleep(0.06)
    assert len(fake.patches) == 1, "duplicate cards should not patch again"
    stream.close(flush=False)


def test_changed_update_triggers_new_patch() -> None:
    fake = FakeLarkClient()
    stream = CardStream(fake, "om_abc", interval_s=0.05)
    stream.start()
    stream.update({"v": 1})
    assert _wait_until(lambda: len(fake.patches) == 1)
    stream.update({"v": 2})
    assert _wait_until(lambda: len(fake.patches) == 2)
    assert fake.patches[-1].card == {"v": 2}
    stream.close(flush=False)


def test_close_with_final_card_forces_one_last_patch() -> None:
    fake = FakeLarkClient()
    stream = CardStream(fake, "om_abc", interval_s=10.0)  # huge interval; only close should flush
    stream.start()
    stream.close({"final": True})
    assert len(fake.patches) == 1
    assert fake.patches[0].card == {"final": True}


def test_close_without_flush_does_not_patch() -> None:
    fake = FakeLarkClient()
    stream = CardStream(fake, "om_abc", interval_s=10.0)
    stream.start()
    stream.update({"x": 1})
    stream.close(flush=False)
    assert fake.patches == []


def test_update_after_close_is_noop() -> None:
    fake = FakeLarkClient()
    stream = CardStream(fake, "om_abc", interval_s=10.0)
    stream.start()
    stream.close(flush=False)
    stream.update({"x": 1})
    time.sleep(0.05)
    assert fake.patches == []


def test_close_final_flush_returns_true_on_success() -> None:
    fake = FakeLarkClient()
    stream = CardStream(fake, "om_abc", interval_s=10.0)
    stream.start()
    assert stream.close({"final": True}) is True
    assert len(fake.patches) == 1


def test_close_flush_false_returns_true() -> None:
    fake = FakeLarkClient()
    stream = CardStream(fake, "om_abc", interval_s=10.0)
    stream.start()
    stream.update({"x": 1})
    assert stream.close(flush=False) is True
    assert fake.patches == []


def test_close_final_flush_retries_then_succeeds() -> None:
    class FlakyClient(FakeLarkClient):
        fails_left: int = 2

        def patch_card(self, message_id: str, card: dict[str, object]) -> None:
            if self.fails_left > 0:
                self.fails_left -= 1
                raise RuntimeError("transient blip")
            super().patch_card(message_id, card)

    fake = FlakyClient()
    stream = CardStream(fake, "om_abc", interval_s=10.0)
    stream.start()
    # 2 failures then success, within retries=3 → delivered.
    assert stream.close({"final": True}, retries=3, backoff_s=0.0) is True
    assert len(fake.patches) == 1


def test_close_final_flush_returns_false_when_all_attempts_fail() -> None:
    class BombClient(FakeLarkClient):
        def patch_card(self, message_id: str, card: dict[str, object]) -> None:
            raise RuntimeError("always down")

    fake = BombClient()
    stream = CardStream(fake, "om_abc", interval_s=10.0)
    stream.start()
    # retries=2 → 3 attempts, all fail → reports failure (caller sends fallback).
    assert stream.close({"final": True}, retries=2, backoff_s=0.0) is False


def test_close_final_flush_short_circuits_on_non_retryable_error() -> None:
    """Format / revoked-target / permission failures can't succeed on
    retry — _flush_final must surface failure on the first attempt
    instead of burning the retry budget."""

    class FormatErrorClient(FakeLarkClient):
        attempts: int = 0

        def patch_card(self, message_id: str, card: dict[str, object]) -> None:
            self.attempts += 1
            raise LarkApiError(code=230001, msg="invalid content")

    fake = FormatErrorClient()
    stream = CardStream(fake, "om_abc", interval_s=10.0)
    stream.start()
    # retries=5 — but FORMAT_ERROR is non-retryable, so we expect exactly
    # one attempt before returning False.
    assert stream.close({"final": True}, retries=5, backoff_s=0.0) is False
    assert fake.attempts == 1


def test_close_final_flush_short_circuits_on_unknown_4xx_business_code() -> None:
    """Regression guard: unknown 4xx / business codes are now classified
    non-retryable (matches SDK ``classify_error``). _flush_final must
    surface failure on the first attempt — retrying just delays the
    terminal-card failure return."""

    class UnknownBusinessClient(FakeLarkClient):
        attempts: int = 0

        def patch_card(self, message_id: str, card: dict[str, object]) -> None:
            self.attempts += 1
            raise LarkApiError(code=424242, msg="unknown business error")

    fake = UnknownBusinessClient()
    stream = CardStream(fake, "om_abc", interval_s=10.0)
    stream.start()
    assert stream.close({"final": True}, retries=5, backoff_s=0.0) is False
    assert fake.attempts == 1


def test_close_final_flush_retries_unknown_5xx_error() -> None:
    """UNKNOWN with a 5xx-shaped code stays retryable (transient server
    failure) — verify _flush_final still consumes its retry budget."""

    class TransientServerClient(FakeLarkClient):
        attempts: int = 0

        def patch_card(self, message_id: str, card: dict[str, object]) -> None:
            self.attempts += 1
            if self.attempts < 3:
                raise LarkApiError(code=50001, msg="internal server error")
            super().patch_card(message_id, card)

    fake = TransientServerClient()
    stream = CardStream(fake, "om_abc", interval_s=10.0)
    stream.start()
    assert stream.close({"final": True}, retries=3, backoff_s=0.0) is True
    assert fake.attempts == 3


def test_close_final_flush_retries_rate_limited_error() -> None:
    """RATE_LIMITED is retryable — verify we still consume retry budget
    on classified-but-retryable errors."""

    class RateLimitedClient(FakeLarkClient):
        attempts: int = 0

        def patch_card(self, message_id: str, card: dict[str, object]) -> None:
            self.attempts += 1
            if self.attempts < 3:
                raise LarkApiError(code=11020, msg="too many requests")
            super().patch_card(message_id, card)

    fake = RateLimitedClient()
    stream = CardStream(fake, "om_abc", interval_s=10.0)
    stream.start()
    assert stream.close({"final": True}, retries=3, backoff_s=0.0) is True
    assert fake.attempts == 3


def test_patch_failure_does_not_stop_the_stream() -> None:
    class BombClient(FakeLarkClient):
        def patch_card(self, message_id: str, card: dict[str, object]) -> None:
            super().patch_card(message_id, card)
            raise RuntimeError("simulated network blip")

    fake = BombClient()
    stream = CardStream(fake, "om_abc", interval_s=0.05)
    stream.start()
    stream.update({"x": 1})
    # First tick raises but is caught; the bad hash should not be cached, so
    # the next changed card still gets retried.
    time.sleep(0.15)
    stream.update({"x": 2})
    assert _wait_until(lambda: len(fake.patches) >= 2, timeout_s=2.0)
    stream.close(flush=False)
