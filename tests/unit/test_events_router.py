"""Unit tests for relay/events_router.py — HookEventsDispatcher routing.

We don't exercise EventsPoller's thread loop here — it's a thin wrapper
around an EventsReader (tested separately) and the dispatcher (tested here).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 — used at runtime in stub WindowInfo
from typing import Any

from pocket_cc.app.persistence import ChatBinding, Registry
from pocket_cc.claude.events import HookEvent
from pocket_cc.relay.events_router import HookEventsDispatcher


@dataclass
class _StubWindow:
    session: str = "stub"
    window_id: str = "@0"
    name: str = "stub"
    cwd: str = "/tmp"
    pane_id: str = "%0"


def _binding(
    *,
    chat_id: str = "oc_x",
    cwd: Path,
    transcript_path: Path | None = None,
    excluded: frozenset[Path] = frozenset(),
) -> ChatBinding:
    b = ChatBinding(
        chat_id=chat_id,
        window=_StubWindow(),  # type: ignore[arg-type]
        cwd=cwd,
        transcript_path=transcript_path,
        excluded_transcripts=excluded,
    )
    return b


def _event(
    event: str,
    *,
    transcript_path: str = "",
    cwd: str = "",
    session_id: str = "sid",
    raw: dict[str, Any] | None = None,
) -> HookEvent:
    return HookEvent(
        event=event,
        timestamp=0.0,
        session_id=session_id,
        transcript_path=transcript_path,
        cwd=cwd,
        raw=raw or {},
    )


# ---------------------------------------------------------------- Stop / Fail


def test_stop_event_routes_to_binding_with_matching_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "ours.jsonl"
    binding = _binding(cwd=tmp_path, transcript_path=transcript)
    registry = Registry()
    registry.set(binding)

    captured: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(registry, on_stop=lambda b, _e: captured.append(b))
    dispatcher.dispatch(_event("Stop", transcript_path=str(transcript), cwd=str(tmp_path)))

    assert captured == [binding]


def test_stop_failure_routes_to_failure_callback(tmp_path: Path) -> None:
    transcript = tmp_path / "ours.jsonl"
    binding = _binding(cwd=tmp_path, transcript_path=transcript)
    registry = Registry()
    registry.set(binding)

    stops: list[ChatBinding] = []
    fails: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(
        registry,
        on_stop=lambda b, _e: stops.append(b),
        on_stop_failure=lambda b, _e: fails.append(b),
    )
    dispatcher.dispatch(_event("StopFailure", transcript_path=str(transcript), cwd=str(tmp_path)))

    assert stops == []
    assert fails == [binding]


def test_stop_event_for_unknown_transcript_is_ignored(tmp_path: Path) -> None:
    binding = _binding(cwd=tmp_path, transcript_path=tmp_path / "ours.jsonl")
    registry = Registry()
    registry.set(binding)

    captured: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(registry, on_stop=lambda b, _e: captured.append(b))
    dispatcher.dispatch(_event("Stop", transcript_path=str(tmp_path / "someone-else.jsonl")))

    assert captured == []


def test_stop_event_for_excluded_transcript_is_ignored(tmp_path: Path) -> None:
    """A binding's `transcript_path` field equaling event.transcript_path
    while *also* being in excluded_transcripts should never fire — defensive."""
    transcript = tmp_path / "weird.jsonl"
    binding = _binding(
        cwd=tmp_path,
        transcript_path=transcript,
        excluded=frozenset({transcript}),
    )
    registry = Registry()
    registry.set(binding)

    captured: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(registry, on_stop=lambda b, _e: captured.append(b))
    dispatcher.dispatch(_event("Stop", transcript_path=str(transcript)))

    assert captured == []


def test_stop_event_with_empty_transcript_path_is_ignored(tmp_path: Path) -> None:
    binding = _binding(cwd=tmp_path, transcript_path=tmp_path / "x.jsonl")
    registry = Registry()
    registry.set(binding)

    captured: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(registry, on_stop=lambda b, _e: captured.append(b))
    dispatcher.dispatch(_event("Stop", transcript_path=""))

    assert captured == []


# --------------------------------------------------------------- SessionStart


def test_session_start_locks_waiting_binding(tmp_path: Path) -> None:
    binding = _binding(cwd=tmp_path)  # transcript_path is None
    registry = Registry()
    registry.set(binding)

    captured: list[tuple[ChatBinding, str]] = []
    dispatcher = HookEventsDispatcher(
        registry,
        on_session_start=lambda b, e: captured.append((b, e.transcript_path)),
    )
    fresh = str(tmp_path / "ours-new.jsonl")
    dispatcher.dispatch(_event("SessionStart", transcript_path=fresh, cwd=str(tmp_path)))

    assert len(captured) == 1
    assert captured[0][0] is binding
    assert captured[0][1] == fresh


def test_session_start_ignores_binding_already_locked(tmp_path: Path) -> None:
    binding = _binding(cwd=tmp_path, transcript_path=tmp_path / "already-locked.jsonl")
    registry = Registry()
    registry.set(binding)

    captured: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(registry, on_session_start=lambda b, _e: captured.append(b))
    dispatcher.dispatch(
        _event(
            "SessionStart",
            transcript_path=str(tmp_path / "different.jsonl"),
            cwd=str(tmp_path),
        )
    )

    assert captured == []


def test_session_start_ignores_event_for_excluded_transcript(tmp_path: Path) -> None:
    """If the event's transcript_path is in excluded_transcripts, it's a
    concurrent (e.g. desktop) Claude — not ours."""
    desktop = tmp_path / "desktop.jsonl"
    binding = _binding(cwd=tmp_path, excluded=frozenset({desktop}))
    registry = Registry()
    registry.set(binding)

    captured: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(registry, on_session_start=lambda b, _e: captured.append(b))
    dispatcher.dispatch(_event("SessionStart", transcript_path=str(desktop), cwd=str(tmp_path)))

    assert captured == []


def test_session_start_ignores_event_with_wrong_cwd(tmp_path: Path) -> None:
    binding = _binding(cwd=tmp_path)
    registry = Registry()
    registry.set(binding)

    captured: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(registry, on_session_start=lambda b, _e: captured.append(b))
    dispatcher.dispatch(
        _event(
            "SessionStart",
            transcript_path=str(tmp_path / "fresh.jsonl"),
            cwd="/some/other/cwd",
        )
    )

    assert captured == []


def test_session_start_skips_when_multiple_bindings_match(tmp_path: Path) -> None:
    """If two bindings share a cwd and neither has a locked transcript, we
    can't tell which one the event belongs to — skip rather than guess wrong."""
    a = _binding(chat_id="a", cwd=tmp_path)
    b = _binding(chat_id="b", cwd=tmp_path)
    registry = Registry()
    registry.set(a)
    registry.set(b)

    captured: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(
        registry, on_session_start=lambda bind, _e: captured.append(bind)
    )
    dispatcher.dispatch(
        _event("SessionStart", transcript_path=str(tmp_path / "f.jsonl"), cwd=str(tmp_path))
    )

    assert captured == []


