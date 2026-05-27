"""LarkEventLoop — WS subscription wrapper.

Wraps `lark.ws.Client` so the rest of pocket-cc never imports Lark SDK event
types directly. Instead handlers register two callables, and we translate
incoming SDK events into plain dataclasses (:class:`IncomingMessage` /
:class:`CardAction`) that are stable across SDK upgrades.

Translation lives in :func:`_to_incoming_message` and :func:`_to_card_action`
— extract those if we ever need to unit-test the field plumbing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

if TYPE_CHECKING:
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
    )


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """A user-sent message normalized away from the Lark SDK shape."""

    message_id: str
    chat_id: str
    chat_type: str  # "p2p" / "group"
    sender_open_id: str
    message_type: str  # "text" / "post" / "image" / ...
    text: str  # extracted plain text if message_type == "text", else ""
    raw_content: str  # original JSON `content` string from the API
    # Server-recorded send time in milliseconds since epoch. Used by the
    # relay to drop replayed events that are too old to still matter
    # (WS reconnect after a multi-hour outage, etc). ``None`` means the
    # field was absent from the event — caller should not treat the
    # message as stale in that case (back-compat / fake messages).
    create_time_ms: int | None = None


@dataclass(frozen=True, slots=True)
class CardAction:
    """A user clicked a button on one of our cards."""

    message_id: str  # the card's message_id (from event.context.open_message_id)
    chat_id: str  # event.context.open_chat_id
    sender_open_id: str
    token: str  # short-lived token for replying with a toast / new card
    tag: str  # button tag (typically "button")
    value: dict[str, Any]  # whatever we put in CardButton.value


MessageHandler = Callable[[IncomingMessage], None]
# Card action handlers may *optionally* return a card dict — the SDK ships it
# back in the same WS frame as the user's button-click event so Lark renders
# it without a follow-up REST PATCH. ``None`` means "no card update" (the
# common case for buttons that only fire tmux keys). The dict, when present,
# is the same shape ``lark.card.build_status_card`` produces.
CardActionHandler = Callable[[CardAction], dict[str, Any] | None]
# WS reconnect-lifecycle observers. The SDK fires ``on_reconnecting`` when it
# decides the connection was lost and begins retrying, then ``on_reconnected``
# on the first successful re-establishment. Both are best-effort and run on
# the asyncio loop thread the SDK owns — callers must keep them fast and
# non-blocking (logging / tiny state writes only).
ReconnectHandler = Callable[[], None]


class LarkEventLoop:
    """Owns the SDK's WS client and dispatches events to user-registered handlers.

    Construction is cheap; `start()` blocks until the WS connection dies
    (the SDK handles reconnection internally with `auto_reconnect=True`).
    Register handlers *before* calling start().
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        domain: str = "https://open.feishu.cn",
        log_level: int | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain
        self._log_level = log_level if log_level is not None else lark.LogLevel.INFO
        self._on_message: MessageHandler | None = None
        self._on_card_action: CardActionHandler | None = None
        self._on_reconnecting: ReconnectHandler | None = None
        self._on_reconnected: ReconnectHandler | None = None
        self._ws: Any = None

    def on_message(self, handler: MessageHandler) -> None:
        self._on_message = handler

    def on_card_action(self, handler: CardActionHandler) -> None:
        self._on_card_action = handler

    def on_reconnecting(self, handler: ReconnectHandler) -> None:
        """Register a callback fired when the SDK starts reconnecting.

        Fires once per disconnect (not once per retry attempt). Runs on the
        SDK's asyncio loop thread — keep the handler fast and non-blocking.
        Replaces any previously-registered handler.
        """
        self._on_reconnecting = handler

    def on_reconnected(self, handler: ReconnectHandler) -> None:
        """Register a callback fired when the SDK re-establishes the WS.

        Pairs with :meth:`on_reconnecting`. Same threading constraints apply.
        Replaces any previously-registered handler.
        """
        self._on_reconnected = handler

    def start(self) -> None:
        """Open the WS connection. Blocks indefinitely (auto-reconnects on drop)."""
        dispatcher = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._dispatch_message)
            .register_p2_card_action_trigger(self._dispatch_card_action)
            # No-op subscribers to silence "processor not found" ERROR spam for
            # events Lark auto-pushes to bots but we don't act on yet.
            .register_p2_im_message_message_read_v1(lambda _e: None)
            .build()
        )
        self._ws = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=dispatcher,
            log_level=self._log_level,
            # Without this the SDK defaults to open.feishu.cn and the WS
            # silently connects to the wrong tenant when LARK_DOMAIN points
            # at Lark international / a self-hosted domain — REST goes to
            # the configured domain, WS does not, and the handshake fails
            # in a way that's hard to attribute.
            domain=self._domain,
            # See LarkOapiClient — same tag, applied to the WS endpoint
            # handshake's User-Agent so REST and WS traffic carry a matching
            # identifier in Lark's logs.
            source="pocket-cc",
        )
        # Wire reconnect-lifecycle observers if the caller registered any. The
        # SDK exposes these as plain attributes (default to no-op lambdas); we
        # overwrite only when we actually have a handler so unrelated tests /
        # callers that don't care keep the SDK default.
        if self._on_reconnecting is not None:
            self._ws.on_reconnecting = self._on_reconnecting
        if self._on_reconnected is not None:
            self._ws.on_reconnected = self._on_reconnected
        # NOTE: SDK pins `proxy=None` so WS bypasses HTTPS_PROXY/ALL_PROXY by design.
        # Networks that require a proxy to reach Lark need to undo this — see DESIGN.md §10.
        self._ws.start()

    # ----------------------------------------------------------- dispatch glue

    def _dispatch_message(self, data: P2ImMessageReceiveV1) -> None:
        handler = self._on_message
        if handler is None:
            return
        msg = _to_incoming_message(data)
        if msg is not None:
            handler(msg)

    def _dispatch_card_action(
        self, data: P2CardActionTrigger
    ) -> P2CardActionTriggerResponse | None:
        """Run the registered handler and, if it returned a card dict, wrap it
        in the SDK's response envelope so Lark renders the patched card from
        the same WS frame — no extra REST PATCH.

        Returning ``None`` (the default for buttons that only side-effect tmux)
        leaves the SDK's outer dispatcher to ship the standard "no update"
        response, matching the historical behaviour.
        """
        handler = self._on_card_action
        if handler is None:
            return None
        action = _to_card_action(data)
        if action is None:
            return None
        card = handler(action)
        if card is None:
            return None
        # "raw" tells Lark `data` is the full card schema (the same dict shape
        # our card.py builders produce), not a template id + variables pair.
        # Pass the whole tree as nested dict literals — the SDK's ``init``
        # walks ``_types`` and rebuilds the typed model from each sub-dict;
        # passing pre-built ``CallBackCard`` instances trips its parser
        # (which expects dicts at every nested level).
        return P2CardActionTriggerResponse({"card": {"type": "raw", "data": card}})


