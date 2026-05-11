"""TelegramLiveIngestor: monitor Telegram channels in real-time via Telethon.

Connects to the Telegram API using MTProto (via Telethon) and listens for
new messages on configured channels. Each message is normalized into a
NormalizedPost and either yielded or published to NATS.

Requires the ``telegram`` optional dependency::

    pip install heddle-ai[telegram]

Environment variables:
    TELEGRAM_API_ID      — Telegram API ID (from https://my.telegram.org)
    TELEGRAM_API_HASH    — Telegram API hash
    TELEGRAM_SESSION     — Session file path (default: ~/.heddle/telegram.session)
    TELEGRAM_PHONE       — Phone number for first-time auth (interactive)

Actor message contract (NATS):
    Subject:  rag.ingestion.<channel_id>
    Payload:  NormalizedPost (JSON)

Usage::

    ingestor = TelegramLiveIngestor(
        channels=["@farsna", "@IranIntl_Fa"],
        api_id=12345,
        api_hash="abc...",
    )
    await ingestor.start()

    # Buffered posts available via ingest()
    for post in ingestor.ingest():
        process(post)

    await ingestor.stop()
"""

from __future__ import annotations

import logging
import os
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import Ingestor

if TYPE_CHECKING:
    from collections.abc import Generator

    from ..schemas.post import NormalizedPost

logger = logging.getLogger(__name__)


