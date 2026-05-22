"""Shared pytest fixtures for tests that touch the storage layer.

A few of the new interview tests need a real SQLite database. We point
``SOLOMON_DB_URL`` at a tmp_path-based file per test and reset the
pool module's cached backend so the second test in a session doesn't
keep talking to the first test's DB.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def solomon_db(tmp_path, monkeypatch):
    """Point Solomon at a fresh SQLite file and bootstrap the schema.

    Resets the cached backend / DSN on the pool module so the next call
    to ``backend()`` picks up the new env var.
    """
    db_path = tmp_path / "solomon.db"
    monkeypatch.setenv("SOLOMON_DB_URL", f"sqlite:///{db_path}")

    # Reset cached module state in solomon.storage.pool.
    from solomon.storage import pool
    pool._BACKEND = None
    pool._DSN = None
    pool._PG_POOL = None
    pool._SCHEMA_CACHE.clear()

    pool.init_storage()

    # Seed a default tenant — every interview row references it via FK.
    with pool.get_conn() as conn:
        with pool.cursor(conn) as cur:
            pool.execute(
                cur,
                "INSERT INTO tenants (tenant_id, business_name) "
                "VALUES (?, ?) ON CONFLICT (tenant_id) DO NOTHING",
                ("default", "Test Co"),
            )
        conn.commit()

    yield db_path

    # Teardown: reset again so the next test's monkeypatched env var sticks.
    pool._BACKEND = None
    pool._DSN = None
    pool._PG_POOL = None
