"""Tests for `solomon mentoring ...` CLI subcommands.

Same pattern as tests/test_corpus_cli.py: drive solomon.cli.main()
with argv lists, capture stdout, then assert on plain text. Rich
strips `[label]` markup so assertions look for content, not bracketed
labels.

Stdin for the interactive review is stubbed by monkeypatching
solomon.mentoring.review.run_review's ``input_fn=`` indirectly — the
public ``main()`` shells into ``run_review()`` with real ``input``, so
for CLI tests we monkeypatch ``builtins.input``.
"""

from __future__ import annotations

import builtins
import io
from contextlib import redirect_stdout
from typing import List

import pytest

from solomon import cli as solomon_cli
from solomon.mentoring import review as mr
from solomon.storage.pool import cursor, execute, get_conn, jsonify


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = solomon_cli.main(argv)
    return rc, buf.getvalue()


def _scripted_input(answers):
    it = iter(answers)

    def _inner(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError("scripted input exhausted")

    return _inner


def _seed_rule_for_cli(rule_id: str, statement: str = "Quote in CAD."):
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO proposed_rules "
                "(id, tenant_id, domain, proposed_statement, "
                " verbatim_excerpt, source_path, keywords, "
                " confidence_hint, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                (
                    rule_id, "default", "pricing", statement,
                    "We quote in CAD.", "corpus/inbox/p.txt",
                    jsonify([]), "stated",
                    "2026-05-23T10:00:00Z",
                ),
            )
            execute(
                cur,
                "INSERT INTO mentoring_queue "
                "(tenant_id, source, surfaced_at, status, priority, payload) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (
                    "default", mr.CORPUS_RULE_SOURCE,
                    "2026-05-23T10:00:00Z", 4,
                    jsonify({"proposed_rule_id": rule_id}),
                ),
            )
        conn.commit()


def test_mentoring_no_args_prints_usage(solomon_db):
    rc, out = _run(["mentoring"])
    assert rc == 1
    assert "Usage" in out


def test_mentoring_unknown_subcommand_fails(solomon_db):
    rc, out = _run(["mentoring", "bogus"])
    assert rc == 1
    assert "Unknown subcommand" in out


def test_mentoring_review_inbox_zero(solomon_db):
    rc, out = _run(["mentoring", "review"])
    assert rc == 0
    assert "Inbox zero" in out


def test_mentoring_review_approve_through_cli(solomon_db, monkeypatch):
    _seed_rule_for_cli("cli_r1", statement="Net 30 is the default.")
    monkeypatch.setattr(builtins, "input", _scripted_input(["a"]))
    rc, out = _run(["mentoring", "review"])
    assert rc == 0
    assert "approved" in out.lower()
    # Verify side-effect: heuristic exists now.
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT COUNT(*) FROM heuristics WHERE tenant_id = ? AND source = ?",
                ("default", mr.HEURISTIC_SOURCE),
            )
            count = cur.fetchone()[0]
    assert count == 1


def test_mentoring_review_quit_through_cli(solomon_db, monkeypatch):
    _seed_rule_for_cli("cli_q1")
    monkeypatch.setattr(builtins, "input", _scripted_input(["q"]))
    rc, out = _run(["mentoring", "review"])
    assert rc == 0
    assert "Quit" in out or "quit" in out.lower()
    # The rule row is still queued.
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT status FROM proposed_rules WHERE id = ?",
                ("cli_q1",),
            )
            status = cur.fetchone()[0]
    assert status == "queued"