class TelegramLiveIngestor(Ingestor):
    """Live Telegram channel monitor using Telethon.

    Connects to the Telegram API and subscribes to new messages on the
    specified channels. Messages are buffered in memory and available
    via :meth:`ingest`.

    Args:
        channels: List of channel handles (e.g. ``["@farsna", "@bbcpersian"]``)
            or numeric channel IDs.
        api_id: Telegram API ID. Falls back to ``TELEGRAM_API_ID`` env var.
        api_hash: Telegram API hash. Falls back to ``TELEGRAM_API_HASH`` env var.
        session_path: Path to Telethon session file.
            Falls back to ``TELEGRAM_SESSION`` env var or ``~/.heddle/telegram.session``.
        buffer_size: Maximum number of posts to buffer before oldest are dropped.
        min_text_len: Minimum text length after normalization.
    """

    def __init__(
        self,
        channels: list[str | int] | None = None,
        api_id: int | None = None,
        api_hash: str | None = None,
        session_path: str | None = None,
        buffer_size: int = 10_000,
        min_text_len: int = 10,
    ) -> None:
        self._channels_config = channels or []
        self._api_id = api_id or int(os.environ.get("TELEGRAM_API_ID", "0"))
        self._api_hash = api_hash or os.environ.get("TELEGRAM_API_HASH", "")
        self._session_path = session_path or os.environ.get(
            "TELEGRAM_SESSION",
            str(Path.home() / ".heddle" / "telegram.session"),
        )
        self._buffer_size = buffer_size
        self._min_text_len = min_text_len

        self._buffer: deque[NormalizedPost] = deque(maxlen=buffer_size)
        self._client: Any = None
        self._running = False
        self._resolved_entities: dict[str | int, Any] = {}
        self._total_received = 0
        self._total_normalized = 0

    # ------------------------------------------------------------------
    # Ingestor interface
    # ------------------------------------------------------------------

    def load(self) -> TelegramLiveIngestor:
        """Validate configuration (no-op for live ingestor — use start())."""
        if not self._api_id or not self._api_hash:
            raise ValueError(
                "Telegram API credentials required. Set TELEGRAM_API_ID and "
                "TELEGRAM_API_HASH environment variables."
            )
        if not self._channels_config:
            raise ValueError("At least one channel must be specified.")
        return self

    def ingest(self) -> Generator[NormalizedPost, None, None]:
        """Yield buffered posts, draining the buffer."""
        while self._buffer:
            yield self._buffer.popleft()

    # ------------------------------------------------------------------
    # Async lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:  # pragma: no cover
        """Connect to Telegram and start listening for new messages.

        This is a long-running operation. Call from an async context.
        First-time auth may require interactive phone/code input.
        """
        try:
            from telethon import TelegramClient, events
        except ImportError as exc:
            raise ImportError(
                "Telethon is required for live Telegram capture. "
                "Install with: pip install heddle-ai[telegram]"
            ) from exc

        # Ensure session directory exists
        session_dir = Path(self._session_path).parent
        session_dir.mkdir(parents=True, exist_ok=True)

        self._client = TelegramClient(
            self._session_path,
            self._api_id,
            self._api_hash,
        )

        await self._client.start()
        logger.info(
            "Connected to Telegram as %s",
            (await self._client.get_me()).username or "unknown",
        )

        # Resolve channel entities
        for ch in self._channels_config:
            try:
                entity = await self._client.get_entity(ch)
                self._resolved_entities[ch] = entity
                logger.info(
                    "Resolved channel %s -> id=%d name=%s",
                    ch,
                    entity.id,
                    getattr(entity, "title", "?"),
                )
            except Exception as exc:
                logger.warning("Failed to resolve channel %s: %s", ch, exc)

        if not self._resolved_entities:
            raise ValueError("No channels could be resolved. Check channel handles.")

        # Register event handler for new messages
        channel_entities = list(self._resolved_entities.values())

        @self._client.on(events.NewMessage(chats=channel_entities))
        async def _on_new_message(event: Any) -> None:
            await self._handle_message(event)

        self._running = True
        logger.info(
            "Listening on %d channel(s) for new messages",
            len(self._resolved_entities),
        )

    async def stop(self) -> None:  # pragma: no cover
        """Disconnect from Telegram."""
        self._running = False
        if self._client:
            await self._client.disconnect()
            self._client = None
        logger.info(
            "Disconnected from Telegram. Received=%d, normalized=%d, buffered=%d",
            self._total_received,
            self._total_normalized,
            len(self._buffer),
        )

    async def run_until_disconnected(self) -> None:  # pragma: no cover
        """Block until the client disconnects (for standalone usage)."""
        if self._client:
            await self._client.run_until_disconnected()

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, event: Any) -> None:  # pragma: no cover
        """Process a new message event from Telethon."""
        self._total_received += 1

        try:
            msg = event.message
            chat = await event.get_chat()
            channel_id = chat.id
            channel_name = getattr(chat, "title", str(channel_id))

            # Build a lightweight normalized post from Telethon message
            text = msg.text or ""
            if len(text.strip()) < self._min_text_len:
                return

            from datetime import UTC

            from heddle.contrib.rag.ingestion.normalize import normalize_live_text

            timestamp = msg.date.replace(tzinfo=UTC) if msg.date.tzinfo is None else msg.date

            # Run the shared normalizer so live posts and batch-
            # imported posts produce matching ``text_clean`` /
            # ``text_rtl`` / ``language`` (C3).  Telethon doesn't
            # surface ``text_entities``, so link extraction is the
            # URL-regex subset; everything else agrees with the
            # batch path.
            post = normalize_live_text(
                text,
                channel_id=channel_id,
                channel_name=channel_name,
                message_id=msg.id,
                timestamp=timestamp,
                has_media=msg.media is not None,
                has_photo=msg.photo is not None,
                is_forward=msg.forward is not None,
                forwarded_from=(
                    str(msg.forward.from_id) if msg.forward and msg.forward.from_id else None
                ),
                reply_to_id=msg.reply_to.reply_to_msg_id if msg.reply_to else None,
                was_edited=msg.edit_date is not None,
            )

            self._buffer.append(post)
            self._total_normalized += 1

            logger.debug(
                "Captured message %d from %s (%d chars)",
                msg.id,
                channel_name,
                len(text),
            )

        except Exception:
            logger.exception("Failed to process Telethon message event")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether the client is connected and listening."""
        return self._running and self._client is not None

    @property
    def buffer_size(self) -> int:
        """Number of posts currently buffered."""
        return len(self._buffer)

    def status(self) -> dict[str, Any]:
        """Return current status summary."""
        return {
            "running": self.is_running,
            "channels_configured": len(self._channels_config),
            "channels_resolved": len(self._resolved_entities),
            "total_received": self._total_received,
            "total_normalized": self._total_normalized,
            "buffer_size": len(self._buffer),
            "buffer_capacity": self._buffer_size,
        }

    def fetch_recent(self, limit: int = 50) -> list[NormalizedPost]:
        """Return the most recent buffered posts without removing them."""
        recent = list(self._buffer)
        return recent[-limit:]
