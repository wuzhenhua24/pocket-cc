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


# ----------------------------------------------------------- cardkit surface


def test_create_card_entity_allocates_card_id_and_records() -> None:
    fake = FakeLarkClient()
    card = {"schema": "2.0", "header": {"title": {"content": "hi"}}, "body": {"elements": []}}
    card_id = fake.create_card_entity(card)
    assert card_id.startswith("card_fake")
    assert len(fake.card_entities) == 1
    last = fake.last_card_entity()
    assert last.card_id == card_id
    assert last.card == card


def test_create_card_entity_is_defensive_copy() -> None:
    fake = FakeLarkClient()
    card = {"x": 1}
    fake.create_card_entity(card)
    card["x"] = 99
    assert fake.card_entities[0].card == {"x": 1}


def test_card_ids_are_unique_and_monotonic() -> None:
    fake = FakeLarkClient()
    ids = [fake.create_card_entity({"i": i}) for i in range(3)]
    assert len(set(ids)) == 3
    assert ids == sorted(ids)


def test_send_card_id_records_reference_and_returns_message_id() -> None:
    fake = FakeLarkClient()
    card_id = fake.create_card_entity({"x": 1})
    mid = fake.send_card_id("oc_chat1", card_id)
    assert mid.startswith("om_fake")
    last = fake.last_sent()
    assert last.kind == "card_id"
    assert last.chat_id == "oc_chat1"
    assert last.card_id == card_id
    assert last.card == {}  # no inline card body on card_id references


def test_send_card_id_id_namespace_independent_from_card_id_namespace() -> None:
    """`om_fake*` (IM message ids) and `card_fake*` (cardkit card ids) come
    from independent counters — the IM message that references a card must
    not collide with the card's own id, and tests assert against both."""
    fake = FakeLarkClient()
    card_id = fake.create_card_entity({})
    mid = fake.send_card_id("c", card_id)
    assert card_id != mid
    assert card_id.startswith("card_fake")
    assert mid.startswith("om_fake")


def test_update_card_entity_records_sequence_and_uuid() -> None:
    fake = FakeLarkClient()
    card_id = fake.create_card_entity({"x": 1})
    fake.update_card_entity(card_id, {"x": 2}, sequence=1, uuid="u1")
    fake.update_card_entity(card_id, {"x": 3}, sequence=2)
    assert len(fake.card_entity_updates) == 2
    first, second = fake.card_entity_updates
    assert first.card_id == card_id
    assert first.card == {"x": 2}
    assert first.sequence == 1
    assert first.uuid == "u1"
    assert second.sequence == 2
    assert second.uuid is None
    assert fake.last_card_entity_update().card == {"x": 3}


def test_update_card_entity_is_defensive_copy() -> None:
    fake = FakeLarkClient()
    payload = {"x": 1}
    fake.update_card_entity("card_x", payload, sequence=1)
    payload["x"] = 99
    assert fake.card_entity_updates[0].card == {"x": 1}


def test_stream_element_content_records_per_call_fields() -> None:
    fake = FakeLarkClient()
    fake.stream_element_content(
        "card_x", "elem_text", "hello", sequence=1, uuid="u1"
    )
    fake.stream_element_content("card_x", "elem_text", "hello world", sequence=2)
    assert len(fake.element_content_updates) == 2
    first, second = fake.element_content_updates
    assert first.card_id == "card_x"
    assert first.element_id == "elem_text"
    assert first.content == "hello"
    assert first.sequence == 1
    assert first.uuid == "u1"
    assert second.content == "hello world"
    assert second.uuid is None
    assert fake.last_element_content_update().content == "hello world"


def test_cardkit_helpers_raise_when_empty() -> None:
    fake = FakeLarkClient()
    with pytest.raises(AssertionError):
        fake.last_card_entity()
    with pytest.raises(AssertionError):
        fake.last_card_entity_update()
    with pytest.raises(AssertionError):
        fake.last_element_content_update()


def test_fake_satisfies_protocol_with_cardkit_surface() -> None:
    """Cardkit methods are part of the LarkClient Protocol — assigning Fake
    to the Protocol type must still typecheck after the surface grew."""
    fake = FakeLarkClient()
    client: LarkClient = fake
    # Smoke-call each cardkit method to confirm the Protocol shape matches.
    cid = client.create_card_entity({})
    mid = client.send_card_id("c", cid)
    client.update_card_entity(cid, {}, sequence=1)
    client.stream_element_content(cid, "el", "x", sequence=1)
    assert mid.startswith("om_fake")


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
