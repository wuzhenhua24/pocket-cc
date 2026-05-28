"""LarkClient — narrow seam over the Lark OAPI Python SDK.

Only methods actually used by pocket-cc handlers are exposed (grep-verified,
no aspirational additions). New methods land here when (and only when) a
handler needs them — matching the `TelegramClient` discipline used by ccgram
(F5 in their Round-4 modularity refactor).

The Protocol exists so handlers depend on a narrow type, not on `lark_oapi`
directly. Tests pass `FakeLarkClient` for assertions without network I/O.

API surface:
  - send_text(chat_id, text)             — POST /im/v1/messages msg_type=text
  - create_card_entity(card)             — POST /cardkit/v1/cards      → card_id
  - send_card_id(chat_id, card_id)       — POST /im/v1/messages referencing card_id
  - update_card_entity(card_id, card, …) — PUT  /cardkit/v1/cards/:card_id
  - stream_element_content(card_id, …)   — PUT  /cardkit/v1/cards/:id/elements/:eid/content

All API errors raise :class:`LarkApiError` carrying the Lark `code` / `msg` /
`log_id` triple — log_id is what you give to Lark support to look up failures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import (
    Card as CardkitCardModel,
)
from lark_oapi.api.cardkit.v1 import (
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    UpdateCardRequest,
    UpdateCardRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
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
        return is_retryable(self.kind, self.code)


class LarkClient(Protocol):
    """The IM + cardkit operations pocket-cc handlers need.

    Implementations: :class:`LarkOapiClient` (prod) + :class:`FakeLarkClient`
    (tests). All interactive cards go through cardkit's Schema 2.0 path
    (:meth:`create_card_entity` + :meth:`send_card_id` + streaming /
    whole-card updates); text messages still use the IM
    :meth:`send_text` endpoint.
    """

    def send_text(self, chat_id: str, text: str) -> str: ...

    # ----- cardkit / streaming -----

    def create_card_entity(self, card: dict[str, Any]) -> str:
        """Register a card payload server-side; returns the cardkit ``card_id``.

        The returned id is what subsequent element-level / card-level updates
        target, and what :meth:`send_card_id` references when posting an IM
        message. The card body uses the cardkit Schema 2.0 dict shape.
        """

    def send_card_id(self, chat_id: str, card_id: str) -> str:
        """Post an IM message that *references* an existing cardkit card.

        The message ``content`` is ``{"type":"card","data":{"card_id":...}}``
        — Lark renders the card by pulling the latest server-side state, so
        any subsequent ``stream_element_content`` / ``update_card_entity``
        call is reflected in the chat without a per-update PATCH on the IM
        message. Returns the IM ``message_id`` (used for card_action routing
        and bookkeeping).
        """

    def update_card_entity(
        self,
        card_id: str,
        card: dict[str, Any],
        *,
        sequence: int,
        uuid: str | None = None,
    ) -> None:
        """Replace the entire card body server-side (header + all elements).

        Use for state transitions where many fields change at once (e.g.
        running → done flips header color + drops action row). For pure
        content append, prefer :meth:`stream_element_content` which avoids
        re-sending the whole card.

        ``sequence`` is the monotonic per-card update counter (cardkit
        rejects out-of-order updates). ``uuid`` provides idempotency for
        retries — same uuid + same sequence = same effect.
        """

    def stream_element_content(
        self,
        card_id: str,
        element_id: str,
        content: str,
        *,
        sequence: int,
        uuid: str | None = None,
    ) -> None:
        """Stream-update a single element's text content.

        The cardkit endpoint accepts whole-content replacement per call;
        flow-style "append" is achieved by always sending the cumulative
        text the element should display. ``sequence`` orders concurrent
        updates per element. ``uuid`` lets retries dedupe at the server.
        """


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

    # ----------------------------------------------------- cardkit / streaming

    def create_card_entity(self, card: dict[str, Any]) -> str:
        # The cardkit `data` field is the JSON-encoded card body as a string,
        # not a nested object — the SDK's `type/data` envelope wraps it that
        # way for both create and update. ensure_ascii=False matches the rest
        # of pocket-cc for CJK byte-cost parity.
        body = (
            CreateCardRequestBody.builder()
            .type("card_json")
            .data(json.dumps(card, ensure_ascii=False))
            .build()
        )
        req = CreateCardRequest.builder().request_body(body).build()
        resp = self._rest.cardkit.v1.card.create(req)
        self._check(resp)
        card_id = resp.data.card_id
        return cast("str", card_id)

    def send_card_id(self, chat_id: str, card_id: str) -> str:
        # IM message body for a cardkit-managed card: msg_type=interactive,
        # content references the card_id rather than inlining the card dict.
        # Lark's renderer fetches the latest server-held card state at view
        # time, so streaming updates on `card_id` show up without further IM
        # PATCH calls.
        content = {"type": "card", "data": {"card_id": card_id}}
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(content, ensure_ascii=False))
            .build()
        )
        return self._create_message(chat_id, body)

    def update_card_entity(
        self,
        card_id: str,
        card: dict[str, Any],
        *,
        sequence: int,
        uuid: str | None = None,
    ) -> None:
        card_model = (
            CardkitCardModel.builder()
            .type("card_json")
            .data(json.dumps(card, ensure_ascii=False))
            .build()
        )
        body_builder = UpdateCardRequestBody.builder().card(card_model).sequence(sequence)
        if uuid is not None:
            body_builder = body_builder.uuid(uuid)
        req = (
            UpdateCardRequest.builder()
            .card_id(card_id)
            .request_body(body_builder.build())
            .build()
        )
        resp = self._rest.cardkit.v1.card.update(req)
        self._check(resp)

    def stream_element_content(
        self,
        card_id: str,
        element_id: str,
        content: str,
        *,
        sequence: int,
        uuid: str | None = None,
    ) -> None:
        body_builder = (
            ContentCardElementRequestBody.builder().content(content).sequence(sequence)
        )
        if uuid is not None:
            body_builder = body_builder.uuid(uuid)
        req = (
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id(element_id)
            .request_body(body_builder.build())
            .build()
        )
        resp = self._rest.cardkit.v1.card_element.content(req)
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
    kind: str  # "text" | "card_id"
    chat_id: str
    message_id: str
    text: str = ""
    card_id: str = ""  # set when kind == "card_id"


@dataclass
class _FakeCardEntity:
    """A cardkit card registered server-side. ``card_id`` is the fake id we
    handed back from :meth:`FakeLarkClient.create_card_entity`."""

    card_id: str
    card: dict[str, Any]


@dataclass
class _FakeCardEntityUpdate:
    card_id: str
    card: dict[str, Any]
    sequence: int
    uuid: str | None


@dataclass
class _FakeElementContentUpdate:
    card_id: str
    element_id: str
    content: str
    sequence: int
    uuid: str | None


@dataclass
class FakeLarkClient:
    """In-memory LarkClient for tests — records calls, never touches the network.

    Each ``send_*`` allocates a deterministic ``message_id`` (``om_fake000001``,
    …) so tests can assert call order without mocking time. The cardkit
    surface mirrors that scheme with ``card_id`` (``card_fake000001``, …).
    """

    sent: list[_FakeSentMessage] = field(default_factory=list)
    card_entities: list[_FakeCardEntity] = field(default_factory=list)
    card_entity_updates: list[_FakeCardEntityUpdate] = field(default_factory=list)
    element_content_updates: list[_FakeElementContentUpdate] = field(default_factory=list)
    _counter: int = 0
    _card_counter: int = 0

    def _alloc_id(self) -> str:
        self._counter += 1
        return f"om_fake{self._counter:06d}"

    def _alloc_card_id(self) -> str:
        self._card_counter += 1
        return f"card_fake{self._card_counter:06d}"

    # --- Protocol surface ---

    def send_text(self, chat_id: str, text: str) -> str:
        message_id = self._alloc_id()
        self.sent.append(
            _FakeSentMessage(kind="text", chat_id=chat_id, message_id=message_id, text=text)
        )
        return message_id

    def create_card_entity(self, card: dict[str, Any]) -> str:
        card_id = self._alloc_card_id()
        self.card_entities.append(_FakeCardEntity(card_id=card_id, card=dict(card)))
        return card_id

    def send_card_id(self, chat_id: str, card_id: str) -> str:
        message_id = self._alloc_id()
        self.sent.append(
            _FakeSentMessage(
                kind="card_id", chat_id=chat_id, message_id=message_id, card_id=card_id
            )
        )
        return message_id

    def update_card_entity(
        self,
        card_id: str,
        card: dict[str, Any],
        *,
        sequence: int,
        uuid: str | None = None,
    ) -> None:
        self.card_entity_updates.append(
            _FakeCardEntityUpdate(
                card_id=card_id, card=dict(card), sequence=sequence, uuid=uuid
            )
        )

    def stream_element_content(
        self,
        card_id: str,
        element_id: str,
        content: str,
        *,
        sequence: int,
        uuid: str | None = None,
    ) -> None:
        self.element_content_updates.append(
            _FakeElementContentUpdate(
                card_id=card_id,
                element_id=element_id,
                content=content,
                sequence=sequence,
                uuid=uuid,
            )
        )

    # --- test helpers ---

    def last_sent(self) -> _FakeSentMessage:
        if not self.sent:
            raise AssertionError("no messages sent")
        return self.sent[-1]

    def last_card_entity(self) -> _FakeCardEntity:
        if not self.card_entities:
            raise AssertionError("no card entities created")
        return self.card_entities[-1]

    def last_card_entity_update(self) -> _FakeCardEntityUpdate:
        if not self.card_entity_updates:
            raise AssertionError("no card entity updates recorded")
        return self.card_entity_updates[-1]

    def last_element_content_update(self) -> _FakeElementContentUpdate:
        if not self.element_content_updates:
            raise AssertionError("no element content updates recorded")
        return self.element_content_updates[-1]
