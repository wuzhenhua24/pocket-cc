"""Lark IM error-code classification.

A thin port of the code-bucket sets from `lark_oapi.channel.errors`
(SDK's channel layer). Buckets and edge-case notes are taken verbatim
from the SDK so we inherit its incident knowledge — e.g. 230001 is
"invalid message content" (format), NOT a revoked target; misclassifying
it triggers the wrong fallback and hides schema bugs.

We copy rather than import so pocket-cc doesn't depend on the SDK's
high-level channel module (newer, less stable than the core client).
"""

from __future__ import annotations

from enum import Enum


class LarkErrorKind(str, Enum):
    """Coarse taxonomy of Lark IM API failures.

    Callers branch on these to decide retry / surface / fallback. The
    string values match the SDK's ``FeishuChannelErrorCode`` enum so
    logs read the same across both layers.
    """

    RATE_LIMITED = "rate_limited"
    TARGET_REVOKED = "target_revoked"
    FORMAT_ERROR = "format_error"
    PERMISSION_DENIED = "permission_denied"
    UNKNOWN = "unknown"


# Token / auth refresh — SDK marks retryable because a stale token can be
# refreshed and the call retried. We keep them in PERMISSION_DENIED here
# because pocket-cc doesn't drive the refresh flow itself; surfacing as
# a permission error is more honest for the call sites we have.
_TOKEN_INVALID_CODES = {99991663, 99991664, 99991665, 99991666, 99991668}

# Genuine "too many requests" backpressure codes. Do NOT add 99991400 /
# 99991401 here — those are auth denials, not rate limits.
_RATE_LIMIT_CODES = {99991402, 11020, 11021}

# Reply / message target is gone (deleted, chat exited, etc). 230001 is
# NOT in this set — it's "invalid message content" (format).
_TARGET_REVOKED_CODES = {230002, 230005, 230020, 230017}

# Content too long. Bucketed as FORMAT_ERROR — caller must shrink, retry
# won't help.
_LENGTH_EXCEED_CODES = {230021, 230022}

_PERMISSION_CODES = {
    99991400, 99991401,  # auth / token denials (not rate-limit)
    99991672, 99991679, 99991680, 99991681,
    230003, 230010,
}

_FORMAT_CODES = {
    230001,  # invalid message content — shape mismatch, malformed JSON
    230099,  # CardKit "failed to create card content"
}

# Cardkit / streaming endpoints (Phase 1 stub).
#
# The cardkit family (POST /cardkit/v1/cards, PUT
# /cardkit/v1/cards/:id, PUT /cardkit/v1/cards/:id/elements/:eid/content)
# surfaces its own business error codes — notably sequence-conflict and
# card-not-found / element-not-found. We don't have an authoritative list
# here yet; the safe default is to let unknown codes fall through to
# :data:`LarkErrorKind.UNKNOWN`, where ``is_retryable`` only retries 5xx /
# 50000–59999 (transient server-side) and surfaces business 4xx as
# non-retryable. As Phase 2/3 exercises the cardkit endpoints, real codes
# we observe get added below with their semantics. Search anchor:
# CARDKIT_ERROR_CODES_TODO.


def classify(raw_code: int) -> LarkErrorKind:
    """Map a Lark API ``code`` to a :class:`LarkErrorKind`.

    ``code=0`` is success and should not reach this function; we still
    return UNKNOWN defensively rather than asserting, so a misuse
    doesn't crash the relay.
    """
    if raw_code in _TOKEN_INVALID_CODES:
        return LarkErrorKind.PERMISSION_DENIED
    if raw_code in _RATE_LIMIT_CODES:
        return LarkErrorKind.RATE_LIMITED
    if raw_code in _TARGET_REVOKED_CODES:
        return LarkErrorKind.TARGET_REVOKED
    if raw_code in _LENGTH_EXCEED_CODES:
        return LarkErrorKind.FORMAT_ERROR
    if raw_code in _PERMISSION_CODES:
        return LarkErrorKind.PERMISSION_DENIED
    if raw_code in _FORMAT_CODES:
        return LarkErrorKind.FORMAT_ERROR
    return LarkErrorKind.UNKNOWN


def is_retryable(kind: LarkErrorKind, raw_code: int) -> bool:
    """True when the same call may succeed on a later attempt.

    Mirrors the SDK's per-code retry semantics (see
    ``lark_oapi.channel.errors.classify_error``): an UNKNOWN classification
    is only retryable when the raw code looks like a transient server-side
    failure (HTTP 5xx, or Lark's 50000–59999 internal range). Unknown 4xx
    / business codes are surfaced as non-retryable so card_stream's final
    flush short-circuits instead of burning the retry budget on calls that
    will keep failing.
    """
    if kind is LarkErrorKind.RATE_LIMITED:
        return True
    if kind is LarkErrorKind.UNKNOWN:
        return 500 <= raw_code < 600 or 50000 <= raw_code < 60000
    return False
