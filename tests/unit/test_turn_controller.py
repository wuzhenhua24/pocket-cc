"""Unit tests for TurnController — focused on the generation fence (step 3).

The render/rotate/seal behavior is covered indirectly via the card-renderer
and input-router suites; here we pin the turn-generation semantics that
deferred/async work relies on to avoid acting on a superseded/sealed turn.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used in test signatures
from typing import Any, cast

from pocket_cc.app.config import Config
from pocket_cc.app.persistence import ChatBinding, TurnState
from pocket_cc.lark.client import FakeLarkClient
from pocket_cc.relay.card_renderer import TurnAccumulator
from pocket_cc.relay.card_stream import CardStream
from pocket_cc.relay.turn_controller import TurnController, TurnPhase
from pocket_cc.relay.waiting import WaitingFor
from pocket_cc.tmux import WindowInfo


def _waiting(fingerprint: str) -> WaitingFor:
    return WaitingFor(
        source="permission", question="Proceed?", options=(), fingerprint=fingerprint
    )


def _config(tmp_path: Path) -> Config:
    return Config(
        app_id="cli_x",
        app_secret="secret",
        lark_domain="https://open.feishu.cn",
        workspace_root=tmp_path,
        claude_command="claude",
        tmux_session="pocket-cc-test",
        user_whitelist=frozenset(),
        patch_interval_s=10.0,  # don't actually patch during the test
        transcript_poll_s=0.5,
        events_poll_s=0.5,
        pane_poll_s=1.0,
    )


def _binding(tmp_path: Path) -> ChatBinding:
    return ChatBinding(
        chat_id="oc_chat1",
        window=WindowInfo(
            session="pocket-cc", window_id="@1", name="chat-x", cwd="/tmp", pane_id="%1"
        ),
        cwd=tmp_path,
    )


def _attach_turn(binding: ChatBinding, lark: FakeLarkClient) -> TurnState:
    """Give the binding a current turn with a (not-started) CardStream so
    seal() can close it without spinning up a background thread."""
    stream = CardStream(cast("Any", lark), "om_card", interval_s=10.0)
    turn = TurnState(
        card_message_id="om_card",
        card_stream=stream,
        accumulator=TurnAccumulator(),
    )
    binding.current_turn = turn
    return turn


def test_begin_turn_increments_and_marks_current(tmp_path: Path) -> None:
    controller = TurnController(binding=_binding(tmp_path), lark=FakeLarkClient(), config=_config(tmp_path))
    g1 = controller.begin_turn()
    g2 = controller.begin_turn()
    assert (g1, g2) == (1, 2)
    # Only the latest generation is current.
    assert controller.is_current_gen(2)
    assert not controller.is_current_gen(1)


def test_seal_retires_active_generation(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))
    gen = controller.begin_turn()
    _attach_turn(binding, lark)

    controller.seal(state="done")

    # The turn is gone and its generation is retired — a deferred worker that
    # captured `gen` will now see is_current_gen False and abort.
    assert binding.current_turn is None
    assert not controller.is_current_gen(gen)


def test_mark_submit_cancelled_blocks_deferred_enter(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    controller = TurnController(
        binding=binding, lark=FakeLarkClient(), config=_config(tmp_path)
    )
    gen = controller.begin_turn()
    assert controller.should_submit_deferred_enter(gen) is True
    controller.mark_submit_cancelled()
    assert controller.should_submit_deferred_enter(gen) is False


def test_should_submit_deferred_enter_false_for_superseded_gen(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    controller = TurnController(
        binding=binding, lark=FakeLarkClient(), config=_config(tmp_path)
    )
    gen_a = controller.begin_turn()
    controller.begin_turn()  # supersede → gen_a no longer active
    assert controller.should_submit_deferred_enter(gen_a) is False


def test_superseding_turn_invalidates_prior_generation(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))
    gen_a = controller.begin_turn()
    _attach_turn(binding, lark)
    # New turn opens (e.g. user superseded the prior one).
    gen_b = controller.begin_turn()

    assert controller.is_current_gen(gen_b)
    assert not controller.is_current_gen(gen_a)


def test_seal_on_stop_seals_the_awaiting_turn(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))
    gen = controller.begin_turn()
    _attach_turn(binding, lark)
    controller.expect_stop(gen)

    controller.seal_on_stop(state="done")

    assert binding.current_turn is None
    assert not controller.is_current_gen(gen)


def test_seal_on_stop_with_empty_fifo_is_noop(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))
    controller.begin_turn()
    _attach_turn(binding, lark)
    # No expect_stop was recorded → a stray/duplicate Stop must not seal a
    # turn that may still be running.
    controller.seal_on_stop(state="done")

    assert binding.current_turn is not None


def test_stale_stop_does_not_seal_the_superseding_turn(tmp_path: Path) -> None:
    """The core race: Stop(A) arrives after the user already opened B. It must
    consume A's FIFO entry and discard — never seal B."""
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))

    # Turn A: opened, submitted (awaiting stop).
    gen_a = controller.begin_turn()
    _attach_turn(binding, lark)
    controller.expect_stop(gen_a)

    # User supersedes with B: WS path seals A directly, then B opens + submits.
    controller.seal(state="done")  # seals A
    gen_b = controller.begin_turn()
    _attach_turn(binding, lark)
    controller.expect_stop(gen_b)

    # Stop(A) lands first → pops gen_a, which is no longer active → discard.
    controller.seal_on_stop(state="done")
    assert binding.current_turn is not None  # B untouched
    assert controller.is_current_gen(gen_b)

    # Stop(B) lands → pops gen_b == active → seals B.
    controller.seal_on_stop(state="done")
    assert binding.current_turn is None


