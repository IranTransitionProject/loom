"""Shared normalization utilities for Telegram message processing.

Extracted from TelegramIngestor to allow reuse by both the batch
JSON ingestor and the live Telethon-based ingestor.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..schemas.post import ChannelBias, ChannelEditorProfile, Language, NormalizedPost
from ..tools.rtl_normalizer import extract_links_from_entities, normalize

if TYPE_CHECKING:
    from datetime import datetime

    from ..schemas.telegram import RawTelegramMessage

logger = logging.getLogger(__name__)


def normalize_live_text(
    text: str,
    *,
    channel_id: int,
    channel_name: str,
    message_id: int,
    timestamp: datetime,
    has_media: bool = False,
    has_photo: bool = False,
    is_forward: bool = False,
    forwarded_from: str | None = None,
    reply_to_id: int | None = None,
    was_edited: bool = False,
) -> NormalizedPost:
    """Normalize a live (Telethon-sourced) message into a NormalizedPost.

    Live-captured posts go through the same ``normalize()`` call the
    batch ingestor uses, so ``text_clean``, ``text_rtl``, and
    ``language`` agree across paths.  Earlier the live path
    hard-coded ``text_clean=text`` and ``text_rtl=True``, so RAG
    consistency between batch and live capture was broken: identical
    messages produced different vector embeddings depending on which
    path they entered the store through.

    Telethon does not surface ``text_entities``, so link extraction
    falls back to the URL regex inside ``normalize()`` — the batch
    path's entity-based extraction is strictly richer but the live
    path's URL-regex result is a strict subset, not divergent data.
    """
    norm = normalize(
        text,
        strip_emojis=True,
        normalize_digits=True,
        strip_tg_footer=True,
        preserve_zwnj=True,
    )
    try:
        language = Language(norm.language_hint)
    except ValueError:
        language = Language.UNKNOWN
    return NormalizedPost(
        source_channel_id=channel_id,
        source_channel_name=channel_name,
        message_id=message_id,
        global_id=f"{channel_id}:{message_id}",
        timestamp=timestamp,
        timestamp_unix=int(timestamp.timestamp()),
        text_raw=text,
        text_clean=norm.text_clean,
        text_rtl=norm.is_rtl,
        language=language,
        has_media=has_media,
        has_photo=has_photo,
        is_forward=is_forward,
        forwarded_from=forwarded_from,
        reply_to_id=reply_to_id,
        was_edited=was_edited,
        links=norm.links,
        hashtags=norm.hashtags,
        mentions=norm.mentions,
    )


def normalize_telegram_message(  # pragma: no cover
    msg: RawTelegramMessage,
    channel_id: int,
    channel_name: str,
    *,
    skip_empty: bool = True,
    min_text_len: int = 10,
) -> NormalizedPost | None:
    """Normalize a single RawTelegramMessage into a NormalizedPost.

    Returns None if the message should be skipped (empty, too short, etc.).

    Args:
        msg: Validated Telegram message.
        channel_id: Numeric channel ID.
        channel_name: Human-readable channel name.
        skip_empty: Skip messages with empty text.
        min_text_len: Minimum text length after normalization.
    """
    plain = msg.plain_text
    if skip_empty and not plain.strip():
        return None

    # Extract links from entities
    entities_raw = [e.model_dump() for e in msg.text_entities] if msg.text_entities else []
    links = extract_links_from_entities(entities_raw)

    # Normalize text
    norm = normalize(
        plain,
        strip_emojis=True,
        normalize_digits=True,
        strip_tg_footer=True,
        preserve_zwnj=True,
        links=links,
    )

    if len(norm.text_clean) < min_text_len:
        return None

    # Reaction breakdown (skip 'paid' type which has no emoji)
    reaction_breakdown = {r.emoji: r.count for r in msg.reactions if r.emoji is not None}

    # Resolve language hint safely
    try:
        language = Language(norm.language_hint)
    except ValueError:
        language = Language.UNKNOWN

    return NormalizedPost(
        source_channel_id=channel_id,
        source_channel_name=channel_name,
        message_id=msg.id,
        global_id=f"{channel_id}:{msg.id}",
        timestamp=msg.date,
        timestamp_unix=int(msg.date_unixtime),
        text_raw=plain,
        text_clean=norm.text_clean,
        text_rtl=norm.is_rtl,
        language=language,
        has_media=msg.has_media,
        has_photo=bool(msg.photo),
        media_type=msg.media_type,
        is_forward=msg.is_forward,
        forwarded_from=msg.forwarded_from,
        reply_to_id=msg.reply_to_message_id,
        reaction_total=msg.reaction_total,
        reaction_breakdown=reaction_breakdown,
        was_edited=msg.edited is not None,
        links=norm.links,
        hashtags=norm.hashtags,
        mentions=norm.mentions,
    )


def resolve_editor_profile(
    channel_id: int,
    channel_name: str,
    profiles: dict[int, ChannelEditorProfile] | None = None,
    override: ChannelEditorProfile | None = None,
) -> ChannelEditorProfile:
    """Resolve the editorial profile for a channel.

    Priority: override > profiles dict > synthesized default.
    """
    if override:
        return override
    if profiles and channel_id in profiles:
        return profiles[channel_id]
    return ChannelEditorProfile(
        channel_id=channel_id,
        channel_name=channel_name,
        bias=ChannelBias.UNKNOWN,
        language=Language.PERSIAN,
        trust_weight=0.5,
    )
