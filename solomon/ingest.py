"""Document ingestion.

Picks up files from ~/.hermes/solomon/inbox/, asks the LLM (with the
solomon-ingest.md skill loaded) to extract findings, then moves the
file to archive/processed/ on success or archive/failed/ on error.

The actual LLM call goes through the adapter so we use whatever model
Hermes is configured with. In tests, we use a fake adapter that returns
scripted tool calls.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import logs, profile

SKILL_PATH = Path(__file__).parent / "skills" / "solomon-ingest.md"

# Files we know how to extract text from cheaply. Other extensions still
# get tried as plain text; if they're not UTF-8 we log an error and move on.
TEXT_EXTENSIONS = {".txt", ".md", ".eml", ".log", ".csv", ".tsv", ".html", ".htm", ".json", ".yaml", ".yml"}

# Soft cap. Documents above this get processed in chunks. Most personal
# files are well under this.
MAX_CHARS_PER_LLM_CALL = 60_000


def _load_skill_body() -> str:
    text = SKILL_PATH.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---\n", 3)
        if end != -1:
            text = text[end + 5:]
    return text.strip()


def _read_document(path: Path) -> Optional[str]:
    """Return the document's text content, or None if we can't read it."""
    ext = path.suffix.lower()
    if ext in TEXT_EXTENSIONS or ext == "":
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError) as e:
            logs.log_error("error", e, where="ingest._read_document")
            return None
    # Other types (.pdf, .docx, etc.) — we don't include heavy extractors
    # in the bare-bones build. If the file is plain text disguised as another
    # extension, fall back to a best-effort read.
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        logs.log("unsupported_filetype", file=path.name, level="WARN")
        return None


def _chunk(text: str) -> list[str]:
    """Split a long document into LLM-sized chunks at paragraph boundaries."""
    if len(text) <= MAX_CHARS_PER_LLM_CALL:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        plen = len(para) + 2
        if size + plen > MAX_CHARS_PER_LLM_CALL and buf:
            chunks.append("\n\n".join(buf))
            buf = []
            size = 0
        buf.append(para)
        size += plen
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


def _archive_dest(kind: str, file_name: str) -> Path:
    """Return archive/{kind}/<YYYY-MM-DD>/<file>. Creates the dir."""
    today = datetime.now(timezone.utc).date().isoformat()
    dest_dir = profile.home() / "archive" / kind / today
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / file_name
    if out.exists():
        # Avoid clobbering.
        stem, suffix = out.stem, out.suffix
        i = 1
        while (dest_dir / f"{stem}.{i}{suffix}").exists():
            i += 1
        out = dest_dir / f"{stem}.{i}{suffix}"
    return out


def _move_to_archive(path: Path, kind: str, error: Optional[str] = None) -> Path:
    dest = _archive_dest(kind, path.name)
    shutil.move(str(path), str(dest))
    if error and kind == "failed":
        (dest.parent / f"{dest.name}.error.txt").write_text(error, encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------


def process_file(path: Path, *, adapter: Optional[Any] = None) -> dict:
    """Process one document. Returns {ok, proposals, contradictions, error}."""
    if not path.exists() or not path.is_file():
        return {"ok": False, "proposals": 0, "contradictions": 0, "error": "not a file"}
    logs.log("inbound_processed", source_kind="document",
             source_id=path.name, action="ingest_start",
             file=str(path))
    text = _read_document(path)
    if text is None:
        _move_to_archive(path, "failed", error="unsupported filetype")
        return {"ok": False, "proposals": 0, "contradictions": 0, "error": "unsupported filetype"}

    chunks = _chunk(text)
    proposals = 0
    contradictions = 0

    if adapter is None:
        # No adapter (tests sometimes invoke without one). Treat as a no-op:
        # archive the file as processed but make no proposals.
        _move_to_archive(path, "processed")
        return {"ok": True, "proposals": 0, "contradictions": 0, "error": None}

    skill = _load_skill_body()
    for i, chunk in enumerate(chunks):
        system = (
            "You are conducting a document ingestion pass. Follow the "
            "solomon-ingest role rules.\n\n" + skill +
            f"\n\nDocument: {path.name} (chunk {i+1}/{len(chunks)})\n"
        )
        messages = [{"role": "user", "content": chunk}]
        try:
            adapter.llm_call(system=system, messages=messages,
                              json_mode=False, max_tokens=2048)
        except Exception as e:  # noqa: BLE001
            logs.log_error("error", e, where="ingest.process_file.llm_call",
                            file=path.name)
            _move_to_archive(path, "failed", error=f"LLM call failed: {e}")
            return {"ok": False, "proposals": 0, "contradictions": 0,
                     "error": f"LLM call failed: {e}"}

    # Count what landed during this run by scanning recent queue items
    # tagged with this file as source. (The LLM was instructed to put the
    # filename in `reason`.)
    items = profile.read_queue("review", status="pending", limit=200)
    proposals = sum(1 for it in items if path.name in (it.get("reason") or ""))
    contradictions = sum(1 for it in items if it.get("kind") == "contradiction"
                          and path.name in (it.get("description") or ""))

    _move_to_archive(path, "processed")
    logs.log("inbound_processed", source_kind="document",
             source_id=path.name, action="ingest_complete",
             context={"proposals": proposals, "contradictions": contradictions})
    return {"ok": True, "proposals": proposals,
             "contradictions": contradictions, "error": None}


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------


def process_all(*, adapter: Optional[Any] = None) -> dict:
    """Process every file currently in the inbox."""
    inbox = profile.home() / "inbox"
    if not inbox.exists():
        return {"ok": 0, "failed": 0, "proposals": 0}
    files = sorted([p for p in inbox.iterdir() if p.is_file()])
    ok = failed = proposals = 0
    for f in files:
        result = process_file(f, adapter=adapter)
        if result["ok"]:
            ok += 1
            proposals += result["proposals"]
        else:
            failed += 1
    return {"ok": ok, "failed": failed, "proposals": proposals}
