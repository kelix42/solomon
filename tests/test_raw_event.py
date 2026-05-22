"""Tests for the RawEvent capture model."""
from datetime import datetime, timezone
from solomon.capture.raw_event import RawEvent, raw_event_from_message


def test_raw_event_serializes_to_db_row():
    ev = RawEvent(
        id="x",
        source="gmail",
        received_at=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
        participants=["alice@example.com"],
        raw_content="hello",
        channel_metadata={"thread_id": 42},
    )
    row = ev.to_db_row(tenant_id="t1", salience_score=0.7, private=False)
    assert row["event_id"] == "x"
    assert row["tenant_id"] == "t1"
    assert row["salience_score"] == 0.7
    assert '"alice@example.com"' in row["participants"]
    assert row["channel_metadata"] == '{"thread_id": 42}'


def test_from_message_handles_object_attributes():
    class Event:
        text = "Need a quote for warehouse cleanup"
        platform = "telegram"
        user_id = 42
        chat_id = -1001
        thread_id = 17
        message_id = "abc"
    ev = raw_event_from_message(Event())
    assert ev is not None
    assert ev.raw_content == "Need a quote for warehouse cleanup"
    assert ev.source == "telegram"
    assert "42" in ev.participants
    assert ev.channel_metadata["chat_id"] == -1001


def test_from_message_handles_dict():
    ev = raw_event_from_message({"text": "hi"})
    assert ev is not None
    assert ev.raw_content == "hi"


def test_from_message_returns_none_for_empty():
    assert raw_event_from_message(None) is None
    assert raw_event_from_message({}) is None
