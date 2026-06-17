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
        open_id="ou_user1",
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


# --------------------------------------------------- Stop lazy-lock fallback


def test_stop_lazily_locks_and_fires_when_transcript_not_yet_locked(tmp_path: Path) -> None:
    """The fix: a Stop arriving before the transcript was locked (SessionStart
    missed/ambiguous, poller hadn't ticked) must still attribute — via the
    path the Stop event itself carries — instead of being dropped."""
    binding = _binding(cwd=tmp_path)  # transcript_path is None — never locked
    registry = Registry()
    registry.set(binding)

    fresh = str(tmp_path / "ours-new.jsonl")
    locked: list[str] = []
    stopped: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(
        registry,
        on_session_start=lambda _b, e: locked.append(e.transcript_path),
        on_stop=lambda b, _e: stopped.append(b),
    )
    dispatcher.dispatch(_event("Stop", transcript_path=fresh, cwd=str(tmp_path)))

    # Lazily locked via the Stop's own transcript_path, then sealed.
    assert locked == [fresh]
    assert stopped == [binding]


def test_stop_fallback_matches_cwd_across_symlink(tmp_path: Path) -> None:
    """cwd compare must be symlink-tolerant: binding holds the configured path,
    the hook reports the realpath (the /tmp vs /private/tmp case on macOS)."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    binding = _binding(cwd=link)  # configured path (symlink), unlocked
    registry = Registry()
    registry.set(binding)

    stopped: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(
        registry,
        on_session_start=lambda _b, _e: None,
        on_stop=lambda b, _e: stopped.append(b),
    )
    # Event reports the resolved (real) cwd.
    dispatcher.dispatch(_event("Stop", transcript_path=str(real / "ours.jsonl"), cwd=str(real)))

    assert stopped == [binding]


def test_stop_fallback_skips_when_multiple_bindings_match(tmp_path: Path) -> None:
    """Two unlocked bindings in the same cwd → can't tell which the Stop is
    for; never guess (would seal the wrong turn)."""
    a = _binding(chat_id="a", cwd=tmp_path)
    b = _binding(chat_id="b", cwd=tmp_path)
    registry = Registry()
    registry.set(a)
    registry.set(b)

    stopped: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(registry, on_stop=lambda bind, _e: stopped.append(bind))
    dispatcher.dispatch(
        _event("Stop", transcript_path=str(tmp_path / "f.jsonl"), cwd=str(tmp_path))
    )

    assert stopped == []


def test_stop_fallback_ignores_excluded_transcript(tmp_path: Path) -> None:
    """A Stop for a pre-existing (excluded) transcript is a concurrent Claude's,
    even in our cwd — must not lazy-lock onto it."""
    desktop = tmp_path / "desktop.jsonl"
    binding = _binding(cwd=tmp_path, excluded=frozenset({desktop}))  # unlocked
    registry = Registry()
    registry.set(binding)

    stopped: list[ChatBinding] = []
    locked: list[str] = []
    dispatcher = HookEventsDispatcher(
        registry,
        on_session_start=lambda _b, e: locked.append(e.transcript_path),
        on_stop=lambda b, _e: stopped.append(b),
    )
    dispatcher.dispatch(_event("Stop", transcript_path=str(desktop), cwd=str(tmp_path)))

    assert locked == []
    assert stopped == []


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


def test_session_start_rebinds_already_bound_binding_after_clear(tmp_path: Path) -> None:
    """A SessionStart with a *new* path for an already-bound binding in the
    same cwd is our Claude starting a new session (/clear, /compact) — it must
    rebind, so the poller (now off mtime) keeps following the session."""
    binding = _binding(cwd=tmp_path, transcript_path=tmp_path / "already-locked.jsonl")
    registry = Registry()
    registry.set(binding)

    captured: list[tuple[ChatBinding, str]] = []
    dispatcher = HookEventsDispatcher(
        registry,
        on_session_start=lambda b, e: captured.append((b, e.transcript_path)),
    )
    new_path = str(tmp_path / "different.jsonl")
    dispatcher.dispatch(_event("SessionStart", transcript_path=new_path, cwd=str(tmp_path)))

    assert len(captured) == 1
    assert captured[0][0] is binding
    assert captured[0][1] == new_path


def test_session_start_does_not_rebind_when_multiple_bound_bindings_share_cwd(
    tmp_path: Path,
) -> None:
    """Two bound Claudes in one cwd → a new SessionStart there is ambiguous;
    refuse to rebind either rather than cross-bind to the wrong session."""
    b1 = _binding(chat_id="oc_1", cwd=tmp_path, transcript_path=tmp_path / "one.jsonl")
    b2 = _binding(chat_id="oc_2", cwd=tmp_path, transcript_path=tmp_path / "two.jsonl")
    registry = Registry()
    registry.set(b1)
    registry.set(b2)

    captured: list[ChatBinding] = []
    dispatcher = HookEventsDispatcher(registry, on_session_start=lambda b, _e: captured.append(b))
    dispatcher.dispatch(
        _event("SessionStart", transcript_path=str(tmp_path / "three.jsonl"), cwd=str(tmp_path))
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
