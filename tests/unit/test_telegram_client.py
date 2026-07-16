"""Tests for TelegramPrivacyFenceClient's parsing logic (dialog/message
normalization, media classification, peer-id extraction) and the async
connect/read/write operations. Telethon's real types are used for the
isinstance-based dialog classification (User/Chat/Channel) via
MagicMock(spec=...), which Python's mock library recognizes for isinstance
checks; _build_client() itself is bypassed everywhere except the dedicated
connect() tests, since constructing a real Telethon client touches disk
(the session file).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from privacyfence.telegram_client import (
    TelegramChat,
    TelegramClientError,
    TelegramPrivacyFenceClient,
    _classify_media,
    _parse_dialog,
    _parse_message,
    _peer_id,
)
from telethon.tl.types import Channel, Chat, User

LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "telegram"


def make_client() -> TelegramPrivacyFenceClient:
    return TelegramPrivacyFenceClient(api_id=123, api_hash="hash", session_file="/tmp/unused.session")


def connected_client(fake_telethon_client: MagicMock) -> TelegramPrivacyFenceClient:
    client = make_client()
    client._client = fake_telethon_client
    client._connected = True
    return client


# ---------------------------------------------------------------------------- #
# _peer_id
# ---------------------------------------------------------------------------- #

class TestPeerId:
    def test_none_peer_returns_zero(self):
        assert _peer_id(None) == 0

    def test_user_id_extracted(self):
        assert _peer_id(SimpleNamespace(user_id=42)) == 42

    def test_chat_id_extracted_when_no_user_id(self):
        assert _peer_id(SimpleNamespace(chat_id=99)) == 99

    def test_channel_id_extracted_when_others_absent(self):
        assert _peer_id(SimpleNamespace(channel_id=7)) == 7

    def test_all_absent_returns_zero(self):
        assert _peer_id(SimpleNamespace()) == 0


# ---------------------------------------------------------------------------- #
# _classify_media
# ---------------------------------------------------------------------------- #

class MessageMediaPhoto:
    pass


class MessageMediaGeo:
    pass


class MessageMediaPoll:
    pass


class MessageMediaContact:
    pass


class DocAttrFilename:
    def __init__(self, file_name):
        self.file_name = file_name


class MessageMediaDocument:
    def __init__(self, mime_type="application/pdf", filename=None):
        attrs = [DocAttrFilename(filename)] if filename else []
        self.document = SimpleNamespace(mime_type=mime_type, attributes=attrs)


class TestClassifyMedia:
    def test_no_media_yields_empty(self):
        assert _classify_media(SimpleNamespace(media=None)) == ("", "")

    def test_photo(self):
        assert _classify_media(SimpleNamespace(media=MessageMediaPhoto())) == ("photo", "")

    def test_document_generic(self):
        media = MessageMediaDocument(mime_type="application/pdf", filename="report.pdf")
        assert _classify_media(SimpleNamespace(media=media)) == ("document", "report.pdf")

    def test_document_video_mime(self):
        media = MessageMediaDocument(mime_type="video/mp4", filename="clip.mp4")
        assert _classify_media(SimpleNamespace(media=media)) == ("video", "clip.mp4")

    def test_document_audio_mime(self):
        media = MessageMediaDocument(mime_type="audio/ogg", filename="voice.ogg")
        assert _classify_media(SimpleNamespace(media=media)) == ("audio", "voice.ogg")

    def test_document_voice_mime(self):
        media = MessageMediaDocument(mime_type="audio/x-voice", filename=None)
        assert _classify_media(SimpleNamespace(media=media)) == ("audio", "")

    def test_document_no_filename(self):
        media = MessageMediaDocument(mime_type="application/zip", filename=None)
        assert _classify_media(SimpleNamespace(media=media)) == ("document", "")

    def test_geo_and_venue(self):
        assert _classify_media(SimpleNamespace(media=MessageMediaGeo())) == ("location", "")

    def test_poll(self):
        assert _classify_media(SimpleNamespace(media=MessageMediaPoll())) == ("poll", "")

    def test_contact(self):
        assert _classify_media(SimpleNamespace(media=MessageMediaContact())) == ("contact", "")

    def test_unrecognized_media_falls_back_to_generic(self):
        class SomeOtherMedia:
            pass
        assert _classify_media(SimpleNamespace(media=SomeOtherMedia())) == ("media", "")


# ---------------------------------------------------------------------------- #
# _parse_dialog
# ---------------------------------------------------------------------------- #

class TestParseDialog:
    def test_regular_user(self):
        entity = MagicMock(spec=User)
        entity.id = 1
        entity.username = "jdoe"
        entity.is_self = False
        entity.bot = False
        dialog = SimpleNamespace(name="Jane", unread_count=3)

        chat = _parse_dialog(dialog, entity)

        assert chat == TelegramChat(id=1, name="Jane", username="jdoe", chat_type="user", unread_count=3, is_self=False)

    def test_bot_user(self):
        entity = MagicMock(spec=User)
        entity.id = 2
        entity.username = "mybot"
        entity.is_self = False
        entity.bot = True
        dialog = SimpleNamespace(name="MyBot", unread_count=0)

        chat = _parse_dialog(dialog, entity)
        assert chat.chat_type == "bot"

    def test_self_chat_saved_messages(self):
        entity = MagicMock(spec=User)
        entity.id = 3
        entity.username = ""
        entity.is_self = True
        entity.bot = False
        dialog = SimpleNamespace(name="Saved Messages", unread_count=0)

        chat = _parse_dialog(dialog, entity)
        assert chat.is_self is True
        assert chat.chat_type == "user"

    def test_basic_group_chat(self):
        entity = MagicMock(spec=Chat)
        entity.id = 4
        entity.username = None
        entity.is_self = False
        dialog = SimpleNamespace(name="Group Chat", unread_count=5)

        chat = _parse_dialog(dialog, entity)
        assert chat.chat_type == "group"
        assert chat.username == ""

    def test_channel_not_megagroup(self):
        entity = MagicMock(spec=Channel)
        entity.id = 5
        entity.username = "channel1"
        entity.is_self = False
        entity.megagroup = False
        dialog = SimpleNamespace(name="News Channel", unread_count=0)

        chat = _parse_dialog(dialog, entity)
        assert chat.chat_type == "channel"

    def test_channel_megagroup_treated_as_group(self):
        entity = MagicMock(spec=Channel)
        entity.id = 6
        entity.username = "supergroup1"
        entity.is_self = False
        entity.megagroup = True
        dialog = SimpleNamespace(name="Supergroup", unread_count=0)

        chat = _parse_dialog(dialog, entity)
        assert chat.chat_type == "group"

    def test_dialog_name_falls_back_to_entity_id(self):
        entity = MagicMock(spec=User)
        entity.id = 7
        entity.username = ""
        entity.is_self = False
        entity.bot = False
        dialog = SimpleNamespace(name="", unread_count=0)

        chat = _parse_dialog(dialog, entity)
        assert chat.name == "7"

    def test_to_dict_round_trips(self):
        chat = TelegramChat(id=1, name="Jane", username="jdoe", chat_type="user", unread_count=2, is_self=False)
        assert chat.to_dict() == {
            "id": 1, "name": "Jane", "username": "jdoe", "chat_type": "user",
            "unread_count": 2, "is_self": False,
        }


# ---------------------------------------------------------------------------- #
# _parse_message
# ---------------------------------------------------------------------------- #

class TestParseMessage:
    def test_full_message_with_sender_username(self):
        msg = SimpleNamespace(
            id=1, sender=SimpleNamespace(id=10, username="jdoe", first_name="Jane", last_name="Doe"),
            date=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
            text="hello", message="hello", out=False, media=None,
        )
        result = _parse_message(msg, chat_id=5, chat_name="General")
        assert result.sender_id == 10
        assert result.sender_name == "jdoe"
        assert result.date == "2024-01-01T12:00:00+00:00"
        assert result.is_outgoing is False

    def test_sender_name_falls_back_to_full_name_without_username(self):
        msg = SimpleNamespace(
            id=1, sender=SimpleNamespace(id=10, username="", first_name="Jane", last_name="Doe"),
            date=None, text="hi", message="hi", out=False, media=None,
        )
        result = _parse_message(msg, 5, "General")
        assert result.sender_name == "Jane Doe"

    def test_sender_name_falls_back_to_id_when_no_name_at_all(self):
        msg = SimpleNamespace(
            id=1, sender=SimpleNamespace(id=10, username="", first_name="", last_name=""),
            date=None, text="hi", message="hi", out=False, media=None,
        )
        result = _parse_message(msg, 5, "General")
        assert result.sender_name == "10"

    def test_no_sender_yields_zero_id_and_empty_name(self):
        msg = SimpleNamespace(id=1, sender=None, date=None, text="hi", message="hi", out=False, media=None)
        result = _parse_message(msg, 5, "General")
        assert result.sender_id == 0
        assert result.sender_name == ""

    def test_naive_datetime_treated_as_utc(self):
        msg = SimpleNamespace(
            id=1, sender=None, date=datetime(2024, 1, 1, 12, 0), text="hi", message="hi", out=False, media=None,
        )
        result = _parse_message(msg, 5, "General")
        assert result.date == "2024-01-01T12:00:00+00:00"

    def test_no_date_yields_empty_string(self):
        msg = SimpleNamespace(id=1, sender=None, date=None, text="hi", message="hi", out=False, media=None)
        result = _parse_message(msg, 5, "General")
        assert result.date == ""

    def test_text_falls_back_to_message_attribute(self):
        msg = SimpleNamespace(id=1, sender=None, date=None, text=None, message="fallback text", out=False, media=None)
        result = _parse_message(msg, 5, "General")
        assert result.text == "fallback text"

    def test_short_summary_uses_media_type_when_no_text(self):
        result_msg = SimpleNamespace(
            id=1, chat_id=5, chat_name="General", sender_id=0, sender_name="Bot",
            text="", date="", is_outgoing=False, media_type="photo", media_filename="",
        )
        from privacyfence.telegram_client import TelegramMessage
        tm = TelegramMessage(**result_msg.__dict__)
        assert tm.short_summary() == "Bot: [photo]"

    def test_to_dict_round_trips(self):
        from privacyfence.telegram_client import TelegramMessage
        tm = TelegramMessage(
            id=1, chat_id=5, chat_name="General", sender_id=10, sender_name="Jane",
            text="hi", date="d", is_outgoing=True, media_type="", media_filename="",
        )
        assert tm.to_dict()["sender_name"] == "Jane"
        assert tm.to_dict()["is_outgoing"] is True


# ---------------------------------------------------------------------------- #
# connect(): the interactive-vs-cached-session distinction
# ---------------------------------------------------------------------------- #

class TestConnect:
    async def test_already_connected_short_circuits(self, monkeypatch):
        client = make_client()
        client._connected = True
        client._client = MagicMock()
        build_called = []
        monkeypatch.setattr(client, "_build_client", lambda: build_called.append(1))

        await client.connect()
        assert build_called == []

    async def test_connect_failure_wraps_exception(self, monkeypatch):
        client = make_client()
        fake_telethon_client = MagicMock()
        fake_telethon_client.connect = AsyncMock(side_effect=RuntimeError("network down"))
        monkeypatch.setattr(client, "_build_client", lambda: fake_telethon_client)

        with pytest.raises(TelegramClientError, match="Failed to connect"):
            await client.connect()

    async def test_unauthorized_session_disconnects_and_raises(self, monkeypatch):
        client = make_client()
        fake_telethon_client = MagicMock()
        fake_telethon_client.connect = AsyncMock()
        fake_telethon_client.is_user_authorized = AsyncMock(return_value=False)
        fake_telethon_client.disconnect = AsyncMock()
        monkeypatch.setattr(client, "_build_client", lambda: fake_telethon_client)

        with pytest.raises(TelegramClientError, match="not authorized"):
            await client.connect()
        fake_telethon_client.disconnect.assert_awaited_once()

    async def test_authorized_session_sets_connected_state(self, monkeypatch):
        client = make_client()
        fake_telethon_client = MagicMock()
        fake_telethon_client.connect = AsyncMock()
        fake_telethon_client.is_user_authorized = AsyncMock(return_value=True)
        monkeypatch.setattr(client, "_build_client", lambda: fake_telethon_client)

        await client.connect()

        assert client._connected is True
        assert client._client is fake_telethon_client


# ---------------------------------------------------------------------------- #
# check_connection
# ---------------------------------------------------------------------------- #

class TestCheckConnection:
    async def test_formats_name_and_username(self):
        fake = MagicMock()
        fake.get_me = AsyncMock(return_value=SimpleNamespace(first_name="Jane", last_name="Doe", username="jdoe"))
        client = connected_client(fake)
        result = await client.check_connection()
        assert result == "Jane Doe (@jdoe)"

    async def test_no_username_omits_parens(self):
        fake = MagicMock()
        fake.get_me = AsyncMock(return_value=SimpleNamespace(first_name="Jane", last_name="", username=""))
        client = connected_client(fake)
        result = await client.check_connection()
        assert result == "Jane"

    async def test_error_becomes_telegram_client_error(self):
        fake = MagicMock()
        fake.get_me = AsyncMock(side_effect=RuntimeError("boom"))
        client = connected_client(fake)
        with pytest.raises(TelegramClientError, match="get_me"):
            await client.check_connection()


# ---------------------------------------------------------------------------- #
# list_chats / get_messages / search_messages / send_message
# ---------------------------------------------------------------------------- #

class TestListChats:
    async def test_populates_chat_name_cache(self):
        entity = MagicMock(spec=User)
        entity.id = 1
        entity.username = "jdoe"
        entity.is_self = False
        entity.bot = False
        dialog = SimpleNamespace(name="Jane", unread_count=0, entity=entity)

        fake = MagicMock()
        fake.get_dialogs = AsyncMock(return_value=[dialog])
        client = connected_client(fake)

        chats = await client.list_chats()

        assert chats[0].name == "Jane"
        assert client._chat_name_cache[1] == "Jane"

    async def test_limit_clamped_into_1_to_200(self):
        fake = MagicMock()
        fake.get_dialogs = AsyncMock(return_value=[])
        client = connected_client(fake)
        await client.list_chats(limit=5000)
        assert fake.get_dialogs.call_args.kwargs["limit"] == 200

    async def test_error_becomes_telegram_client_error(self):
        fake = MagicMock()
        fake.get_dialogs = AsyncMock(side_effect=RuntimeError("boom"))
        client = connected_client(fake)
        with pytest.raises(TelegramClientError, match="get_dialogs"):
            await client.list_chats()


class TestGetMessages:
    async def test_uses_cached_chat_name(self):
        msg = SimpleNamespace(id=1, sender=None, date=None, text="hi", message="hi", out=False, media=None)
        fake = MagicMock()
        fake.get_messages = AsyncMock(return_value=[msg])
        client = connected_client(fake)
        client._chat_name_cache[5] = "General"

        messages = await client.get_messages(5)

        assert messages[0].chat_name == "General"

    async def test_uncached_chat_falls_back_to_str_id(self):
        msg = SimpleNamespace(id=1, sender=None, date=None, text="hi", message="hi", out=False, media=None)
        fake = MagicMock()
        fake.get_messages = AsyncMock(return_value=[msg])
        client = connected_client(fake)

        messages = await client.get_messages(999)
        assert messages[0].chat_name == "999"

    async def test_error_becomes_telegram_client_error(self):
        fake = MagicMock()
        fake.get_messages = AsyncMock(side_effect=RuntimeError("boom"))
        client = connected_client(fake)
        with pytest.raises(TelegramClientError, match="get_messages"):
            await client.get_messages(5)


class TestSearchMessages:
    async def test_resolves_chat_id_from_peer(self):
        msg = SimpleNamespace(
            id=1, sender=None, date=None, text="found", message="found", out=False, media=None,
            peer_id=SimpleNamespace(channel_id=42),
        )
        fake = MagicMock()
        fake.get_messages = AsyncMock(return_value=[msg])
        client = connected_client(fake)

        results = await client.search_messages("query")

        assert results[0].chat_id == 42
        fake.get_messages.assert_awaited_once_with(None, search="query", limit=30)

    async def test_error_becomes_telegram_client_error(self):
        fake = MagicMock()
        fake.get_messages = AsyncMock(side_effect=RuntimeError("boom"))
        client = connected_client(fake)
        with pytest.raises(TelegramClientError, match="search_messages"):
            await client.search_messages("q")


class TestGetChatName:
    async def test_returns_cached_name_without_a_lookup(self):
        client = connected_client(MagicMock())
        client._chat_name_cache[5] = "General"

        assert await client.get_chat_name(5) == "General"

    async def test_resolves_and_caches_uncached_user_entity(self):
        entity = MagicMock(spec=User)
        entity.first_name = "Jane"
        entity.last_name = "Doe"
        fake = MagicMock()
        fake.get_entity = AsyncMock(return_value=entity)
        client = connected_client(fake)

        name = await client.get_chat_name(5)

        assert name == "Jane Doe"
        assert client._chat_name_cache[5] == "Jane Doe"

    async def test_lookup_failure_returns_empty_string_not_raise(self):
        fake = MagicMock()
        fake.get_entity = AsyncMock(side_effect=RuntimeError("no such peer"))
        client = connected_client(fake)

        assert await client.get_chat_name(999) == ""
        assert 999 not in client._chat_name_cache


class TestSendMessage:
    async def test_requires_non_empty_text(self):
        client = connected_client(MagicMock())
        with pytest.raises(TelegramClientError, match="requires non-empty text"):
            await client.send_message(5, "")

    async def test_sends_and_returns_result_dict(self):
        fake = MagicMock()
        fake.send_message = AsyncMock(return_value=SimpleNamespace(id=99))
        client = connected_client(fake)
        client._chat_name_cache[5] = "General"

        result = await client.send_message(5, "hi")

        assert result == {"chat_id": 5, "chat_name": "General", "msg_id": 99, "text": "hi"}

    async def test_error_becomes_telegram_client_error(self):
        fake = MagicMock()
        fake.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        client = connected_client(fake)
        with pytest.raises(TelegramClientError, match="send_message"):
            await client.send_message(5, "hi")


# ---------------------------------------------------------------------------- #
# Live fixture replay
# ---------------------------------------------------------------------------- #

def _message_from_fixture(raw: dict) -> SimpleNamespace:
    """Reconstruct just enough of a Telethon Message's attribute shape from
    a recorded fixture dict to drive the real _parse_message().

    Unlike every other connector's fixture (a plain JSON dict/list that its
    _parse_* method already accepts as-is), _parse_message() reads
    attributes (msg.sender, msg.date, ...) off a real Telethon object, not
    dict keys -- and msg.sender in particular is a *lazily resolved*
    Telethon property (SenderGetter), not part of to_dict()'s output at
    all, so this can only rebuild an id-only stand-in from from_id, not a
    fully resolved profile. That's a real, structural limitation of
    replaying Telegram fixtures, not an oversight.
    """
    from_id = raw.get("from_id") or {}
    user_id = from_id.get("user_id")
    sender = SimpleNamespace(id=user_id, username="", first_name="", last_name="") if user_id else None
    date_str = raw.get("date")
    date = datetime.fromisoformat(date_str) if date_str else None
    return SimpleNamespace(
        id=raw.get("id"), sender=sender, date=date,
        text=raw.get("message", ""), message=raw.get("message", ""),
        out=False, media=None,
    )


class TestLiveFixtureParsing:
    """Replays a fixture recorded from the real, [QATEST]-tagged seed
    message in Saved Messages by scripts/qa_fixture_recorder.py --record
    telegram -- real API shape, not hand-authored. Skipped (not failed)
    until that fixture exists; see tests/fixtures/live/README.md and
    docs/testing-policy.md. Re-record via that
    script if this ever starts failing after a genuine Telegram API change.
    """

    def test_get_messages_fixture_still_parses(self):
        path = LIVE_FIXTURES_DIR / "get_messages.json"
        if not path.exists():
            pytest.skip(
                f"{path} not recorded yet -- run "
                "`python3 scripts/qa_fixture_recorder.py --record telegram` locally first"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw, "recorded fixture has no messages"

        msg = _message_from_fixture(raw[0])
        parsed = _parse_message(msg, chat_id=0, chat_name="Saved Messages")

        assert parsed.text and "[QATEST]" in parsed.text
