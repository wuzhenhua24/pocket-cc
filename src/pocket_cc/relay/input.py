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

import contextlib
import logging
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from pocket_cc.app.persistence import ChatBinding, TurnState
from pocket_cc.claude.pane_inspector import detect_mode
from pocket_cc.claude.session_index import snapshot_existing_transcripts
from pocket_cc.lark.card import build_text_card
from pocket_cc.lark.client import LarkApiError
from pocket_cc.relay.card_renderer import TurnAccumulator, render_card
from pocket_cc.relay.card_stream import CardStream
from pocket_cc.relay.waiting import KeysResponse, TextResponse
from pocket_cc.tmux import TmuxError

if TYPE_CHECKING:
    from collections.abc import Callable

    from pocket_cc.app.config import Config
    from pocket_cc.app.persistence import Registry
    from pocket_cc.lark.card import CardState
    from pocket_cc.lark.client import LarkClient
    from pocket_cc.lark.event_loop import CardAction, IncomingMessage
    from pocket_cc.relay.turn_controller import TurnController
    from pocket_cc.tmux import TmuxManager

logger = logging.getLogger(__name__)

_PANE_TAIL_CHARS = 2000

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


class InputRouter:
    """Translates Lark events into tmux side-effects + new-turn provisioning."""

    def __init__(
        self,
        *,
        config: Config,
        tmux: TmuxManager,
        lark: LarkClient,
        registry: Registry,
        controller_for: Callable[[ChatBinding], TurnController] | None = None,
        seal_active: Callable[[ChatBinding, CardState, str], None] | None = None,
    ) -> None:
        self._config = config
        self._tmux = tmux
        self._lark = lark
        self._registry = registry
        # Look up the binding's TurnController (bootstrap-owned, get-or-create).
        # The router never constructs one — it only reaches a controller through
        # this provider to drive rotation-aware state transitions (clear-waiting
        # rerender, mode update). None in tests that don't exercise those paths.
        self._controller_for = controller_for
        # Seal the active turn via the bootstrap's rotation-aware path so a
        # long final turn rolls across "(续)" cards instead of tail-truncating
        # on the current card. Injected because the rotation machinery lives in
        # the controller. None in tests → fall back to the simple single-card
        # close in `_close_turn`.
        self._seal_active = seal_active
        self._seen_event_ids: OrderedDict[str, None] = OrderedDict()

    # =============================================================== messages

    def handle_message(self, msg: IncomingMessage) -> None:
        if self._is_duplicate(f"msg:{msg.message_id}"):
            logger.info(
                "dropped duplicate message",
                extra={"message_id": msg.message_id, "chat_id": msg.chat_id},
            )
            return
        if not self._is_allowed(msg.sender_open_id):
            try:
                self._lark.send_text(
                    msg.chat_id,
                    "🚫 你不在 pocket-cc 白名单 — 请联系管理员添加你的 open_id。",
                )
            except LarkApiError:
                logger.warning("failed to send denial", exc_info=True)
            return

        if msg.message_type != "text" or not msg.text:
            # Non-text (image/file/post) is out of scope for M1.
            return

        binding = self._registry.get(msg.chat_id)
        if binding is None:
            binding = self._create_binding(msg)
            if binding is None:
                return

        # **Continuation path** (M2-0): when the current turn is waiting on a
        # user response (Permission / AskUserQuestion prompt detected by
        # M2-A or M2-C), text from Lark is a *reply to that prompt*, not a
        # new request. Don't open a new turn — keep the current card and
        # send the text through. The waiting_for is cleared optimistically;
        # the detector will re-set it if Claude actually re-prompts.
        if binding.current_turn is not None and binding.current_turn.waiting_for is not None:
            self._continue_waiting_turn(binding, msg.text)
            return

        # Closing the previous turn is purely a UX seal — the *card* is
        # marked done, but Claude in the tmux pane is still running if it
        # hasn't finished. M1-E wires the Stop hook to drive this transition
        # from the real session-end signal instead.
        if binding.current_turn is not None:
            self._close_turn(binding, state="done")

        self._open_turn(binding, msg.text)

    # ============================================================ card actions

    def handle_card_action(self, action: CardAction) -> None:
        if self._is_duplicate(f"card:{action.token}"):
            logger.info(
                "dropped duplicate card action",
                extra={"token": action.token, "message_id": action.message_id},
            )
            return
        binding = self._registry.find_by_card_message_id(action.message_id)
        if binding is None:
            logger.info("card_action on unknown card", extra={"message_id": action.message_id})
            return

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
                self._handle_waiting_response(binding, action.value)
            else:
                logger.info("unknown card action", extra={"value": action.value})
        except TmuxError:
            logger.warning(
                "tmux failure handling card action",
                extra={"chat_id": binding.chat_id, "cmd": cmd},
                exc_info=True,
            )

    # ================================================================ helpers

    def _is_allowed(self, sender_open_id: str) -> bool:
        if self._config.is_whitelist_open:
            return True
        return sender_open_id in self._config.user_whitelist

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
        cwd = self._config.workspace_root
        # Snapshot *before* spawning Claude — so the snapshot reflects the
        # pre-bot state of the projects dir. Any jsonl that appears after
        # this point is necessarily our Claude's.
        existing = snapshot_existing_transcripts(cwd, self._config.claude_projects_dir)
        try:
            cwd.mkdir(parents=True, exist_ok=True)
            window = self._tmux.new_window(
                name=f"chat-{msg.chat_id[-8:] or 'unknown'}",
                cwd=str(cwd),
                command=self._config.claude_command,
            )
        except (OSError, TmuxError) as e:
            logger.exception("failed to create tmux window for chat")
            with contextlib.suppress(LarkApiError):
                self._lark.send_text(msg.chat_id, f"❌ 启动 Claude 失败：{e}")
            return None

        binding = ChatBinding(
            chat_id=msg.chat_id,
            window=window,
            cwd=cwd,
            excluded_transcripts=existing,
        )
        self._registry.set(binding)
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
        accumulator = TurnAccumulator()
        accumulator.user_prompt = user_text  # show immediately, transcript will confirm
        # Carry over the session-level permission mode so the Mode button
        # on the very first card render doesn't flash "默认" before the
        # next transcript `permission-mode` record lands.
        accumulator.current_mode = binding.current_mode

        try:
            message_id = self._lark.send_card(
                binding.chat_id,
                render_card(accumulator.snapshot(state="running")),
            )
        except LarkApiError:
            logger.exception("send initial card failed")
            return

        stream = CardStream(self._lark, message_id, interval_s=self._config.patch_interval_s)
        stream.start()
        turn = TurnState(
            card_message_id=message_id,
            card_stream=stream,
            accumulator=accumulator,
        )
        binding.current_turn = turn

        try:
            # Inject the text only — Enter is sent on a separate worker
            # after a brief grace period during which `_handle_cancel` can
            # set `turn.cancel_event` to abort the submission. Without this
            # split, an early ⏹ 中断 races send_text's internal Enter delay
            # and the prompt still gets submitted to Claude.
            self._tmux.send_text(binding.window.window_id, user_text, with_enter=False)
        except TmuxError as e:
            logger.exception("tmux send_text failed")
            self._close_turn(binding, state="failed", error=str(e))
            return

        threading.Thread(
            target=self._deferred_enter,
            args=(binding, turn),
            name=f"deferred-enter-{binding.window.window_id}",
            daemon=True,
        ).start()

    def _deferred_enter(self, binding: ChatBinding, turn: TurnState) -> None:
        """Send Enter after the grace period, unless cancel fired first.

        `wait(timeout=…)` returns True iff the event was set before timeout,
        i.e. the user clicked ⏹ 中断 during the grace window. In that case
        we skip Enter entirely — Claude never sees the prompt, so the cancel
        sequence only has to clear the input box (no "Interrupted · redirect"
        to escape out of).
        """
        if turn.cancel_event.wait(timeout=_DEFERRED_ENTER_DELAY_S):
            logger.info(
                "deferred Enter skipped — cancel signaled before grace period elapsed",
                extra={"chat_id": binding.chat_id, "window_id": binding.window.window_id},
            )
            return
        if binding.current_turn is not turn:
            # Turn was closed between scheduling and firing — drop the Enter
            # to avoid submitting an empty / stale prompt against whatever
            # state Claude is in now.
            return
        try:
            self._tmux.send_key(binding.window.window_id, "Enter")
        except TmuxError:
            logger.exception(
                "deferred Enter send failed",
                extra={"chat_id": binding.chat_id},
            )

    def close_active_turn(
        self, binding: ChatBinding, *, state: CardState = "done", error: str = ""
    ) -> None:
        """Seal whichever turn is currently active on this binding (no-op if none).

        Public so the hooks/events router can call it on Stop / StopFailure
        without going through the message handler.
        """
        self._close_turn(binding, state=state, error=error)

    def _close_turn(
        self, binding: ChatBinding, *, state: CardState = "done", error: str = ""
    ) -> None:
        turn = binding.current_turn
        if turn is None:
            return
        if self._seal_active is not None:
            # Rotation-aware seal (bootstrap-owned): rolls any oversized
            # uncommitted tail across "(续)" cards before closing, so the
            # final card never tail-truncates the whole turn. Sets
            # current_turn = None itself.
            self._seal_active(binding, state, error)
            return
        # Fallback for tests without the seal injected: simple single-card
        # close. Uses the full snapshot (fine for short turns; long turns
        # only occur with the real bootstrap path injected).
        snapshot = turn.accumulator.snapshot(state=state, error=error)
        try:
            turn.card_stream.close(render_card(snapshot))
        except Exception:
            logger.warning("closing card stream failed", exc_info=True)
        binding.current_turn = None

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
        self._clear_waiting(binding, turn)
        try:
            self._tmux.send_text(binding.window.window_id, text)
        except TmuxError as e:
            logger.exception("tmux send_text failed during continuation")
            self._close_turn(binding, state="failed", error=str(e))

    def _handle_waiting_response(self, binding: ChatBinding, value: dict[str, Any]) -> None:
        """User tapped an option button on a waiting card."""
        turn = binding.current_turn
        if turn is None or turn.waiting_for is None:
            logger.info(
                "waiting_response on a turn without active waiting state",
                extra={"chat_id": binding.chat_id},
            )
            return
        try:
            index = int(value.get("index", -1))
        except (TypeError, ValueError):
            logger.info("waiting_response with non-int index", extra={"value": value})
            return
        options = turn.waiting_for.options
        if not (0 <= index < len(options)):
            logger.info(
                "waiting_response index out of range",
                extra={"index": index, "n_options": len(options)},
            )
            return
        option = options[index]
        # Clear waiting state before sending — the detector will re-set it
        # if needed. Holds the lock briefly.
        # Clear waiting + flip the card back to running *now* (before the tmux
        # send, even), so the buttons disappear immediately when the user taps.
        # Otherwise the card sits in waiting state until the next poll tick — a
        # noticeable lag in real-world testing.
        self._clear_waiting(binding, turn)
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
           hit cancel during `_open_turn`'s grace period. Setting
           `cancel_event` makes the deferred-Enter worker drop the Enter
           before it ever reaches Claude, so the prompt never gets
           submitted. The C-c + Escapes still run to clear any text the
           `send-keys -l` already injected into the input box.

        3. **Idle with leftover input** — defense-in-depth for cases where
           something slipped through (e.g. a previous turn left text on
           the pane). Double-Escape clears the input regardless of state.

        Without (2)'s cancel_event signal, an early cancel races send_text's
        500ms internal Enter delay: C-c+Escape clears the visible input,
        but the queued Enter fires afterwards and submits the prompt
        anyway, leaving Claude actually running with no way to interrupt
        from the user's perspective.
        """
        window_id = binding.window.window_id
        turn = binding.current_turn
        if turn is not None:
            # Tell the deferred-Enter worker (if any) to abort. This is
            # *the* fix for the early-cancel race — the C-c/Escape keys
            # below can't catch an Enter that fires after they ran.
            turn.cancel_event.set()
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
        if self._controller_for is not None:
            # Controller owns the binding/turn mode write + rotation-aware
            # rerender of the Mode button label.
            self._controller_for(binding).update_mode(mode)
            return
        # Fallback for tests without a controller: update the fields directly.
        binding.current_mode = mode
        turn = binding.current_turn
        if turn is not None:
            turn.accumulator.current_mode = mode

    def _clear_waiting(self, binding: ChatBinding, turn: TurnState) -> None:
        """Clear the turn's waiting state and flip the card back to running.

        Called right after a user responds to a waiting prompt (button or
        free-form text) so the card leaves waiting state without waiting for
        the next transcript-poller / pane-watcher tick. CardStream's throttle
        still applies (~1.5s max), but it beats two-stage polling.

        Goes through the binding's TurnController (rotation-aware) so a turn
        that already rotated to a "(续)" card doesn't re-dump full history onto
        the current card and tail-truncate ("已截断早期内容"). The local clear
        below is a fallback for tests without a controller injected.
        """
        if self._controller_for is not None:
            self._controller_for(binding).clear_waiting_and_rerender()
            return
        turn.waiting_for = None

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
        except LarkApiError:
            logger.warning("send pane dump failed", exc_info=True)
