"""Solomon storage layer.

Public API:
  - init_storage()          — apply schema + open the connection (idempotent)
  - get_conn()              — context manager yielding a connection
  - cursor(conn)            — context manager yielding a cursor
  - execute(cur, sql, params)  — parameterised SQL (use '?' placeholders)
  - executemany(cur, sql, seq)
  - insert_returning(conn, sql, params)
  - jsonify(value), parse_json(value), row_to_dict(row)
  - backend()               — 'sqlite' (default) or 'postgres'
  - get_pool()              — back-compat shim, prefer get_conn() in new code
"""
from .pool import (  # noqa: F401
    backend,
    cursor,
    execute,
    executemany,
    get_conn,
    get_pool,
    init_storage,
    insert_returning,
    jsonify,
    parse_json,
    row_to_dict,
)
