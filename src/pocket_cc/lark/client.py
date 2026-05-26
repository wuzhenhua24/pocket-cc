"""LarkClient — narrow seam over the Lark OAPI Python SDK.

Only methods actually used by pocket-cc handlers are exposed (grep-verified,
no aspirational additions). New methods land here when (and only when) a
handler needs them — matching the `TelegramClient` discipline used by ccgram
(F5 in their Round-4 modularity refactor).

The Protocol exists so handlers depend on a narrow type, not on `lark_oapi`
directly. Tests pass `FakeLarkClient` for assertions without network I/O.

API surface (M1):
  - send_text(chat_id, text)        — POST /im/v1/messages msg_type=text
  - send_card(chat_id, card)        — POST /im/v1/messages msg_type=interactive
  - patch_card(message_id, card)    — PATCH /im/v1/messages/:id

All API errors raise :class:`LarkApiError` carrying the Lark `code` / `msg` /
`log_id` triple — log_id is what you give to Lark support to look up failures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from pocket_cc.lark.error_codes import LarkErrorKind, classify, is_retryable


class LarkApiError(RuntimeError):
    """A Lark REST call returned a non-success response.

    ``kind`` is the classified taxonomy bucket (rate_limited /
    target_revoked / format_error / permission_denied / unknown). Use
    :attr:`retryable` to gate retry loops — non-retryable errors (format,
    revoked target, permission) should fail fast instead of burning the
    retry budget on calls that can't succeed.
    """

    def __init__(
        self,
        code: int,
        msg: str,
        log_id: str | None = None,
        *,
        kind: LarkErrorKind | None = None,
    ) -> None:
        suffix = f" log_id={log_id}" if log_id else ""
        super().__init__(f"Lark API failed: code={code} msg={msg}{suffix}")
        self.code = code
        self.msg = msg
        self.log_id = log_id
        self.kind: LarkErrorKind = kind if kind is not None else classify(code)

    @property
    def retryable(self) -> bool:
        return is_retryable(self.kind)


class LarkClient(Protocol):
    """The IM operations pocket-cc handlers need. Implementations: OAPI + Fake."""

    def send_text(self, chat_id: str, text: str) -> str: ...

    def send_card(self, chat_id: str, card: dict[str, Any]) -> str: ...

    def patch_card(self, message_id: str, card: dict[str, Any]) -> None: ...


# ----------------------------------------------------------------- OAPI impl


class LarkOapiClient:
    """Production adapter — wraps `lark_oapi.Client` and surfaces our types.

    Synchronous on purpose — the SDK's WS event handlers fire on a worker
    thread, and our relay layer uses regular (non-async) callables. If we
    ever go async, swap to the SDK's `acreate` / `apatch` variants.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        domain: str = "https://open.feishu.cn",
        log_level: int | None = None,
    ) -> None:
        builder = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .domain(domain)
            # Identify pocket-cc in the SDK-built User-Agent — the SDK appends
            # ``source/pocket-cc`` (sanitized), which surfaces in Lark's
            # request logs. When pinging Lark support with a log_id, the
            # source tag makes our traffic instantly distinguishable from
            # other tenants of the same app.
            .source("pocket-cc")
        )
        # Lark uses an IntEnum-ish log level; let callers override if needed.
        if log_level is not None:
            builder = builder.log_level(log_level)
        else:
            builder = builder.log_level(lark.LogLevel.INFO)
        self._rest = builder.build()

    def send_text(self, chat_id: str, text: str) -> str:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            # ensure_ascii=False keeps CJK literal in the wire payload —
            # matches the rest of pocket-cc (persistence / events / hooks /
            # card_stream all set this) and roughly halves the byte cost of
            # Chinese card bodies vs the json default's \uXXXX escapes.
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        return self._create_message(chat_id, body)

    def send_card(self, chat_id: str, card: dict[str, Any]) -> str:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        return self._create_message(chat_id, body)

    def patch_card(self, message_id: str, card: dict[str, Any]) -> None:
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self._rest.im.v1.message.patch(req)
        self._check(resp)

    # ------------------------------------------------------------------ inner

    def _create_message(self, chat_id: str, body: CreateMessageRequestBody) -> str:
        req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        resp = self._rest.im.v1.message.create(req)
        self._check(resp)
        # resp.data.message_id is typed Any in SDK; we know it's str on success.
        message_id = resp.data.message_id
        return cast("str", message_id)

    @staticmethod
    def _check(resp: Any) -> None:
        if not resp.success():
            raise LarkApiError(
                code=int(resp.code) if resp.code is not None else -1,
                msg=str(resp.msg) if resp.msg is not None else "(no msg)",
                log_id=resp.get_log_id(),
            )


# ------------------------------------------------------------------ test fake


@dataclass
class _FakeSentMessage:
    kind: str  # "text" | "card"
    chat_id: str
    message_id: str
    text: str = ""
    card: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakePatch:
    message_id: str
    card: dict[str, Any]


@dataclass
class FakeLarkClient:
    """In-memory LarkClient for tests — records calls, never touches the network.

    Each ``send_*`` allocates a deterministic ``message_id`` (``om_fake000001``,
    …) so tests can assert call order without mocking time.
    """

    sent: list[_FakeSentMessage] = field(default_factory=list)
    patches: list[_FakePatch] = field(default_factory=list)
    _counter: int = 0

    def _alloc_id(self) -> str:
        self._counter += 1
        return f"om_fake{self._counter:06d}"

    # --- Protocol surface ---

    def send_text(self, chat_id: str, text: str) -> str:
        message_id = self._alloc_id()
        self.sent.append(
            _FakeSentMessage(kind="text", chat_id=chat_id, message_id=message_id, text=text)
        )
        return message_id

    def send_card(self, chat_id: str, card: dict[str, Any]) -> str:
        message_id = self._alloc_id()
        self.sent.append(
            _FakeSentMessage(kind="card", chat_id=chat_id, message_id=message_id, card=dict(card))
        )
        return message_id

    def patch_card(self, message_id: str, card: dict[str, Any]) -> None:
        self.patches.append(_FakePatch(message_id=message_id, card=dict(card)))

    # --- test helpers ---

    def last_sent(self) -> _FakeSentMessage:
        if not self.sent:
            raise AssertionError("no messages sent")
        return self.sent[-1]

    def last_patch(self) -> _FakePatch:
        if not self.patches:
            raise AssertionError("no patches recorded")
        return self.patches[-1]