# ===================================================== cancel_active_turn


def test_cancel_active_turn_idle_returns_false(tmp_path: Path) -> None:
    controller = TurnController(
        binding=_binding(tmp_path), lark=FakeLarkClient(), config=_config(tmp_path)
    )
    # Nothing open → no-op, must not raise (the ⏹ defense-in-depth case).
    assert controller.cancel_active_turn() is False


def test_cancel_active_turn_opened_seals_and_drops_deferred_enter(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))
    gen = controller.begin_turn()
    _attach_turn(binding, lark)
    # OPENED: deferred Enter not yet fired (no expect_stop).
    assert controller.should_submit_deferred_enter(gen) is True

    assert controller.cancel_active_turn() is True

    assert binding.current_turn is None
    assert controller.phase() is TurnPhase.IDLE
    # The deferred-Enter worker must now drop its Enter.
    assert controller.should_submit_deferred_enter(gen) is False


def test_cancel_active_turn_running_removes_pending_stop(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))
    gen = controller.begin_turn()
    _attach_turn(binding, lark)
    controller.expect_stop(gen)  # submitted → RUNNING, gen in FIFO

    assert controller.cancel_active_turn() is True

    assert binding.current_turn is None
    # A stray/late Stop after the interrupt must find an empty FIFO → no-op,
    # never seal a future turn.
    controller.seal_on_stop(state="done")  # must not raise / must be harmless
    assert binding.current_turn is None


def test_cancel_does_not_poison_next_turns_stop(tmp_path: Path) -> None:
    """The core reason cancel must drain the FIFO: an interrupt fires no Stop,
    so a left-behind FIFO entry for the cancelled turn would be mis-popped
    against the *next* turn's Stop, leaving that turn stuck running forever."""
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))

    # Turn A: submitted (awaiting a Stop that will never come — user interrupts).
    gen_a = controller.begin_turn()
    _attach_turn(binding, lark)
    controller.expect_stop(gen_a)
    controller.cancel_active_turn()  # removes gen_a from the FIFO

    # Turn B: opened + submitted.
    gen_b = controller.begin_turn()
    _attach_turn(binding, lark)
    controller.expect_stop(gen_b)

    # B completes → its Stop must pop gen_b (not a stale gen_a) and seal B.
    controller.seal_on_stop(state="done")
    assert binding.current_turn is None
    assert not controller.is_current_gen(gen_b)


# ===================================================== phase()


def test_phase_idle_without_turn(tmp_path: Path) -> None:
    controller = TurnController(
        binding=_binding(tmp_path), lark=FakeLarkClient(), config=_config(tmp_path)
    )
    assert controller.phase() is TurnPhase.IDLE


