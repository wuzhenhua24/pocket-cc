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
from pocket_cc.relay.turn_controller import TurnController
from pocket_cc.tmux import WindowInfo


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
