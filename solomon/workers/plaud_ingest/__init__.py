"""Plaud voice-recording ingest worker — IMAP IDLE listener.

REPORT-CORPUS.md §1.8: Plaud emails .txt transcripts via AutoFlow. The
worker runs an IMAP IDLE listener (instant push) plus a 60s backup
poller (catches anything IDLE misses), saves attachments to
``corpus/inbox/messages/``, and the corpus_inbox_watcher picks them up
through the normal pipeline.

THIS IS A STUB for the current build. Item 12 in the build plan
explicitly allows it to be deferred. We ship:

  * The persistent state helpers (``load_state`` / ``save_state``)
    backed by the existing ``plaud_state`` table — tested.
  * The configuration shape (``PlaudConfig`` dataclass) — tested.
  * The ``save_attachment`` helper that writes a Plaud-style .txt
    attachment to ``corpus/inbox/messages/`` — tested.
  * A ``main()`` entry point that errors with a clear message until
    the IMAP wiring lands; the corpus_inbox_watcher still does its
    job on whatever the user manually drops in.

When the real wiring lands, the IMAP IDLE thread + backup poller
slot in here without changing the rest of the system.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...corpus.schema_config import corpus_root
from ...storage.pool import cursor, execute, get_conn, jsonify, parse_json

logger = logging.getLogger("solomon.workers.plaud_ingest")

PLAUD_DEFAULT_FROM = os.getenv("SOLOMON_PLAUD_FROM", "autoflow@plaud.ai")
PLAUD_MAILBOX = os.getenv("SOLOMON_PLAUD_MAILBOX", "INBOX")
PLAUD_RECENT_BUFFER_MAX = 2000


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class PlaudState:
    last_seen_uid: Optional[int] = None
    recent_email_ids: List[str] = field(default_factory=list)
    last_idle_at: Optional[str] = None
    last_poll_at: Optional[str] = None
    consecutive_fails: int = 0


def _default_tenant() -> str:
    return os.getenv("SOLOMON_TENANT_ID", "default")


def load_state(*, tenant_id: Optional[str] = None) -> PlaudState:
    """Read the singleton row, returning a default-state dataclass when absent."""
    tid = tenant_id or _default_tenant()
    with get_conn() as conn:
        with cursor(conn) as cur:
            execute(
                cur,
                "SELECT last_seen_uid, recent_email_ids, last_idle_at, "
                "       last_poll_at, consecutive_fails "
                "FROM plaud_state WHERE tenant_id = ? LIMIT 1",
                (tid,),
            )
            row = cur.fetchone()
    if not row:
        return PlaudState()
    return PlaudState(
        last_seen_uid=row[0],
        recent_email_ids=parse_json(row[1]) or [],
        last_idle_at=row[2],
        last_poll_at=row[3],
        consecutive_fails=int(row[4] or 0),
    )


def save_state(state: PlaudState, *, tenant_id: Optional[str] = None) -> None:
    """Upsert the singleton plaud_state row."""
    tid = tenant_id or _default_tenant()
    recent = state.recent_email_ids[-PLAUD_RECENT_BUFFER_MAX:]
    with get_conn() as conn:
        with cursor(conn) as cur:
            # delete-then-insert is portable; the schema's PK is a constant 1
            # so we substitute tenant_id for the lookup key.
            execute(cur, "DELETE FROM plaud_state WHERE tenant_id = ?", (tid,))
            execute(
                cur,
                "INSERT INTO plaud_state "
                "(tenant_id, last_seen_uid, recent_email_ids, last_idle_at, "
                " last_poll_at, consecutive_fails) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    tid,
                    state.last_seen_uid,
                    jsonify(recent),
                    state.last_idle_at,
                    state.last_poll_at,
                    state.consecutive_fails,
                ),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class PlaudConfig:
    imap_host: str
    imap_user: str
    imap_password: str
    imap_port: int = 993
    imap_use_ssl: bool = True
    mailbox: str = PLAUD_MAILBOX
    from_address: str = PLAUD_DEFAULT_FROM

    @classmethod
    def from_env(cls) -> Optional["PlaudConfig"]:
        host = os.getenv("SOLOMON_PLAUD_IMAP_HOST", "").strip()
        user = os.getenv("SOLOMON_PLAUD_IMAP_USER", "").strip()
        password = os.getenv("SOLOMON_PLAUD_IMAP_PASSWORD", "").strip()
        if not (host and user and password):
            return None
        return cls(
            imap_host=host,
            imap_user=user,
            imap_password=password,
            imap_port=int(os.getenv("SOLOMON_PLAUD_IMAP_PORT", "993") or 993),
            imap_use_ssl=os.getenv("SOLOMON_PLAUD_IMAP_SSL", "1") != "0",
            mailbox=os.getenv("SOLOMON_PLAUD_MAILBOX", PLAUD_MAILBOX),
            from_address=os.getenv("SOLOMON_PLAUD_FROM", PLAUD_DEFAULT_FROM),
        )


# ---------------------------------------------------------------------------
# Attachment writing
# ---------------------------------------------------------------------------


_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DOTRUN_RE = re.compile(r"\.{2,}")


def _safe_filename(name: str) -> str:
    cleaned = _FILENAME_RE.sub("-", name).strip("-")
    cleaned = _DOTRUN_RE.sub("-", cleaned)  # block path traversal patterns
    return cleaned or "attachment.txt"


def save_attachment(
    *,
    content: str,
    original_filename: str,
    received_at: Optional[datetime] = None,
    inbox_root: Optional[Path] = None,
) -> Path:
    """Write the .txt attachment into corpus/inbox/messages/ with an
    ISO-timestamp prefix. Returns the absolute path written.
    """
    when = received_at or datetime.utcnow()
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    safe = _safe_filename(original_filename) or "transcript.txt"
    if not safe.lower().endswith(".txt"):
        safe = f"{safe}.txt"
    root = inbox_root or (corpus_root() / "inbox")
    target_dir = root / "messages"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{stamp}-{safe}"
    target.write_text(content, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Entry point (stub)
# ---------------------------------------------------------------------------


def main() -> int:
    """Stub entry point.

    Refuses to start until real IMAP wiring lands. Inbox watcher still
    processes anything dropped manually under ``corpus/inbox/messages/``.
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = PlaudConfig.from_env()
    if cfg is None:
        logger.error(
            "Plaud IMAP credentials not set. Configure "
            "SOLOMON_PLAUD_IMAP_HOST / USER / PASSWORD then re-enable this worker."
        )
        return 1
    logger.warning(
        "plaud_ingest worker is a STUB in this build. "
        "Drop transcripts manually into corpus/inbox/messages/ until the "
        "IMAP IDLE listener lands. Real implementation deferred per "
        "BUILD-STATE.md item 12."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
