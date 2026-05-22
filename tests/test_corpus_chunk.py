"""Tests for solomon.corpus.chunk."""

from __future__ import annotations

from solomon.corpus import chunk as cc
from solomon.corpus.chunk import Chunk, chunk, sliding_window


def test_empty_text_yields_no_chunks():
    assert chunk("", "other") == []
    assert chunk("   \n  ", "other") == []


def test_sliding_window_simple():
    text = "abc def ghi " * 1000  # 12000 chars
    chunks = sliding_window(text, chunk_size_tokens=200, overlap_tokens=50)
    # 200 tokens = 800 chars; step 150 tokens = 600 chars. 12000/600 = 20.
    assert len(chunks) >= 15
    # Step is 600 chars so consecutive chunks should start at different offsets.
    assert chunks[0].char_offsets[0] != chunks[1].char_offsets[0]
    for ch in chunks:
        assert isinstance(ch, Chunk)
        assert ch.char_offsets[1] > ch.char_offsets[0]
        assert ch.source_section is None
        assert ch.metadata.get("kind") == "sliding_window"


def test_sliding_window_skips_tiny_tail():
    # 70-char text — under the 50-char minimum after the first step.
    text = "x" * 40
    out = sliding_window(text, chunk_size_tokens=10, overlap_tokens=2)
    # 40 chars > 50? No — actually 40 < 50, so we skip entirely.
    assert out == []


def test_sliding_window_offsets_align_with_source():
    text = "Hello world. " * 200  # 2600 chars
    chunks = sliding_window(text, chunk_size_tokens=100, overlap_tokens=20)
    for ch in chunks:
        s, e = ch.char_offsets
        # The substring at the recorded offsets must contain the chunk text.
        assert text[s:e] == ch.text


def test_chunk_falls_back_to_sliding_for_unknown_type():
    text = "para one.\n\npara two.\n\n" + ("padding text " * 200)
    out = chunk(text, "other")
    assert out, "expected non-empty chunk list"
    assert all(ch.metadata.get("kind") == "sliding_window" for ch in out)


def test_chunk_uses_type_aware_for_transcript():
    # Each turn must exceed the chunker's 200-char short-turn threshold so
    # speakers survive the merge step rather than collapsing to 'transcript_turn'.
    long_a = "opening statement, " * 30  # ~600 chars
    long_b = "rebuttal goes here, " * 30
    text = f"Alice: {long_a}\nBob: {long_b}\n"
    out = chunk(text, "transcript")
    assert out
    speakers = {ch.source_section for ch in out}
    # At least one chunk should carry a real speaker name in source_section.
    assert "Alice" in speakers or "Bob" in speakers


def test_chunk_uses_type_aware_for_sop():
    text = (
        "# Onboarding SOP\n\n"
        "## Day 1\n\nGreet the new hire. Set up accounts. Walk through the docs.\n\n"
        "## Day 2\n\nPair them with their mentor. Assign first ticket. Review at EOD.\n\n"
        "## Week 2\n\nFirst real ship. Code review. Postmortem if anything went sideways.\n"
    )
    out = chunk(text, "sop")
    assert out
    headings = [ch.source_section for ch in out if ch.source_section]
    # At least one heading should land in source_section.
    assert any("Day 1" in (h or "") or "Day 2" in (h or "") or "Week 2" in (h or "") for h in headings)


def test_chunk_email_thread_returns_messages():
    text = (
        "Hey team, picking up the thread.\n"
        "On Mon, Jan 1 2026 at 09:00 Alice wrote:\n"
        "  Started the work. Should be done EOW.\n"
        "From: bob@example.com\n"
        "  Sounds great. Let me know.\n"
    )
    out = chunk(text, "email_thread")
    assert out
    assert all(isinstance(ch.char_offsets, tuple) and len(ch.char_offsets) == 2 for ch in out)


def test_chunks_have_monotonic_seq():
    text = ("para one. " * 100) + "\n\n" + ("para two. " * 100)
    out = chunk(text, "other")
    seqs = [ch.seq for ch in out]
    assert seqs == sorted(seqs)
    assert seqs == list(range(len(out)))
