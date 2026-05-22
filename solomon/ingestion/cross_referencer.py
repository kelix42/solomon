"""Part 26 Stage 6 — cross-reference linker.

Finds documents that reference each other so the rest of the ingestion
pipeline (and later retrieval) can treat them as a connected unit. The
canonical example: an email thread split across several raw documents,
or contract_v1.pdf -> contract_v2.pdf -> contract_v3.pdf.

Phase 1 deliberately uses only regex and string matching. No LLM call.
Stage 6 is cheap and runs over every ingested document, so spending a
model call here would blow the per-tenant ingestion budget without
proportional benefit. A future phase can add an LLM-based pass for
ambiguous cases (e.g. "as I mentioned last week...") behind a feature
flag.

References are emitted as (source_doc_id, target_doc_id, reason) tuples
and stored via `store_references`. There is no `document_references`
table in the schema yet (Part 26 hasn't landed that migration), so for
Phase 1 we just log them. Once the table exists, swap the body of
`store_references` for an INSERT.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Dict, List, Tuple

logger = logging.getLogger("solomon.ingestion.crossref")

# Strip these reply/forward markers when normalizing email subjects so
# "Re: Re: Fwd: Q3 plan" and "Q3 plan" collapse to the same key.
_SUBJECT_PREFIX_RE = re.compile(r"^\s*(re|fwd|fw)\s*:\s*", re.IGNORECASE)

# Two filenames count as "the same document" if their normalized prefixes
# (lowercased, version/extension stripped) share at least this much.
_FILENAME_SIMILARITY_THRESHOLD = 0.80


def _normalize_subject(subject: str) -> str:
    """Strip all leading Re:/Fwd: prefixes and surrounding whitespace.

    Repeats until no prefix remains, so "Re: Fwd: Re: foo" -> "foo".
    """
    if not subject:
        return ""
    s = subject.strip()
    while True:
        new = _SUBJECT_PREFIX_RE.sub("", s, count=1)
        if new == s:
            break
        s = new
    return s.lower().strip()


def _filename_root(filename: str) -> str:
    """Reduce a filename to a comparable root.

    Drops the extension and any trailing _v1 / -v2 / .final / (1) style
    version markers so contract_v1.pdf and contract_v2_final.pdf compare
    on "contract".
    """
    if not filename:
        return ""
    name = filename.lower().strip()
    # Strip path
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if "\\" in name:
        name = name.rsplit("\\", 1)[-1]
    # Strip extension
    if "." in name:
        name = name.rsplit(".", 1)[0]
    # Strip common version suffixes repeatedly.
    suffix_re = re.compile(
        r"([_\-\s]*(v\d+|version\d+|final|draft|copy|\(\d+\)))+$",
        re.IGNORECASE,
    )
    name = suffix_re.sub("", name).strip(" _-")
    return name


def _filename_similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    ratio = SequenceMatcher(None, a, b).ratio()
    return ratio >= _FILENAME_SIMILARITY_THRESHOLD


def _doc_id(doc: Dict) -> int:
    """Return the integer id of a document dict, tolerating either
    `document_id` or `id`. Docs without an id are skipped upstream."""
    return int(doc.get("document_id") or doc.get("id"))


def _channel_meta(doc: Dict) -> Dict:
    meta = doc.get("channel_metadata") or {}
    if isinstance(meta, dict):
        return meta
    return {}


def find_references(
    documents: List[Dict],
) -> List[Tuple[int, int, str]]:
    """Find inter-document references inside a batch.

    Rules applied (in order):
      1. Email subject continuation: same normalized subject after
         stripping Re:/Fwd:/FW:/RE: prefixes.
      2. Same `thread_id` in channel_metadata (Slack/Teams threads,
         Gmail thread ids, etc.).
      3. Filename similarity >= 80% on the normalized root, e.g.
         contract_v1.pdf -> contract_v2.pdf.

    Returns a list of (source_doc_id, target_doc_id, reason) tuples.
    Both directions are emitted (a->b and b->a) so downstream code
    doesn't have to symmetrize. Self-references are never emitted.
    The result is deduplicated on (source, target, reason).
    """
    refs: List[Tuple[int, int, str]] = []
    seen: set = set()

    def _emit(src: int, tgt: int, reason: str) -> None:
        if src == tgt:
            return
        key = (src, tgt, reason)
        if key in seen:
            return
        seen.add(key)
        refs.append(key)

    # Bucket by normalized subject and by thread_id in one pass.
    subject_buckets: Dict[str, List[int]] = {}
    thread_buckets: Dict[str, List[int]] = {}
    file_entries: List[Tuple[int, str]] = []

    for doc in documents:
        try:
            did = _doc_id(doc)
        except (TypeError, ValueError):
            continue

        meta = _channel_meta(doc)

        subject = meta.get("subject") or doc.get("subject") or ""
        norm_subj = _normalize_subject(str(subject))
        if norm_subj:
            subject_buckets.setdefault(norm_subj, []).append(did)

        thread_id = meta.get("thread_id")
        if thread_id:
            thread_buckets.setdefault(str(thread_id), []).append(did)

        filename = (
            meta.get("filename")
            or doc.get("filename")
            or doc.get("storage_path")
            or ""
        )
        root = _filename_root(str(filename))
        if root:
            file_entries.append((did, root))

    # Rule 1: subject continuation.
    for subj, ids in subject_buckets.items():
        if len(ids) < 2:
            continue
        reason = f"subject_continuation:{subj[:60]}"
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                _emit(a, b, reason)
                _emit(b, a, reason)

    # Rule 2: shared thread_id.
    for thread_id, ids in thread_buckets.items():
        if len(ids) < 2:
            continue
        reason = f"thread_id:{thread_id}"
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                _emit(a, b, reason)
                _emit(b, a, reason)

    # Rule 3: filename similarity. O(n^2) but n is a single ingestion
    # batch (rarely > a few hundred), so this is fine.
    for i in range(len(file_entries)):
        did_a, root_a = file_entries[i]
        for j in range(i + 1, len(file_entries)):
            did_b, root_b = file_entries[j]
            if _filename_similar(root_a, root_b):
                reason = f"filename_similarity:{root_a}~{root_b}"
                _emit(did_a, did_b, reason)
                _emit(did_b, did_a, reason)

    logger.info(
        "cross_referencer: found %d references across %d documents",
        len(refs),
        len(documents),
    )
    return refs


def store_references(
    tenant_id: str,
    references: List[Tuple[int, int, str]],
) -> int:
    """Persist cross-references.

    Phase 1: no `document_references` table exists yet, so this just
    logs the references at INFO level and returns the count. Once the
    migration lands, replace the body with a batched INSERT.

    TODO(part-26): add `document_references` table (source_doc_id,
    target_doc_id, reason, tenant_id, created_at) and INSERT here.
    """
    if not references:
        return 0

    # TODO(part-26): batched INSERT into document_references once the
    # migration lands. For now, log so we can audit what *would* have
    # been stored.
    for src, tgt, reason in references:
        logger.info(
            "crossref tenant=%s %d -> %d (%s)",
            tenant_id,
            src,
            tgt,
            reason,
        )
    logger.info(
        "store_references: tenant=%s logged %d references (no table yet)",
        tenant_id,
        len(references),
    )
    return len(references)
