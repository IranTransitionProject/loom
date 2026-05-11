r"""Persian-aware sentence-boundary chunker for RAG text splitting.

Persian sentence endings:
  - U+06D4 (Arabic full stop -- rare but present)
  - . followed by space/newline (common in mixed text)
  - \\n\\n (paragraph break -- very common in Telegram posts)
  - ! ?
  - U+061F (Arabic question mark)

Strategy: split on paragraph breaks first, then on sentence-terminal
punctuation. Merge short sentences up to target_chars. Always respect
max_chars hard ceiling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..schemas.chunk import ChunkStrategy, TextChunk

if TYPE_CHECKING:
    from ..schemas.mux import MuxEntry
    from ..schemas.post import NormalizedPost

# Paragraph boundary -- two or more newlines
_PARA_SPLIT = re.compile(r"\n{2,}")

# Bullet prefix common in Telegram
_BULLET_SPLIT = re.compile(
    r"(?=\U0001f539|\U0001f538|\u2022|\u25aa|\U0001f534|\U0001f535)", re.UNICODE
)


@dataclass(frozen=True)
class _Segment:
    """A text segment with its byte-offset position in the original source.

    Offsets are tracked at split time so chunk citations stay accurate
    even when the original text has leading whitespace or repeated
    phrases.  Earlier the chunker post-stripped each segment then used
    ``text.find(seg, cumulative_offset)`` to recover offsets, which
    could locate the wrong occurrence of a duplicated phrase and
    silently fall back to ``cumulative_offset``.
    """

    text: str  # the segment content (post-strip)
    start: int  # offset in the source text where this segment begins
    end: int  # offset in the source text where this segment ends (exclusive)


@dataclass
class ChunkConfig:
    """Configuration for text chunking."""

    strategy: ChunkStrategy = ChunkStrategy.SENTENCE
    target_chars: int = 400  # soft target per chunk
    max_chars: int = 600  # hard ceiling
    overlap_chars: int = 50  # overlap between consecutive chunks
    min_chars: int = 20  # don't emit chunks shorter than this


def chunk_post(
    post: NormalizedPost,
    config: ChunkConfig | None = None,
) -> list[TextChunk]:
    """Split a NormalizedPost into TextChunk objects.

    Returns at least one chunk (the full text) even if < min_chars.

    Offsets are tracked through the entire split/merge pipeline so
    ``text[chunk.char_start:chunk.char_end] == chunk.text`` holds
    for every chunk (modulo any leading/trailing whitespace the
    splitter stripped — the offsets bracket the *stripped* content's
    position in the source).
    """
    cfg = config or ChunkConfig()
    text = post.text_clean

    if not text.strip():
        return []

    if cfg.strategy == ChunkStrategy.WHOLE_POST:
        return [_make_chunk(post, text, 0, len(text), 0, 1, cfg)]

    # Split into candidate offset-tracked segments.
    if cfg.strategy == ChunkStrategy.PARAGRAPH:
        segments = _para_split(text)
    elif cfg.strategy == ChunkStrategy.SENTENCE:
        segments = _sentence_split(text)
    elif cfg.strategy == ChunkStrategy.FIXED_CHAR:
        segments = _fixed_char_split(text, cfg.max_chars)
    else:
        segments = [_Segment(text=text, start=0, end=len(text))]

    # Merge short segments and enforce max_chars.
    merged = _merge_segments(segments, cfg.target_chars, cfg.max_chars, text)

    # Drop segments below ``min_chars`` (unless we'd end up with
    # nothing).  Offsets are already correct on the survivors.
    kept = [seg for seg in merged if len(seg.text) >= cfg.min_chars] or merged

    total = len(kept)
    return [
        _make_chunk(post, seg.text, seg.start, seg.end, i, total, cfg) for i, seg in enumerate(kept)
    ]


def chunk_mux_entry(entry: MuxEntry, config: ChunkConfig | None = None) -> list[TextChunk]:
    """Convenience wrapper for MuxEntry."""
    return chunk_post(entry.post, config)


# ---------------------------------------------------------------------------
# Internal splitting helpers
# ---------------------------------------------------------------------------


def _strip_segment(raw: str, raw_start: int) -> _Segment | None:
    """Strip surrounding whitespace and return an offset-tracked segment.

    ``raw_start`` is the start offset of ``raw`` in the original text.
    Returns ``None`` if the segment is empty after stripping.
    """
    stripped = raw.strip()
    if not stripped:
        return None
    lead = len(raw) - len(raw.lstrip())
    trail = len(raw) - len(raw.rstrip())
    return _Segment(text=stripped, start=raw_start + lead, end=raw_start + len(raw) - trail)


def _para_split(text: str) -> list[_Segment]:
    """Split on paragraph breaks (double newlines), tracking offsets."""
    segments: list[_Segment] = []
    cursor = 0
    for match in _PARA_SPLIT.finditer(text):
        seg = _strip_segment(text[cursor : match.start()], cursor)
        if seg is not None:
            segments.append(seg)
        cursor = match.end()
    # Tail after the last separator (or the whole string if no separator).
    seg = _strip_segment(text[cursor:], cursor)
    if seg is not None:
        segments.append(seg)
    return segments


_BULLET_RE = re.compile(r"[\U0001f539\U0001f538\u2022\u25aa\U0001f534\U0001f535]")
_SENTENCE_TERMINAL_RE = re.compile(r"([.!?\u061f\u06d4])")


def _split_by_indices(text: str, base_offset: int, indices: list[int]) -> list[_Segment]:
    """Split ``text`` at the given absolute indices and emit offset-tracked segments.

    ``indices`` are split *boundaries* (positions where a new segment
    begins), assumed sorted and inside ``text``.  ``base_offset`` is the
    start of ``text`` in the source.
    """
    if not indices:
        seg = _strip_segment(text, base_offset)
        return [seg] if seg is not None else []
    boundaries = [0, *indices, len(text)]
    out: list[_Segment] = []
    for i in range(len(boundaries) - 1):
        a, b = boundaries[i], boundaries[i + 1]
        seg = _strip_segment(text[a:b], base_offset + a)
        if seg is not None:
            out.append(seg)
    return out


def _sentence_split(text: str) -> list[_Segment]:
    """Split on paragraph breaks, then sentence terminals, tracking offsets.

    Also splits on bullet markers common in Telegram.
    """
    paragraphs = _para_split(text)
    sentences: list[_Segment] = []
    for para in paragraphs:
        if _BULLET_RE.search(para.text):
            # Bullet markers start a new segment; record positions of
            # each bullet character to split on.
            indices = [m.start() for m in _BULLET_RE.finditer(para.text) if m.start() > 0]
            sentences.extend(_split_by_indices(para.text, para.start, indices))
        else:
            # Sentence-terminal split: cut *after* each terminal so the
            # punctuation stays attached to the preceding sentence.
            indices = [m.end() for m in _SENTENCE_TERMINAL_RE.finditer(para.text)]
            # Drop trailing boundary if it equals end-of-text (no
            # content follows it).
            indices = [i for i in indices if i < len(para.text)]
            sentences.extend(_split_by_indices(para.text, para.start, indices))

    return sentences if sentences else [_Segment(text=text, start=0, end=len(text))]


def _fixed_char_split(text: str, max_chars: int) -> list[_Segment]:
    """Hard split every ``max_chars`` characters, tracking offsets."""
    out: list[_Segment] = []
    for i in range(0, len(text), max_chars):
        chunk = text[i : i + max_chars]
        if chunk:
            # Don't strip \u2014 fixed-char split is meant to preserve every
            # character, so offsets are exactly ``[i, i+len)``.
            out.append(_Segment(text=chunk, start=i, end=i + len(chunk)))
    return out


def _merge_segments(
    segments: list[_Segment],
    target: int,
    max_chars: int,
    source: str,
) -> list[_Segment]:
    """Merge short consecutive segments up to ``target``; hard-split if > ``max_chars``.

    Offsets follow the source: a merged segment's ``start`` is the
    first input segment's start, its ``end`` is the last input
    segment's end, and its ``text`` is ``source[start:end]``.  This
    preserves the source's original inter-segment separators (e.g.
    ``". "``) in the chunk text, so the invariant
    ``source[char_start:char_end] == chunk.text`` holds for every
    chunk a downstream citation might point at.
    """
    merged: list[_Segment] = []
    buf: _Segment | None = None

    def _join(a: _Segment, b: _Segment) -> _Segment:
        return _Segment(text=source[a.start : b.end], start=a.start, end=b.end)

    for seg in segments:
        candidate = _join(buf, seg) if buf is not None else seg
        if len(candidate.text) <= max_chars:
            buf = candidate
            if len(buf.text) >= target:
                merged.append(buf)
                buf = None
        else:
            if buf is not None:
                merged.append(buf)
            if len(seg.text) > max_chars:
                # Hard-split the oversize segment.  Use raw source
                # slice so offsets follow the source exactly.
                base = seg.start
                for i in range(0, len(seg.text), max_chars):
                    piece = source[base + i : base + i + max_chars]
                    merged.append(_Segment(text=piece, start=base + i, end=base + i + len(piece)))
                buf = None
            else:
                buf = seg

    if buf is not None:
        merged.append(buf)

    return merged if merged else segments


def _make_chunk(
    post: NormalizedPost,
    text: str,
    char_start: int,
    char_end: int,
    index: int,
    total: int,
    cfg: ChunkConfig,
) -> TextChunk:
    return TextChunk(
        chunk_id=f"{post.global_id}:{index}",
        source_global_id=post.global_id,
        source_channel_id=post.source_channel_id,
        source_channel_name=post.source_channel_name,
        timestamp_unix=post.timestamp_unix,
        text=text,
        char_start=char_start,
        char_end=char_end,
        chunk_index=index,
        total_chunks=total,
        strategy=cfg.strategy,
        overlap_chars=cfg.overlap_chars,
    )