def test_phase_opened_after_open_before_submit(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))
    # Card created + generation active, but the deferred Enter hasn't fired
    # (no expect_stop yet) → still in the cancellable grace window.
    controller.begin_turn()
    _attach_turn(binding, lark)
    assert controller.phase() is TurnPhase.OPENED


def test_phase_running_after_expect_stop(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))
    gen = controller.begin_turn()
    _attach_turn(binding, lark)
    controller.expect_stop(gen)
    assert controller.phase() is TurnPhase.RUNNING


def test_phase_waiting_takes_precedence_over_running(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))
    gen = controller.begin_turn()
    _attach_turn(binding, lark)
    controller.expect_stop(gen)  # submitted → would be RUNNING…
    controller.apply_pane_state(_waiting("fp1"), None)  # …but Claude prompted
    assert controller.phase() is TurnPhase.WAITING


def test_phase_idle_after_seal(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))
    controller.begin_turn()
    _attach_turn(binding, lark)
    controller.seal(state="done")
    assert controller.phase() is TurnPhase.IDLE


def test_phase_opened_for_new_turn_with_stale_fifo_entry(tmp_path: Path) -> None:
    """A superseded turn's generation can linger in the FIFO until its late
    Stop drains it. The freshly-opened turn must read as OPENED (not RUNNING)
    because *its* generation hasn't been submitted yet — phase keys off the
    active gen's membership, not a non-empty FIFO."""
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))
    gen_a = controller.begin_turn()
    _attach_turn(binding, lark)
    controller.expect_stop(gen_a)  # A submitted
    controller.seal(state="done")  # A sealed; gen_a still in _pending_stops
    controller.begin_turn()  # B opens
    _attach_turn(binding, lark)
    assert controller.phase() is TurnPhase.OPENED


# ===================================================== apply_pane_state


def _controller_with_turn(tmp_path: Path) -> tuple[TurnController, ChatBinding]:
    binding = _binding(tmp_path)
    lark = FakeLarkClient()
    controller = TurnController(binding=binding, lark=lark, config=_config(tmp_path))
    _attach_turn(binding, lark)
    return controller, binding


def test_apply_pane_state_sets_waiting_when_prompt_appears(tmp_path: Path) -> None:
    controller, binding = _controller_with_turn(tmp_path)
    controller.apply_pane_state(_waiting("fp1"), None)
    assert binding.current_turn is not None
    assert binding.current_turn.waiting_for is not None
    assert binding.current_turn.waiting_for.fingerprint == "fp1"


def test_apply_pane_state_same_fingerprint_is_idempotent(tmp_path: Path) -> None:
    controller, binding = _controller_with_turn(tmp_path)
    controller.apply_pane_state(_waiting("fp1"), None)
    assert binding.current_turn is not None
    first = binding.current_turn.waiting_for
    controller.apply_pane_state(_waiting("fp1"), None)
    # Same fingerprint → not reassigned.
    assert binding.current_turn.waiting_for is first


def test_apply_pane_state_clears_waiting_when_prompt_gone(tmp_path: Path) -> None:
    controller, binding = _controller_with_turn(tmp_path)
    controller.apply_pane_state(_waiting("fp1"), None)
    controller.apply_pane_state(None, None)
    assert binding.current_turn is not None
    assert binding.current_turn.waiting_for is None


def test_apply_pane_state_syncs_detected_mode(tmp_path: Path) -> None:
    controller, binding = _controller_with_turn(tmp_path)
    controller.apply_pane_state(None, "acceptEdits")
    assert binding.current_mode == "acceptEdits"
    assert binding.current_turn is not None
    assert binding.current_turn.accumulator.current_mode == "acceptEdits"


def test_apply_pane_state_ignores_none_mode(tmp_path: Path) -> None:
    controller, binding = _controller_with_turn(tmp_path)
    binding.current_mode = "acceptEdits"
    controller.apply_pane_state(None, None)
    # A None banner must not force "default".
    assert binding.current_mode == "acceptEdits"


def test_apply_pane_state_noop_without_turn(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    controller = TurnController(
        binding=binding, lark=FakeLarkClient(), config=_config(tmp_path)
    )
    # No current turn → must not raise.
    controller.apply_pane_state(_waiting("fp1"), "plan")
    assert binding.current_turn is None
