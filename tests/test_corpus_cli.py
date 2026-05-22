"""Tests for solomon corpus * CLI subcommands."""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from solomon import cli as solomon_cli


def _run(argv, monkeypatch=None):
    """Invoke the CLI and capture stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = solomon_cli.main(argv)
    return rc, buf.getvalue()


def test_corpus_unknown_subcommand_fails(solomon_db):
    rc, out = _run(["corpus", "bogus"])
    assert rc == 1
    assert "Unknown subcommand" in out


def test_corpus_no_args_prints_usage(solomon_db):
    rc, out = _run(["corpus"])
    assert rc == 1
    assert "Usage" in out


def test_corpus_stats_runs(solomon_db):
    rc, out = _run(["corpus", "stats"])
    assert rc == 0
    assert "Corpus stats" in out
    assert "files:" in out
    assert "embeddings:" in out
    assert "proposed_rules queued: 0" in out


def test_corpus_lint_clean(solomon_db, monkeypatch, tmp_path):
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    rc, out = _run(["corpus", "lint"])
    assert rc == 0
    assert "0 findings" in out


def test_corpus_forget_no_args(solomon_db):
    rc, out = _run(["corpus", "forget"])
    assert rc == 1
    assert "Usage" in out


def test_corpus_forget_not_found(solomon_db):
    rc, out = _run(["corpus", "forget", "--sha", "deadbeef" * 8])
    assert rc == 1
    assert "not found" in out.lower()


def test_corpus_ingest_no_paths(solomon_db):
    rc, out = _run(["corpus", "ingest"])
    assert rc == 1
    assert "Usage" in out


def test_corpus_ingest_end_to_end(solomon_db, monkeypatch, tmp_path):
    """Drive the full pipeline through the CLI with the LLM + embed stubbed."""
    monkeypatch.setenv("SOLOMON_CORPUS_ROOT", str(tmp_path / "corpus"))
    inbox = tmp_path / "corpus" / "inbox" / "docs"
    inbox.mkdir(parents=True)
    src = inbox / "p.txt"
    src.write_text("some policy body content that is long enough to chunk and embed.")

    # Stub embeddings + LLM.
    monkeypatch.setattr(
        "solomon.corpus.embed.embed_batch",
        lambda texts: [[float(len(t)), 0.1, 0.2, 0.3] for t in texts],
    )

    import solomon.reasoning.llm as solomon_llm
    class _Stub:
        configured = True
        def model_for(self, t): return "stub"
        def call(self, *, tier, system, user, json_mode=False, max_tokens=1024, temperature=0.2):
            import json as _json
            if "corpus-ingest analyst" in system:
                return solomon_llm.LLMResponse(
                    text=_json.dumps({
                        "summary": "x", "entities": [], "concepts": [],
                        "playbooks": [], "proposed_rules": [],
                    }),
                    model="stub",
                )
            return solomon_llm.LLMResponse(text="", model="stub")
    monkeypatch.setattr(solomon_llm, "_client", _Stub())

    rc, out = _run(["corpus", "ingest", str(src)])
    assert rc == 0
    # Rich treats [success] as markup so it's stripped from output; check the
    # summary line and the per-file mention instead.
    assert "success=1" in out
    assert "p.txt" in out


def test_main_dispatches_corpus(solomon_db):
    """Sanity check that 'solomon corpus stats' reaches cmd_corpus."""
    rc, out = _run(["corpus", "stats"])
    assert rc == 0
