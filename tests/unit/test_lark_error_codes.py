"""Lark error-code classification + LarkApiError.kind plumbing."""

from __future__ import annotations

import pytest

from pocket_cc.lark.client import LarkApiError
from pocket_cc.lark.error_codes import LarkErrorKind, classify, is_retryable


@pytest.mark.parametrize(
    ("raw_code", "expected"),
    [
        (230001, LarkErrorKind.FORMAT_ERROR),   # invalid message content
        (230099, LarkErrorKind.FORMAT_ERROR),   # CardKit create failure
        (230021, LarkErrorKind.FORMAT_ERROR),   # length exceeded
        (230002, LarkErrorKind.TARGET_REVOKED),
        (230005, LarkErrorKind.TARGET_REVOKED),
        (11020,  LarkErrorKind.RATE_LIMITED),
        (11021,  LarkErrorKind.RATE_LIMITED),
        (99991402, LarkErrorKind.RATE_LIMITED),
        (99991663, LarkErrorKind.PERMISSION_DENIED),  # token invalid
        (99991400, LarkErrorKind.PERMISSION_DENIED),  # auth denial
        (230003, LarkErrorKind.PERMISSION_DENIED),
        (424242, LarkErrorKind.UNKNOWN),         # unmapped
    ],
)
def test_classify_buckets(raw_code: int, expected: LarkErrorKind) -> None:
    assert classify(raw_code) is expected


def test_invalid_content_is_format_not_revoked() -> None:
    """Regression guard: 230001 used to be miscategorized as a revoked
    target, which would trigger fresh-send fallback and hide schema bugs.
    """
    assert classify(230001) is LarkErrorKind.FORMAT_ERROR


def test_is_retryable_rate_limited_always_true() -> None:
    """RATE_LIMITED is retryable regardless of raw_code."""
    assert is_retryable(LarkErrorKind.RATE_LIMITED, 11020)
    assert is_retryable(LarkErrorKind.RATE_LIMITED, 0)


def test_is_retryable_unknown_only_for_5xx_ranges() -> None:
    """UNKNOWN mirrors the SDK: only HTTP 5xx or 50000–59999 are retryable."""
    assert is_retryable(LarkErrorKind.UNKNOWN, 500)
    assert is_retryable(LarkErrorKind.UNKNOWN, 599)
    assert is_retryable(LarkErrorKind.UNKNOWN, 50000)
    assert is_retryable(LarkErrorKind.UNKNOWN, 59999)
    # Unknown 4xx / business codes are NOT retryable — retrying just delays
    # the final-failure surface.
    assert not is_retryable(LarkErrorKind.UNKNOWN, 400)
    assert not is_retryable(LarkErrorKind.UNKNOWN, 424242)
    assert not is_retryable(LarkErrorKind.UNKNOWN, 60000)


def test_is_retryable_classified_non_retryable_kinds() -> None:
    assert not is_retryable(LarkErrorKind.FORMAT_ERROR, 230001)
    assert not is_retryable(LarkErrorKind.TARGET_REVOKED, 230002)
    assert not is_retryable(LarkErrorKind.PERMISSION_DENIED, 99991663)


def test_lark_api_error_auto_classifies_kind() -> None:
    err = LarkApiError(code=11020, msg="too many requests")
    assert err.kind is LarkErrorKind.RATE_LIMITED
    assert err.retryable is True


def test_lark_api_error_unknown_5xx_is_retryable() -> None:
    """5-digit 50000–59999 are Lark's internal server-error range — the SDK
    treats them as transient and retryable."""
    err = LarkApiError(code=50001, msg="boom")
    assert err.kind is LarkErrorKind.UNKNOWN
    assert err.retryable is True


def test_lark_api_error_unknown_4xx_is_not_retryable() -> None:
    """Regression guard: unknown 4xx / business codes used to be retryable
    under a coarse ``is_retryable(kind)`` check, so card_stream burned its
    retry budget waiting for an impossible recovery. Now non-retryable."""
    err = LarkApiError(code=424242, msg="unknown business error")
    assert err.kind is LarkErrorKind.UNKNOWN
    assert err.retryable is False


def test_lark_api_error_format_error_is_not_retryable() -> None:
    err = LarkApiError(code=230001, msg="invalid content")
    assert err.kind is LarkErrorKind.FORMAT_ERROR
    assert err.retryable is False


def test_lark_api_error_explicit_kind_overrides_classify() -> None:
    """Callers can override classification (e.g. when they know more from
    the response body than the bare ``code``)."""
    err = LarkApiError(
        code=0, msg="forced", kind=LarkErrorKind.PERMISSION_DENIED,
    )
    assert err.kind is LarkErrorKind.PERMISSION_DENIED
