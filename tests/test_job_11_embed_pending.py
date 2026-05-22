"""Tests for solomon.sleep.job_11_embed_pending."""

from __future__ import annotations

import pytest

from solomon.sleep import job_11_embed_pending as J
from solomon.storage.pool import cursor, execute, get_conn


def _seed_captured(captured_id: str, statement: str = "be kind", domain: str = "principles") -> None:
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO captured_items "
                "(id, tenant_id, session_id, type, domain, statement, "
                "verbatim_phrase, example, keywords, confidence) "
                "VALUES (?, ?, NULL, 'principle', ?, ?, ?, ?, '[]', 'stated')",
                (captured_id, "default", domain, statement, statement, ""),
            )
        conn.commit()


def _count_embeddings(tenant_id: str = "default") -> int:
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT COUNT(*) FROM embeddings "
                "WHERE tenant_id=? AND source_table='captured_items'",
                (tenant_id,),
            )
            return int(cur.fetchone()[0])


def _stub_embed(monkeypatch):
    monkeypatch.setattr(
        "solomon.corpus.embed.embed_batch",
        lambda texts: [[float(len(t)), 0.1, 0.2, 0.3] for t in texts],
    )


def test_job_11_embeds_pending_captured(solomon_db, monkeypatch):
    _stub_embed(monkeypatch)
    _seed_captured("cap-1", statement="always reply within 24 hours")
    assert _count_embeddings() == 0

    result = J.run(tenant_id="default")

    assert _count_embeddings() == 1
    assert result["embedded"] == 1
    assert result["items_processed"] == 1

    # Verify the embeddings row has the expected source_id.
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT source_id FROM embeddings "
                "WHERE tenant_id=? AND source_table='captured_items'",
                ("default",),
            )
            row = cur.fetchone()
    assert row[0] == "captured:cap-1"


def test_job_11_is_idempotent(solomon_db, monkeypatch):
    _stub_embed(monkeypatch)
    _seed_captured("cap-1")
    _seed_captured("cap-2")

    J.run(tenant_id="default")
    assert _count_embeddings() == 2

    # Second run: LEFT JOIN excludes already-embedded rows, so nothing
    # new should land.
    result2 = J.run(tenant_id="default")
    assert _count_embeddings() == 2
    assert result2["embedded"] == 0
    assert result2["items_processed"] == 0


def test_job_11_respects_batch_cap(solomon_db, monkeypatch):
    _stub_embed(monkeypatch)
    for i in range(5):
        _seed_captured(f"cap-{i}")
    assert _count_embeddings() == 0

    result = J.run(tenant_id="default", batch_cap=3)

    assert _count_embeddings() == 3
    assert result["embedded"] == 3

    # Next run picks up the remaining 2.
    J.run(tenant_id="default", batch_cap=3)
    assert _count_embeddings() == 5


def test_job_11_skips_empty_text(solomon_db, monkeypatch):
    _stub_embed(monkeypatch)
    # Seed a captured row with no statement/verbatim/example.
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "INSERT INTO captured_items "
                "(id, tenant_id, type, domain, statement, verbatim_phrase, example, "
                "keywords, confidence) "
                "VALUES (?, ?, 'principle', 'x', '', '', '', '[]', 'stated')",
                ("empty-1", "default"),
            )
        conn.commit()

    result = J.run(tenant_id="default")

    assert _count_embeddings() == 0
    assert result["skipped_empty"] == 1
    assert result["embedded"] == 0


def test_job_11_marks_embedded_at(solomon_db, monkeypatch):
    _stub_embed(monkeypatch)
    _seed_captured("cap-1")

    # Confirm embedded_at is NULL pre-run.
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT embedded_at FROM captured_items WHERE id=?", ("cap-1",))
            assert cur.fetchone()[0] is None

    J.run(tenant_id="default")

    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(cur, "SELECT embedded_at FROM captured_items WHERE id=?", ("cap-1",))
            assert cur.fetchone()[0] is not None