# ---------------------------------------------------------------- error path


def test_callback_exception_does_not_crash_dispatcher(tmp_path: Path) -> None:
    """A buggy callback must not poison the dispatcher."""
    transcript = tmp_path / "ours.jsonl"
    binding = _binding(cwd=tmp_path, transcript_path=transcript)
    registry = Registry()
    registry.set(binding)

    def _bad(_b: ChatBinding, _e: HookEvent) -> None:
        raise RuntimeError("simulated")

    dispatcher = HookEventsDispatcher(registry, on_stop=_bad)
    # Should not raise
    dispatcher.dispatch(_event("Stop", transcript_path=str(transcript)))


# -------------------------------------------------------- unknown event types


def test_other_events_silently_ignored(tmp_path: Path) -> None:
    binding = _binding(cwd=tmp_path, transcript_path=tmp_path / "x.jsonl")
    registry = Registry()
    registry.set(binding)

    captured: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(
        registry,
        on_session_start=lambda b, _e: captured.append(b),
        on_stop=lambda b, _e: captured.append(b),
        on_stop_failure=lambda b, _e: captured.append(b),
    )
    # Notification / SessionEnd / arbitrary names: no callbacks fire
    for name in ("Notification", "SessionEnd", "PreToolUse", "MysteryEvent"):
        dispatcher.dispatch(_event(name, transcript_path=str(tmp_path / "x.jsonl")))

    assert captured == []
