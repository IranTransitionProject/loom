"""Tests for TelegramLiveIngestor — live channel monitoring via Telethon."""

import pytest


class TestTelegramLiveIngestorConfig:
    """Test configuration and validation (no Telethon needed)."""

    def test_extends_ingestor(self):
        from heddle.contrib.rag.ingestion.base import Ingestor
        from heddle.contrib.rag.ingestion.telegram_live import TelegramLiveIngestor

        assert issubclass(TelegramLiveIngestor, Ingestor)

    def test_load_requires_credentials(self):
        from heddle.contrib.rag.ingestion.telegram_live import TelegramLiveIngestor

        ingestor = TelegramLiveIngestor(channels=["@test"])
        with pytest.raises(ValueError, match="credentials"):
            ingestor.load()

    def test_load_requires_channels(self):
        from heddle.contrib.rag.ingestion.telegram_live import TelegramLiveIngestor

        ingestor = TelegramLiveIngestor(api_id=12345, api_hash="abc123", channels=[])
        with pytest.raises(ValueError, match="channel"):
            ingestor.load()

    def test_load_success(self):
        from heddle.contrib.rag.ingestion.telegram_live import TelegramLiveIngestor

        ingestor = TelegramLiveIngestor(
            channels=["@test"],
            api_id=12345,
            api_hash="abc123",
        )
        result = ingestor.load()
        assert result is ingestor

    def test_status_before_start(self):
        from heddle.contrib.rag.ingestion.telegram_live import TelegramLiveIngestor

        ingestor = TelegramLiveIngestor(
            channels=["@a", "@b"],
            api_id=1,
            api_hash="x",
        )
        status = ingestor.status()
        assert status["running"] is False
        assert status["channels_configured"] == 2
        assert status["total_received"] == 0
        assert status["buffer_size"] == 0

    def test_ingest_empty_buffer(self):
        from heddle.contrib.rag.ingestion.telegram_live import TelegramLiveIngestor

        ingestor = TelegramLiveIngestor(
            channels=["@test"],
            api_id=1,
            api_hash="x",
        )
        posts = ingestor.ingest_all()
        assert posts == []

    def test_ingest_drains_buffer(self):
        from datetime import UTC, datetime

        from heddle.contrib.rag.ingestion.telegram_live import TelegramLiveIngestor
        from heddle.contrib.rag.schemas.post import NormalizedPost

        ingestor = TelegramLiveIngestor(
            channels=["@test"],
            api_id=1,
            api_hash="x",
        )

        # Manually add posts to buffer
        post = NormalizedPost(
            global_id="1:1",
            source_channel_id=1,
            source_channel_name="test",
            message_id=1,
            timestamp=datetime(2026, 3, 1, tzinfo=UTC),
            text_clean="test post content",
        )
        ingestor._buffer.append(post)
        assert ingestor.buffer_size == 1

        posts = ingestor.ingest_all()
        assert len(posts) == 1
        assert posts[0].global_id == "1:1"
        assert ingestor.buffer_size == 0

    def test_fetch_recent(self):
        from datetime import UTC, datetime

        from heddle.contrib.rag.ingestion.telegram_live import TelegramLiveIngestor
        from heddle.contrib.rag.schemas.post import NormalizedPost

        ingestor = TelegramLiveIngestor(
            channels=["@test"],
            api_id=1,
            api_hash="x",
        )

        for i in range(5):
            post = NormalizedPost(
                global_id=f"1:{i}",
                source_channel_id=1,
                source_channel_name="test",
                message_id=i,
                timestamp=datetime(2026, 3, 1, i, tzinfo=UTC),
                text_clean=f"post {i}",
            )
            ingestor._buffer.append(post)

        recent = ingestor.fetch_recent(limit=3)
        assert len(recent) == 3
        # Should not drain the buffer
        assert ingestor.buffer_size == 5

    def test_env_var_defaults(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_API_ID", "99999")
        monkeypatch.setenv("TELEGRAM_API_HASH", "env_hash")
        monkeypatch.setenv("TELEGRAM_SESSION", "/tmp/test.session")

        from heddle.contrib.rag.ingestion.telegram_live import TelegramLiveIngestor

        ingestor = TelegramLiveIngestor(channels=["@test"])
        assert ingestor._api_id == 99999
        assert ingestor._api_hash == "env_hash"
        assert ingestor._session_path == "/tmp/test.session"


class TestNormalize:
    """Test shared normalization utilities."""

    def test_resolve_editor_profile_override(self):
        from heddle.contrib.rag.ingestion.normalize import resolve_editor_profile
        from heddle.contrib.rag.schemas.post import ChannelBias, ChannelEditorProfile

        override = ChannelEditorProfile(
            channel_id=1, channel_name="override", bias=ChannelBias.INDEPENDENT, trust_weight=0.9
        )
        result = resolve_editor_profile(1, "test", override=override)
        assert result.channel_name == "override"

    def test_resolve_editor_profile_from_dict(self):
        from heddle.contrib.rag.ingestion.normalize import resolve_editor_profile
        from heddle.contrib.rag.schemas.post import ChannelBias, ChannelEditorProfile

        profiles = {
            42: ChannelEditorProfile(
                channel_id=42, channel_name="dict", bias=ChannelBias.STATE_MEDIA, trust_weight=0.3
            )
        }
        result = resolve_editor_profile(42, "test", profiles=profiles)
        assert result.channel_name == "dict"

    def test_resolve_editor_profile_default(self):
        from heddle.contrib.rag.ingestion.normalize import resolve_editor_profile

        result = resolve_editor_profile(999, "fallback")
        assert result.channel_name == "fallback"
        assert result.trust_weight == 0.5


class TestNormalizeLiveText:
    """Pin C3: live and batch paths produce matching ``text_clean`` etc.

    Earlier the live handler hard-coded ``text_clean=text`` and
    ``text_rtl=True``, so a Persian post coming through the live
    capture path and the same Persian post coming through the batch
    JSON import path produced different embeddings.  Live now goes
    through the shared ``normalize_live_text`` helper.
    """

    def test_normalize_strips_emojis_and_normalizes_digits(self):
        from datetime import UTC, datetime

        from heddle.contrib.rag.ingestion.normalize import normalize_live_text

        # Eastern-Arabic digits + an emoji.  The shared normalizer
        # strips emojis and rewrites digits.  The earlier live path
        # echoed the raw text verbatim.
        ts = datetime(2026, 5, 11, tzinfo=UTC)
        # Build digits from codepoints so ruff doesn't flag the
        # Arabic-Indic forms as Latin homoglyphs.
        arabic_one = chr(0x0661)  # ARABIC-INDIC DIGIT ONE
        arabic_two = chr(0x0662)
        arabic_three = chr(0x0663)
        text = f"سلام {arabic_one}{arabic_two}{arabic_three} \U0001f600"
        post = normalize_live_text(
            text,
            channel_id=42,
            channel_name="ch",
            message_id=7,
            timestamp=ts,
        )
        assert post.global_id == "42:7"
        assert arabic_one not in post.text_clean, "Eastern-Arabic digits not normalized"
        assert "\U0001f600" not in post.text_clean, "emoji not stripped"
        # Original raw text preserved.
        assert arabic_one in post.text_raw

    def test_normalize_rtl_detection_runs(self):
        from datetime import UTC, datetime

        from heddle.contrib.rag.ingestion.normalize import normalize_live_text

        # English-only text should not be flagged RTL.  Earlier the
        # live path hard-coded ``text_rtl=True`` for everything.
        ts = datetime(2026, 5, 11, tzinfo=UTC)
        post = normalize_live_text(
            "Hello world from English text",
            channel_id=1,
            channel_name="ch",
            message_id=1,
            timestamp=ts,
        )
        assert post.text_rtl is False