# ------------------------------------------------------------ field extractors
# Top-level so they're unit-testable with fake event objects.


def _to_incoming_message(data: Any) -> IncomingMessage | None:
    event = getattr(data, "event", None)
    if event is None:
        return None
    msg = getattr(event, "message", None)
    if msg is None:
        return None

    message_type = msg.message_type or ""
    raw_content = msg.content or ""
    text = ""
    if message_type == "text":
        text = _extract_text(raw_content)

    sender = getattr(event, "sender", None)
    sender_open_id = ""
    if sender is not None:
        sender_id = getattr(sender, "sender_id", None)
        if sender_id is not None:
            sender_open_id = sender_id.open_id or ""

    # ``create_time`` is documented as ms since epoch but the wire format
    # is a numeric string in many Lark response shapes — the SDK's own
    # safety helpers normalize that. Be defensive: accept int / str /
    # missing, and reject anything that doesn't look like a positive int.
    create_time_ms = _coerce_create_time_ms(getattr(msg, "create_time", None))

    return IncomingMessage(
        message_id=msg.message_id or "",
        chat_id=msg.chat_id or "",
        chat_type=msg.chat_type or "",
        sender_open_id=sender_open_id,
        message_type=message_type,
        text=text,
        raw_content=raw_content,
        create_time_ms=create_time_ms,
    )


def _coerce_create_time_ms(raw: Any) -> int | None:
    """Parse Lark's ``create_time`` (int or numeric string ms) → int ms.

    Returns ``None`` for missing / unparseable / non-positive values so
    the relay can fall through to "treat as fresh" rather than guess.
    Defensive against the SDK's wire-vs-typed mismatch (declared ``int``
    but sometimes arrives as ``str``).
    """
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _to_card_action(data: Any) -> CardAction | None:
    event = getattr(data, "event", None)
    if event is None:
        return None

    context = getattr(event, "context", None)
    operator = getattr(event, "operator", None)
    action = getattr(event, "action", None)

    raw_value = getattr(action, "value", None) if action else None
    value: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}

    return CardAction(
        message_id=(getattr(context, "open_message_id", None) or "") if context else "",
        chat_id=(getattr(context, "open_chat_id", None) or "") if context else "",
        sender_open_id=(getattr(operator, "open_id", None) or "") if operator else "",
        token=getattr(event, "token", "") or "",
        tag=(getattr(action, "tag", None) or "") if action else "",
        value=value,
    )


def _extract_text(raw_content: str) -> str:
    """Lark wraps text messages as JSON: `{"text": "hello"}`. Strip the wrapper."""
    if not raw_content:
        return ""
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        return ""
    if isinstance(parsed, dict):
        text = parsed.get("text")
        if isinstance(text, str):
            return text
    return ""
