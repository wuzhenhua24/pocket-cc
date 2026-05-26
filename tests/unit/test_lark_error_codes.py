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


def test_is_retryable_only_rate_limited_and_unknown() -> None:
    assert is_retryable(LarkErrorKind.RATE_LIMITED)
    assert is_retryable(LarkErrorKind.UNKNOWN)
    assert not is_retryable(LarkErrorKind.FORMAT_ERROR)
    assert not is_retryable(LarkErrorKind.TARGET_REVOKED)
    assert not is_retryable(LarkErrorKind.PERMISSION_DENIED)


def test_lark_api_error_auto_classifies_kind() -> None:
    err = LarkApiError(code=11020, msg="too many requests")
    assert err.kind is LarkErrorKind.RATE_LIMITED
    assert err.retryable is True


def test_lark_api_error_unknown_code_stays_retryable() -> None:
    err = LarkApiError(code=500001, msg="boom")
    assert err.kind is LarkErrorKind.UNKNOWN
    assert err.retryable is True


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
