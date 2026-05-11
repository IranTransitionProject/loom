"""Unit tests for heddle.contrib.rag.chunker — sentence chunker."""

from datetime import UTC, datetime

from heddle.contrib.rag.schemas.post import NormalizedPost


def _make_post(text: str, gid: str = "1:1") -> NormalizedPost:

    return NormalizedPost(
        global_id=gid,
        source_channel_id=1,
        source_channel_name="test_ch",
        message_id=1,
        timestamp=datetime(2026, 3, 1, tzinfo=UTC),
        text_clean=text,
    )


class TestSentenceChunker:
    def test_empty_text(self):
        from heddle.contrib.rag.chunker.sentence_chunker import chunk_post

        post = _make_post("")
        assert chunk_post(post) == []

    def test_whitespace_only(self):
        from heddle.contrib.rag.chunker.sentence_chunker import chunk_post

        post = _make_post("   \n\n   ")
        assert chunk_post(post) == []

    def test_short_text_single_chunk(self):
        from heddle.contrib.rag.chunker.sentence_chunker import chunk_post

        post = _make_post("This is a short sentence.")
        chunks = chunk_post(post)
        assert len(chunks) == 1
        assert chunks[0].text == "This is a short sentence."
        assert chunks[0].chunk_index == 0
        assert chunks[0].total_chunks == 1

    def test_whole_post_strategy(self):
        from heddle.contrib.rag.chunker.sentence_chunker import ChunkConfig, chunk_post
        from heddle.contrib.rag.schemas.chunk import ChunkStrategy

        text = "First sentence. Second sentence. Third sentence."
        post = _make_post(text)
        cfg = ChunkConfig(strategy=ChunkStrategy.WHOLE_POST)
        chunks = chunk_post(post, config=cfg)
        assert len(chunks) == 1
        assert chunks[0].text == text

    def test_paragraph_split(self):
        from heddle.contrib.rag.chunker.sentence_chunker import ChunkConfig, chunk_post
        from heddle.contrib.rag.schemas.chunk import ChunkStrategy

        text = (
            "First paragraph with enough text to meet minimum."
            "\n\nSecond paragraph also with enough text here."
        )
        post = _make_post(text)
        cfg = ChunkConfig(strategy=ChunkStrategy.PARAGRAPH, min_chars=10)
        chunks = chunk_post(post, config=cfg)
        assert len(chunks) >= 1

    def test_chunk_ids_sequential(self):
        from heddle.contrib.rag.chunker.sentence_chunker import ChunkConfig, chunk_post

        text = "A" * 200 + ".\n\n" + "B" * 200 + ".\n\n" + "C" * 200 + "."
        post = _make_post(text, gid="100:5")
        cfg = ChunkConfig(target_chars=100, max_chars=250, min_chars=10)
        chunks = chunk_post(post, config=cfg)
        for i, c in enumerate(chunks):
            assert c.chunk_index == i
            assert c.chunk_id == f"100:5:{i}"
            assert c.total_chunks == len(chunks)

    def test_source_provenance(self):
        from heddle.contrib.rag.chunker.sentence_chunker import chunk_post

        post = _make_post("A sentence with enough chars for one chunk.", gid="999:42")
        chunks = chunk_post(post)
        assert chunks[0].source_global_id == "999:42"
        assert chunks[0].source_channel_id == 1
        assert chunks[0].source_channel_name == "test_ch"

    def test_chunk_mux_entry(self):
        from heddle.contrib.rag.chunker.sentence_chunker import chunk_mux_entry
        from heddle.contrib.rag.schemas.mux import MuxEntry

        post = _make_post("Enough text for a chunk here definitely.")
        entry = MuxEntry(mux_seq=0, post=post)
        chunks = chunk_mux_entry(entry)
        assert len(chunks) >= 1

    def test_fixed_char_strategy(self):
        from heddle.contrib.rag.chunker.sentence_chunker import ChunkConfig, chunk_post
        from heddle.contrib.rag.schemas.chunk import ChunkStrategy

        text = "X" * 1500
        post = _make_post(text)
        cfg = ChunkConfig(strategy=ChunkStrategy.FIXED_CHAR, max_chars=500, min_chars=10)
        chunks = chunk_post(post, config=cfg)
        assert len(chunks) == 3

    def test_offset_invariant_with_leading_whitespace_and_duplicates(self):
        """Pin C2: ``text[char_start:char_end] == chunk.text`` holds.

        Earlier the chunker post-stripped each segment then used
        ``text.find(seg, cumulative_offset)`` to recover offsets in
        the original text.  With leading whitespace + embedded
        duplicate phrases, ``find`` could land on the wrong
        occurrence (or fall back to ``cumulative_offset``), so chunk
        citations pointed at the wrong span.  Offsets are now tracked
        natively through split + merge, no ``.find`` round-trips.
        """
        from heddle.contrib.rag.chunker.sentence_chunker import ChunkConfig, chunk_post

        # Leading whitespace + the duplicated phrase "Hello world" in
        # two separated paragraphs.  Without offset tracking, the
        # find-based recovery would have located the wrong occurrence
        # on at least one chunk.
        text = "   Hello world. Hello world.\n\nHello world again. Hello world."
        post = _make_post(text)
        cfg = ChunkConfig(target_chars=15, max_chars=40, min_chars=5)
        chunks = chunk_post(post, config=cfg)
        assert chunks, "expected at least one chunk"
        for c in chunks:
            assert text[c.char_start : c.char_end] == c.text, (
                f"offset slice mismatch for chunk {c.chunk_index}: "
                f"slice={text[c.char_start : c.char_end]!r}, chunk_text={c.text!r}"
            )

    def test_offsets_strictly_increasing_without_overlap(self):
        """Non-overlapping mode: each chunk's start >= prior chunk's end."""
        from heddle.contrib.rag.chunker.sentence_chunker import ChunkConfig, chunk_post

        text = "First sentence here. Second sentence here.\n\nThird sentence."
        post = _make_post(text)
        cfg = ChunkConfig(target_chars=20, max_chars=40, min_chars=5, overlap_chars=0)
        chunks = chunk_post(post, config=cfg)
        from itertools import pairwise

        for prev, curr in pairwise(chunks):
            assert curr.char_start >= prev.char_end, (
                f"chunk {curr.chunk_index} starts at {curr.char_start} "
                f"but previous ended at {prev.char_end}"
            )

    def test_overlap_chars_produces_overlapping_chunks(self):
        """Pin C1: ``overlap_chars=N`` backfills N chars from each prior chunk.

        Before C1, ``overlap_chars`` was stamped onto every chunk
        but never affected segment construction — the config field
        was inert.  Now each chunk after the first starts
        ``overlap_chars`` characters earlier than it would have, so
        boundary-spanning retrieval queries see context on both
        sides of every cut.
        """
        from heddle.contrib.rag.chunker.sentence_chunker import ChunkConfig, chunk_post

        text = (
            "First sentence has enough chars. "
            "Second sentence has enough chars. "
            "Third sentence has enough chars."
        )
        post = _make_post(text)
        cfg = ChunkConfig(target_chars=20, max_chars=40, min_chars=5, overlap_chars=10)
        chunks = chunk_post(post, config=cfg)
        assert len(chunks) >= 2, "expected multiple chunks for an overlap test"

        from itertools import pairwise

        for prev, curr in pairwise(chunks):
            # Curr starts inside (or just at the start of) the prior
            # chunk — i.e. they share characters.
            assert curr.char_start < prev.char_end, (
                f"chunk {curr.chunk_index} starts at {curr.char_start} but "
                f"previous ended at {prev.char_end} — overlap not applied"
            )
            shared = prev.char_end - curr.char_start
            # The overlap is at most ``overlap_chars`` and at most
            # the prior chunk's full length (clamp).
            assert shared <= cfg.overlap_chars
            assert shared <= (prev.char_end - prev.char_start)
            # And the chunk text matches the source slice it claims.
            assert text[curr.char_start : curr.char_end] == curr.text

    def test_overlap_disabled_when_zero(self):
        """``overlap_chars=0`` short-circuits the overlap step entirely."""
        from heddle.contrib.rag.chunker.sentence_chunker import ChunkConfig, chunk_post

        text = "First. " * 50  # many short sentences
        post = _make_post(text)
        cfg_no = ChunkConfig(target_chars=20, max_chars=40, min_chars=5, overlap_chars=0)
        cfg_yes = ChunkConfig(target_chars=20, max_chars=40, min_chars=5, overlap_chars=10)
        ch_no = chunk_post(post, config=cfg_no)
        ch_yes = chunk_post(post, config=cfg_yes)
        # With overlap enabled, total spans cover more characters.
        spans_no = sum(c.char_end - c.char_start for c in ch_no)
        spans_yes = sum(c.char_end - c.char_start for c in ch_yes)
        assert spans_yes > spans_no
