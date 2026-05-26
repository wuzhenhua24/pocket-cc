"""Unit tests for FakeLarkClient — records calls without network I/O."""

from __future__ import annotations

import pytest

from pocket_cc.lark.client import FakeLarkClient, LarkApiError, LarkClient, LarkOapiClient


def test_fake_satisfies_protocol() -> None:
    fake = FakeLarkClient()
    # static_assert-style: assigning to LarkClient should typecheck;
    # this also makes runtime Protocol membership testable.
    client: LarkClient = fake
    assert client is fake


def test_send_text_records_call_and_returns_id() -> None:
    fake = FakeLarkClient()
    mid = fake.send_text("oc_chat1", "hello")
    assert mid.startswith("om_fake")
    assert len(fake.sent) == 1
    last = fake.last_sent()
    assert last.kind == "text"
    assert last.chat_id == "oc_chat1"
    assert last.text == "hello"
    assert last.message_id == mid


def test_send_card_records_card_dict() -> None:
    fake = FakeLarkClient()
    card = {"header": {"template": "blue"}, "elements": []}
    mid = fake.send_card("oc_chat1", card)
    last = fake.last_sent()
    assert last.kind == "card"
    assert last.card == card
    assert last.message_id == mid


def test_message_ids_are_unique_and_monotonic() -> None:
    fake = FakeLarkClient()
    ids = [fake.send_text("c", f"m{i}") for i in range(5)]
    assert len(set(ids)) == 5
    # Monotonic order — "om_fake000001" < "om_fake000002" lexicographically too.
    assert ids == sorted(ids)


def test_patch_card_records_call() -> None:
    fake = FakeLarkClient()
    mid = fake.send_card("oc_chat1", {"x": 1})
    fake.patch_card(mid, {"x": 2})
    fake.patch_card(mid, {"x": 3})
    assert len(fake.patches) == 2
    assert fake.last_patch().card == {"x": 3}
    assert fake.last_patch().message_id == mid


def test_patch_card_is_defensive_copy() -> None:
    fake = FakeLarkClient()
    card = {"x": 1}
    fake.patch_card("mid", card)
    card["x"] = 99
    assert fake.patches[0].card == {"x": 1}


def test_last_helpers_raise_when_empty() -> None:
    fake = FakeLarkClient()
    with pytest.raises(AssertionError):
        fake.last_sent()
    with pytest.raises(AssertionError):
        fake.last_patch()


def test_lark_api_error_message_includes_log_id_when_present() -> None:
    err = LarkApiError(code=200340, msg="permission denied", log_id="abc123")
    assert "200340" in str(err)
    assert "permission denied" in str(err)
    assert "abc123" in str(err)


def test_lark_api_error_message_omits_log_id_when_none() -> None:
    err = LarkApiError(code=99, msg="boom", log_id=None)
    assert "log_id" not in str(err)


def test_oapi_client_tags_user_agent_with_pocket_cc_source() -> None:
    """The SDK appends ``source/<sanitized>`` to its User-Agent — we set it
    to ``pocket-cc`` so REST traffic is instantly identifiable in Lark's
    request logs (helps Lark support correlate a log_id back to us).

    Asserted via the SDK config the builder writes through to, not by
    sniffing the wire — the underlying Transport pulls the value from
    ``_config.source`` at request time, so verifying it landed there
    guarantees every request will carry the tag.
    """
    client = LarkOapiClient(app_id="cli_x", app_secret="secret")
    assert client._rest._config.source == "pocket-cc"
