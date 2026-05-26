"""Inbound routing — Lark messages and card-action callbacks → side effects.

Two entry points:
  - :meth:`InputRouter.handle_message`     — user sent a message
  - :meth:`InputRouter.handle_card_action` — user tapped a button on a card

Both are registered with :class:`pocket_cc.lark.LarkEventLoop` by the
bootstrap layer. This module *creates* turns (new card + new accumulator +
new tmux window if needed) but does not *update* them — updates from the
Claude transcript are pushed by `relay.output` via the bootstrap glue.

The "all text is passthrough" contract (DESIGN.md §5.1): we never intercept
slash commands; `/clear`, `/compact`, `/agents`, etc., are sent straight to
Claude via `tmux.send_text`. pocket-cc has no commands of its own.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from pocket_cc.app.persistence import ChatBinding
from pocket_cc.claude.pane_inspector import detect_mode
from pocket_cc.claude.session_index import snapshot_existing_transcripts
from pocket_cc.lark.card import build_text_card
from pocket_cc.lark.client import LarkApiError
from pocket_cc.lark.error_codes import LarkErrorKind
from pocket_cc.relay.turn_controller import TurnPhase
from pocket_cc.relay.waiting import KeysResponse, TextResponse
from pocket_cc.tmux import TmuxError

if TYPE_CHECKING:
    from collections.abc import Callable

    from pocket_cc.app.config import Config
    from pocket_cc.app.persistence import Registry, StateStore
    from pocket_cc.lark.card import CardState
    from pocket_cc.lark.client import LarkClient
    from pocket_cc.lark.event_loop import CardAction, IncomingMessage
    from pocket_cc.relay.turn_controller import TurnController
    from pocket_cc.tmux import TmuxManager

logger = logging.getLogger(__name__)

_PANE_TAIL_CHARS = 2000

# Map error taxonomy → logger level for best-effort notice sites. None of
# these sites retry (they're short status messages — retrying delays the
# real recovery path), but the failure mode tells an operator very
# different things:
#   - permission_denied / format_error: real bug — bot scope or a literal
#     string we built is broken. Surface as ``error`` so it shows up in
#     alerting.
#   - target_revoked: chat exited / message deleted — totally normal in a
#     long-running bot; demote to ``info`` so it doesn't pollute warnings.
#   - rate_limited / unknown: transient — ``warning`` (the prior default).
_LARK_SEND_FAILURE_LEVELS: dict[LarkErrorKind, int] = {
    LarkErrorKind.PERMISSION_DENIED: logging.ERROR,
    LarkErrorKind.FORMAT_ERROR: logging.ERROR,
    LarkErrorKind.TARGET_REVOKED: logging.INFO,
    LarkErrorKind.RATE_LIMITED: logging.WARNING,
    LarkErrorKind.UNKNOWN: logging.WARNING,
}


def _is_stale_message(msg: IncomingMessage) -> bool:
    """True when ``msg.create_time_ms`` is older than the stale window.

    Returns False when the event carries no ``create_time_ms`` — we'd
    rather process a message we can't time-check than wrongly drop one.
    Uses ``time.time()`` (wall clock) since we're comparing against a
    server-stamped wall-clock time, not a process-local interval.
    """
    if not msg.create_time_ms:
        return False
    age_ms = int(time.time() * 1000) - msg.create_time_ms
    return age_ms > _STALE_MESSAGE_WINDOW_MS


def _log_lark_send_failure(err: LarkApiError, where: str, **extras: object) -> None:
    """Log a Lark send failure at a level chosen by ``err.kind``.

    ``where`` is a short tag identifying the call site (used as the log
    message), ``extras`` are passed through to ``logger.log(extra=…)``
    for structured fields. ``log_id`` and ``code`` are added
    automatically so on-call doesn't have to dig.
    """
    level = _LARK_SEND_FAILURE_LEVELS.get(err.kind, logging.WARNING)
    payload: dict[str, object] = {"kind": err.kind.value, "code": err.code, **extras}
    if err.log_id:
        payload["log_id"] = err.log_id
    logger.log(level, where, extra=payload, exc_info=True)

# Lark WS pushes events at-least-once: if our handler doesn't ack in time
# (or the SDK doesn't surface success cleanly), the same event is re-delivered
# with the same `message_id` / card `token`. We keep an LRU of recently-seen
# IDs so a single user action only triggers one tmux side effect / one card.
_DEDUPE_CAPACITY = 256

# Pause between C-c and the follow-up Escape on the cancel button so Claude
# TUI has time to enter its "Interrupted · redirect" state before we abort
# *out* of it. See _handle_cancel docstring.
_CANCEL_ESCAPE_DELAY_S = 0.2
# Pause between the two Escapes in the cancel sequence. The first Escape exits
# "Interrupted · redirect" state; the second clears any leftover input text
# (Claude TUI's Esc clears the input box). 100ms matches the dedicated
# ⎋ Esc button's `delay_ms` so we don't hit Lark's "操作太频繁" rate limit.
_CANCEL_BETWEEN_ESCAPES_S = 0.1
# How long `_open_turn` waits between injecting the user's text into the
# tmux pane and sending Enter. Mirrors `tmux.manager._ENTER_DELAY_S` (the
# pty batching workaround) — by running this wait *here* instead of inside
# tmux.send_text, we can interrupt it on cancel and drop the Enter before
# it submits the prompt. Module-level so tests can monkeypatch it to 0.
_DEFERRED_ENTER_DELAY_S = 0.5
# After the ⇧⭾ Mode button sends Shift-Tab, wait this long before reading
# the pane back — Claude's TUI redraws the mode-line near-instantly, so a
# short pause is enough to capture the new mode. Mirrors ccgram's ~250ms +
# a little headroom. Runs on the Lark WS callback thread (like _handle_cancel
# at 0.3s, acceptable). Module-level so tests can monkeypatch it to 0.
_MODE_READBACK_DELAY_S = 0.35
# When a message arrives while Claude is still busy (OPENED/RUNNING), we reject
# it with one lightweight notice instead of opening a turn that would steal the
# running task's transcript output. If the user fires several messages in quick
# succession we don't want a notice per message — only re-notify after this many
# seconds of quiet. Wall-clock-independent (uses time.monotonic).
_REJECT_NOTICE_THROTTLE_S = 5.0
# Unauthorized senders get one denial text with their open_id; if they keep
# trying we throttle the reply hard (a not-on-whitelist user has nothing to do
# but ping the admin, so re-spamming them is just noise). Per-sender, monotonic.
_UNAUTH_NOTICE_THROTTLE_S = 60.0
# Messages older than this when they reach us are silently dropped. The
# common case: WS disconnects for a while (laptop sleeps, network blips,
# the bot process restarts), Lark buffers events on its side and replays
# them on reconnect. By the time we process a 30-minute-old prompt the
# user has typically moved on — replying as if it's live confuses them
# and wastes a Claude turn. 30 min mirrors the SDK's default and is well
# under "user fully forgot" while comfortably covering any normal WS
# reconnect window.
_STALE_MESSAGE_WINDOW_MS = 30 * 60 * 1000

# Authorized user sends a non-text message (post / image / merge_forward / …).
# We can't process it but the previous behavior — silent drop — left users
# staring at a non-responsive bot, especially when Lark's mobile composer
# auto-upgraded any formatted text to ``post``. Per-chat throttle so a burst
# of N forwarded items doesn't trigger N replies.
_UNSUPPORTED_NOTICE_THROTTLE_S = 30.0

# Type-aware hint shown after the generic "only plain text" line. Tells the
# user *what to actually do*: cancel formatting for post-family types,
# wait-for-a-future-version for media. Unmapped types fall through to the
# generic message with no hint.
_UNSUPPORTED_HINTS_BY_TYPE: dict[str, str] = {
    "post": "（看起来是格式化文本，移动端长按输入框关掉格式后重发即可）",
    "interactive": "（卡片消息暂不支持，请直接贴文字）",
    "image": "（图片识别还在路上，先用文字描述一下需求吧）",
    "file": "（文件附件暂不支持）",
    "audio": "（语音暂不支持，请用文字）",
    "video": "（视频暂不支持）",
    "media": "（视频暂不支持）",
    "sticker": "（表情消息没有内容可处理）",
    "merge_forward": "（合并转发暂不支持，请把要讨论的内容贴成文字）",
}


class InputRouter:
    """Translates Lark events into tmux side-effects + new-turn provisioning."""

    def __init__(
        self,
        *,
        config: Config,
        tmux: TmuxManager,
        lark: LarkClient,
        registry: Registry,
        controller_for: Callable[[ChatBinding], TurnController],
        state_store: StateStore | None = None,
    ) -> None:
        self._config = config
        self._tmux = tmux
        self._lark = lark
        self._registry = registry
        # Look up the binding's TurnController (bootstrap-owned, get-or-create).
        # The router never constructs one and never mutates turn state directly
        # — every state transition (open / seal / clear-waiting / mode) goes
        # through a controller method so it stays serialized per binding.
        self._controller_for = controller_for
        # Optional: persists L1 binding fields when a new binding is created.
        # Default None keeps existing tests untouched; bootstrap passes a real
        # store in production.
        self._state_store = state_store
        self._seen_event_ids: OrderedDict[str, None] = OrderedDict()
        # chat_id → monotonic timestamp of the last "Claude is busy" notice,
        # so a burst of messages while busy yields at most one notice per
        # `_REJECT_NOTICE_THROTTLE_S` window. See `_reject_busy`.
        self._last_reject_notice: dict[str, float] = {}
        # sender_open_id → monotonic timestamp of the last unauthorized-denial
        # text. Re-tries from the same un-whitelisted user within
        # `_UNAUTH_NOTICE_THROTTLE_S` are silently ignored — they already got the
        # message with their open_id; spamming back doesn't help.
        self._last_unauth_notice: dict[str, float] = {}
        # chat_id → monotonic timestamp of the last "unsupported message type"
        # reply. See `_notify_unsupported_message_type`.
        self._last_unsupported_notice: dict[str, float] = {}

    # =============================================================== messages

    def handle_message(self, msg: IncomingMessage) -> None:
        if self._is_duplicate(f"msg:{msg.message_id}"):
            logger.info(
                "dropped duplicate message",
                extra={"message_id": msg.message_id, "chat_id": msg.chat_id},
            )
            return
        if _is_stale_message(msg):
            # Silent drop: a "sorry I was offline" notice on reconnect would
            # fire for every buffered event from every chat, which is exactly
            # the spam pattern we're trying to avoid. The user can just resend
            # if they still want a reply.
            logger.info(
                "dropped stale message",
                extra={
                    "message_id": msg.message_id,
                    "chat_id": msg.chat_id,
                    "create_time_ms": msg.create_time_ms,
                    "age_s": (
                        (time.time() * 1000 - msg.create_time_ms) / 1000
                        if msg.create_time_ms else None
                    ),
                },
            )
            return
        # Groups are out of scope (Phase 2 design: DM only). We silently drop
        # instead of replying — a denial text in a group would just spam every
        # member every time the bot is mentioned.
        if msg.chat_type != "p2p":
            logger.info(
                "dropped non-p2p message",
                extra={
                    "chat_id": msg.chat_id,
                    "chat_type": msg.chat_type,
                    "sender_open_id": msg.sender_open_id,
                },
            )
            return
        if not self._is_allowed(msg.sender_open_id):
            self._send_unauth_notice(msg)
            return

        if msg.message_type != "text":
            # post / image / file / merge_forward / … — we can't ingest these
            # yet, but a silent drop confuses users (especially the mobile
            # `post`-upgrade case). Notify (throttled) so the user knows it
            # arrived and what to do.
            self._notify_unsupported_message_type(msg)
            return
        if not msg.text:
            # Pure-mention / empty text-typed messages: nothing to act on, and
            # no useful advice to give. Stay silent.
            return

        binding = self._registry.get(msg.chat_id)
        if binding is None:
            binding = self._create_binding(msg)
            if binding is None:
                return

        # Route on the controller's turn phase (the single source of truth for
        # "is Claude still busy"), never by guessing from `current_turn`:
        #
        #   WAITING        — Claude is blocked on a prompt (Permission /
        #                    AskUserQuestion / Plan). The text is a *reply* to
        #                    that prompt, not a new request: keep the current
        #                    card and send it through (continuation). M2-A/C
        #                    re-set waiting_for if Claude re-prompts.
        #   OPENED/RUNNING — a turn is still in flight. We must NOT open a new
        #                    turn: the prior task is still writing the shared
        #                    transcript, and a new turn's accumulator would
        #                    ingest that output (misattribution). We also must
        #                    not seal it prematurely (an interrupt fires no
        #                    Stop, completion does — let the real signal seal
        #                    it). So we reject with a throttled notice.
        #   IDLE           — nothing running: open a fresh turn.
        phase = self._controller_for(binding).phase()
        if phase is TurnPhase.WAITING:
            self._continue_waiting_turn(binding, msg.text)
            return
        if phase in (TurnPhase.OPENED, TurnPhase.RUNNING):
            self._reject_busy(binding)
            return

        self._open_turn(binding, msg.text)

    # ============================================================ card actions

    def handle_card_action(self, action: CardAction) -> dict[str, Any] | None:
        """Route a card-button click and optionally return the patched card.

        When a handler returns a dict, the LarkEventLoop ships it back in the
        same WS frame as the trigger — Lark renders the new card immediately,
        with no follow-up REST PATCH and no 1.5s throttle wait. Returning
        ``None`` is the default ("no card update") and is what every handler
        does unless instant feedback is worth the wiring (today: only the
        waiting-response button).
        """
        if self._is_duplicate(f"card:{action.token}"):
            logger.info(
                "dropped duplicate card action",
                extra={"token": action.token, "message_id": action.message_id},
            )
            return None
        binding = self._registry.find_by_card_message_id(action.message_id)
        if binding is None:
            logger.info("card_action on unknown card", extra={"message_id": action.message_id})
            return None

        cmd = action.value.get("action", "")
        try:
            if cmd == "cancel":
                self._handle_cancel(binding)
            elif cmd == "key":
                key = str(action.value.get("key", ""))
                if key:
                    self._tmux.send_key(binding.window.window_id, key)
                    if key == "BTab":
                        # ⇧⭾ Mode just cycled the permission mode — read the
                        # pane back and refresh the card so the user sees the
                        # new mode without waiting for the next poll tick.
                        self._readback_mode_after_toggle(binding)
            elif cmd == "key_sequence":
                self._handle_key_sequence(binding, action.value)
            elif cmd == "show_pane":
                self._dump_pane(binding)
            elif cmd == "waiting_response":
                # Only path that can benefit from the WS-return optimization:
                # the user just tapped an option, the card needs to flip out
                # of the ❓ waiting state right now or the buttons sit there
                # looking unclicked. Returning the new card to the dispatcher
                # makes that round-trip a single WS frame.
                return self._handle_waiting_response(binding, action.value)
            else:
                logger.info("unknown card action", extra={"value": action.value})
        except TmuxError:
            logger.warning(
                "tmux failure handling card action",
                extra={"chat_id": binding.chat_id, "cmd": cmd},
                exc_info=True,
            )
        return None

    # ================================================================ helpers

    def _is_allowed(self, sender_open_id: str) -> bool:
        return sender_open_id in self._config.users

    def _is_duplicate(self, key: str) -> bool:
        """Return True if ``key`` was seen recently; otherwise record it."""
        if not key or key.endswith(":"):  # empty id → can't dedupe, let through
            return False
        if key in self._seen_event_ids:
            return True
        self._seen_event_ids[key] = None
        while len(self._seen_event_ids) > _DEDUPE_CAPACITY:
            self._seen_event_ids.popitem(last=False)
        return False

    def _create_binding(self, msg: IncomingMessage) -> ChatBinding | None:
        # `_is_allowed` already gated this open_id; the lookup must hit.
        user = self._config.users[msg.sender_open_id]
        cwd = user.workspace
        # Snapshot *before* spawning Claude — so the snapshot reflects the
        # pre-bot state of the projects dir. Any jsonl that appears after
        # this point is necessarily our Claude's.
        existing = snapshot_existing_transcripts(cwd, self._config.claude_projects_dir)
        try:
            window = self._tmux.new_window(
                name=f"chat-{user.display_name}",
                cwd=str(cwd),
                command=self._config.claude_command,
            )
        except (OSError, TmuxError) as e:
            logger.exception("failed to create tmux window for chat")
            try:
                self._lark.send_text(msg.chat_id, f"❌ 启动 Claude 失败：{e}")
            except LarkApiError as send_err:
                # Previously silently suppressed — but a permission_denied
                # here means the bot lost access to the chat entirely,
                # which an operator wants to see.
                _log_lark_send_failure(
                    send_err,
                    "failed to send startup failure notice",
                    chat_id=msg.chat_id,
                )
            return None

        binding = ChatBinding(
            chat_id=msg.chat_id,
            open_id=msg.sender_open_id,
            window=window,
            cwd=cwd,
            excluded_transcripts=existing,
        )
        self._registry.set(binding)
        # Persist L1 (the hard binding) so a restart can re-attach to this
        # tmux window without forcing the user to re-trigger creation.
        # Best-effort: failures are logged inside StateStore; don't abort
        # the create path on a disk hiccup.
        if self._state_store is not None:
            self._state_store.save()
        logger.info(
            "created binding",
            extra={
                "chat_id": msg.chat_id,
                "window_id": window.window_id,
                "cwd": str(cwd),
                "excluded_transcripts": len(existing),
            },
        )
        return binding

    def _open_turn(self, binding: ChatBinding, user_text: str) -> None:
        # The controller owns turn creation (initial card + CardStream +
        # current_turn + generation). It returns the new generation, or None
        # if the initial card send failed (no turn installed).
        gen = self._controller_for(binding).open_turn(user_text)
        if gen is None:
            return
        try:
            # Inject the text only — Enter is sent on a separate worker after a
            # brief grace period during which ⏹ 中断 can cancel the submission
            # (the controller marks the gen cancelled). Without this split, an
            # early cancel races send_text's internal Enter delay and the prompt
            # still gets submitted to Claude.
            self._tmux.send_text(binding.window.window_id, user_text, with_enter=False)
        except TmuxError as e:
            logger.exception("tmux send_text failed")
            self._close_turn(binding, state="failed", error=str(e))
            return

        threading.Thread(
            target=self._deferred_enter,
            args=(binding, gen),
            name=f"deferred-enter-{binding.window.window_id}",
            daemon=True,
        ).start()

    def _deferred_enter(self, binding: ChatBinding, gen: int) -> None:
        """Send Enter after the grace period, unless the submission should be
        dropped — cancelled during the grace window, or the turn was
        superseded/sealed. Both are answered by the controller's generation
        fence (``should_submit_deferred_enter``)."""
        time.sleep(_DEFERRED_ENTER_DELAY_S)
        if not self._controller_for(binding).should_submit_deferred_enter(gen):
            logger.info(
                "deferred Enter dropped (cancelled or superseded)",
                extra={"chat_id": binding.chat_id, "window_id": binding.window.window_id},
            )
            return
        try:
            self._tmux.send_key(binding.window.window_id, "Enter")
        except TmuxError:
            logger.exception(
                "deferred Enter send failed",
                extra={"chat_id": binding.chat_id},
            )
            return
        # The prompt is now in front of Claude → exactly one Stop/StopFailure
        # will eventually fire for it. Record the generation so the Stop hook
        # can attribute its seal to *this* turn (not a turn opened afterwards).
        self._controller_for(binding).expect_stop(gen)

    def _close_turn(
        self, binding: ChatBinding, *, state: CardState = "done", error: str = ""
    ) -> None:
        # Rotation-aware seal owned by the controller: rolls any oversized
        # uncommitted tail across "(续)" cards before closing, so the final
        # card never tail-truncates the whole turn. No-op if no active turn.
        self._controller_for(binding).seal(state=state, error=error)

    def _send_unauth_notice(self, msg: IncomingMessage) -> None:
        """Tell an unauthorized sender how to get whitelisted, throttled.

        We include the sender's ``open_id`` verbatim so they can just forward
        it to the admin instead of having to look it up. Within
        ``_UNAUTH_NOTICE_THROTTLE_S`` of the prior reply *for the same sender*
        we drop silently — the user already has the message, re-spamming them
        every time they retry isn't useful.
        """
        now = time.monotonic()
        last = self._last_unauth_notice.get(msg.sender_open_id, 0.0)
        if now - last < _UNAUTH_NOTICE_THROTTLE_S:
            return
        self._last_unauth_notice[msg.sender_open_id] = now
        try:
            self._lark.send_text(
                msg.chat_id,
                "🚫 你不在 pocket-cc 白名单 — 请把下面这个 open_id "
                f"发给管理员加白：\n{msg.sender_open_id}",
            )
        except LarkApiError as e:
            _log_lark_send_failure(
                e, "failed to send denial",
                chat_id=msg.chat_id, sender_open_id=msg.sender_open_id,
            )

    def _notify_unsupported_message_type(self, msg: IncomingMessage) -> None:
        """Tell an authorized user we can't handle their non-text message.

        The previous behavior was a silent drop — but the most common trigger
        is Lark's mobile composer auto-upgrading any formatted input (bold,
        list, multi-line) to ``msg_type=post``, leaving users staring at a
        bot that "stopped responding". Per-chat throttled so a burst of
        forwarded items doesn't yield N replies.

        Hint text is picked from :data:`_UNSUPPORTED_HINTS_BY_TYPE` — for
        ``post`` it actually tells the user what to *do* (turn off
        formatting); for media we just say "not yet supported".
        """
        now = time.monotonic()
        last = self._last_unsupported_notice.get(msg.chat_id, 0.0)
        if now - last < _UNSUPPORTED_NOTICE_THROTTLE_S:
            return
        self._last_unsupported_notice[msg.chat_id] = now
        kind = msg.message_type or "未知"
        hint = _UNSUPPORTED_HINTS_BY_TYPE.get(msg.message_type, "")
        body = f"🙏 我现在只看得懂纯文本，刚刚那条是 {kind} 类型{hint}"
        try:
            self._lark.send_text(msg.chat_id, body)
        except LarkApiError as e:
            _log_lark_send_failure(
                e, "failed to send unsupported-type notice",
                chat_id=msg.chat_id, message_type=msg.message_type,
            )

    def _reject_busy(self, binding: ChatBinding) -> None:
        """Tell the user Claude is still busy, at most once per throttle window.

        The incoming message is intentionally dropped (not queued): opening a
        turn now would mis-ingest the running task's transcript output, and
        sealing the running turn early would lie about completion. The user
        can wait for it to finish or tap ⏹ 中断. Queueing is a separate, later
        enhancement — this is the minimal correct seal of the semantics.
        """
        now = time.monotonic()
        last = self._last_reject_notice.get(binding.chat_id, 0.0)
        if now - last < _REJECT_NOTICE_THROTTLE_S:
            return  # within the quiet window — swallow to avoid notice spam
        self._last_reject_notice[binding.chat_id] = now
        try:
            self._lark.send_text(
                binding.chat_id,
                "⏳ Claude 还在处理上一条消息——请等它完成，或点卡片上的 ⏹ 中断后再发。",
            )
        except LarkApiError as e:
            _log_lark_send_failure(
                e, "failed to send busy notice", chat_id=binding.chat_id,
            )

    def _continue_waiting_turn(self, binding: ChatBinding, text: str) -> None:
        """User's text is a reply to Claude's waiting prompt.

        Don't open a new turn — keep the same card. Clear ``waiting_for``
        optimistically so the next render shows running state again; if
        Claude re-prompts, the detector (M2-A/C) will set it back.
        """
        turn = binding.current_turn
        if turn is None:  # defensive; caller checked
            return
        # Clear waiting + flip the card back to running *now* (rotation-aware),
        # otherwise the user sees the waiting buttons stuck on screen until the
        # next poll tick. Owned by the controller so an already-rotated turn
        # isn't re-dumped onto the current card.
        self._controller_for(binding).clear_waiting_and_rerender()
        try:
            self._tmux.send_text(binding.window.window_id, text)
        except TmuxError as e:
            logger.exception("tmux send_text failed during continuation")
            self._close_turn(binding, state="failed", error=str(e))

    def _handle_waiting_response(
        self, binding: ChatBinding, value: dict[str, Any]
    ) -> dict[str, Any] | None:
        """User tapped an option button on a waiting card.

        Returns the cleared-waiting card dict so the dispatcher can ship it
        back in the same WS frame — Lark renders the flipped-out-of-waiting
        card instantly, no extra PATCH. ``None`` on the no-op paths (no
        waiting state, malformed index) and on tmux failure (the failed seal
        below issues its own terminal-state PATCH; returning the now-stale
        "running" card via WS would briefly flicker before the seal lands).
        """
        turn = binding.current_turn
        if turn is None or turn.waiting_for is None:
            logger.info(
                "waiting_response on a turn without active waiting state",
                extra={"chat_id": binding.chat_id},
            )
            return None
        try:
            index = int(value.get("index", -1))
        except (TypeError, ValueError):
            logger.info("waiting_response with non-int index", extra={"value": value})
            return None
        options = turn.waiting_for.options
        if not (0 <= index < len(options)):
            logger.info(
                "waiting_response index out of range",
                extra={"index": index, "n_options": len(options)},
            )
            return None
        option = options[index]
        # Clear waiting + build the next card under the controller lock, but
        # *don't* push it through the throttled stream — return it so the
        # dispatcher can deliver it in the WS response frame. The transcript
        # poller's next tick will resume normal stream updates with whatever
        # new content has arrived since.
        new_card = self._controller_for(binding).clear_waiting_and_build_card()
        try:
            response = option.response
            if isinstance(response, TextResponse):
                self._tmux.send_text(binding.window.window_id, response.text)
            elif isinstance(response, KeysResponse):
                for key in response.keys:
                    self._tmux.send_key(binding.window.window_id, key)
        except TmuxError as e:
            logger.exception("tmux failure dispatching waiting_response")
            self._close_turn(binding, state="failed", error=str(e))
            return None
        return new_card

    def _handle_key_sequence(self, binding: ChatBinding, value: dict[str, Any]) -> None:
        """Send multiple tmux keys with optional delay between them.

        Used by buttons that need a multi-key combo to do their job — most
        notably the ⎋ Esc button which fires two Escape keys ~100ms apart
        (Claude TUI requires double-Esc to fully clear the input box, and
        sending both from one Lark click avoids Lark's "操作太频繁" rate
        limit on rapid card action callbacks).
        """
        raw_keys = value.get("keys")
        if not isinstance(raw_keys, list):
            logger.info("key_sequence with non-list keys", extra={"value": value})
            return
        delay_s = float(value.get("delay_ms", 0)) / 1000.0
        window_id = binding.window.window_id
        for i, key in enumerate(raw_keys):
            if not isinstance(key, str) or not key:
                continue
            if i > 0 and delay_s > 0:
                time.sleep(delay_s)
            self._tmux.send_key(window_id, key)

    def _handle_cancel(self, binding: ChatBinding) -> None:
        """The "⏹ 中断" button: stop Claude's task AND clear the input box.

        This has to cover three different states Claude TUI can be in when
        the button fires:

        1. **Running** — Claude is mid-task. C-c lands it in "Interrupted ·
           redirect" mode with the original prompt text retained in the
           input box; Escape then exits redirect and clears the input.

        2. **Just-submitted-but-deferred-Enter-not-yet-fired** — the user
           hit cancel during `_open_turn`'s grace period.
           `controller.mark_submit_cancelled()` makes the deferred-Enter
           worker drop the Enter before it ever reaches Claude, so the prompt
           never gets submitted. The C-c + Escapes still run to clear any text
           the `send-keys -l` already injected into the input box.

        3. **Idle with leftover input** — defense-in-depth for cases where
           something slipped through (e.g. a previous turn left text on
           the pane). Double-Escape clears the input regardless of state.

        Without (2)'s cancel signal, an early cancel races send_text's 500ms
        internal Enter delay: C-c+Escape clears the visible input, but the
        queued Enter fires afterwards and submits the prompt anyway, leaving
        Claude actually running with no way to interrupt from the user's
        perspective.
        """
        window_id = binding.window.window_id
        # Cancel + seal the active turn (if any) now. An interrupt fires no
        # Stop hook, so the controller seals here rather than waiting for
        # seal_on_stop; this also drops the not-yet-sent deferred Enter (the
        # early-cancel-race fix — the C-c/Escape keys below can't catch an
        # Enter that fires after they ran) and removes a submitted turn's
        # stale pending-stop FIFO entry. No-op when idle, so the C-c/Escape
        # defense-in-depth below still runs to clear any leftover input.
        self._controller_for(binding).cancel_active_turn()
        # 1. Interrupt any running Claude task / clear pending text.
        self._tmux.send_key(window_id, "C-c")
        # 2. Let Claude TUI render its "Interrupted · redirect" state.
        time.sleep(_CANCEL_ESCAPE_DELAY_S)
        # 3. Exit redirect mode — or clear input if we weren't in redirect.
        self._tmux.send_key(window_id, "Escape")
        # 4. Brief pause + second Escape. Covers the state where the input
        #    box still has text (Claude TUI's Esc clears input); matches
        #    the dedicated ⎋ Esc button's two-Escape sequence.
        time.sleep(_CANCEL_BETWEEN_ESCAPES_S)
        self._tmux.send_key(window_id, "Escape")

    def _readback_mode_after_toggle(self, binding: ChatBinding) -> None:
        """Read the permission mode back from the pane after ⇧⭾ Mode.

        The transcript only records `permission-mode` at turn boundaries,
        so the running card never gets a mode signal from there. Instead we
        scrape the TUI mode-line directly: Shift-Tab redraws it instantly,
        so a brief pause then a capture reflects the new mode — including a
        cycle back to *default*, which shows no banner (None → "default" is
        safe here precisely because the footer just redrew).
        """
        window_id = binding.window.window_id
        time.sleep(_MODE_READBACK_DELAY_S)
        try:
            pane = self._tmux.capture_pane(window_id, lines=40)
        except TmuxError:
            logger.debug(
                "mode readback capture failed",
                extra={"chat_id": binding.chat_id},
            )
            return
        mode = detect_mode(pane) or "default"
        # Controller owns the binding/turn mode write + rotation-aware rerender
        # of the Mode button label.
        self._controller_for(binding).update_mode(mode)

    def _dump_pane(self, binding: ChatBinding) -> None:
        try:
            pane = self._tmux.capture_pane(binding.window.window_id)
        except TmuxError:
            logger.warning("capture_pane failed", exc_info=True)
            return
        # Strip trailing blank lines tmux pads on capture
        body = pane.rstrip()[-_PANE_TAIL_CHARS:] if pane else "(空)"
        try:
            self._lark.send_card(binding.chat_id, build_text_card(body=f"```\n{body}\n```"))
        except LarkApiError as e:
            _log_lark_send_failure(
                e, "send pane dump failed", chat_id=binding.chat_id,
            )
