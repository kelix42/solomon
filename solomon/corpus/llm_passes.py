"""Karpathy two-pass LLM workflow.

Per REPORT-CORPUS.md §1.3 and §4.5 Phase B:

  Pass 1 — Extract: one ``tier='deep'`` LLM call per file. Returns the
    JSON envelope {summary, entities, concepts, playbooks, proposed_rules}.

  Pass 2 — Page merge: for each entity/concept/playbook with non-empty
    ``new_info``, read the current wiki page, ask the LLM to merge the
    new content into the canonical section structure, return the full
    updated markdown.

Ported from /root/projects/solomon-from-drive/corpus_ingest/llm_passes.py
with three substitutions:

  - Anthropic SDK -> ``solomon.corpus.llm.call`` (delegates to
    ``solomon.reasoning.llm.get_client`` so the provider is config-driven).
  - SOLOMON_ROOT -> ``solomon.corpus.schema_config.corpus_root`` for
    wiki page paths (parent of corpus/wiki/...).
  - pinecone upsert -> ``solomon.corpus.wiki.embed_and_upsert_page``,
    landed in a sibling module.

This module owns NO storage writes itself — Pass 1 returns the parsed
envelope, Pass 2 writes the wiki page on disk and calls the wiki helper
for vector cleanup. The orchestrator wires the result of Pass 1 into
``rules.py`` for the proposed_rules + mentoring_queue side-table writes.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from .llm import call as llm_call
from .prompts import (
    EXTRACT_SYSTEM,
    EXTRACT_USER_TEMPLATE,
    PAGE_MERGE_SYSTEM,
    PAGE_MERGE_USER_TEMPLATE,
)
from .schema_config import corpus_root

logger = logging.getLogger("solomon.corpus.llm_passes")

# How much of the raw text we send to the extract pass. Long-context
# models can ingest more, but staying near 60k chars keeps cost predictable.
EXTRACT_CHAR_BUDGET = 60_000

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]+?)```", re.MULTILINE)
_MD_FENCE_RE = re.compile(r"```(?:markdown|md)?\s*([\s\S]+?)```", re.MULTILINE)


# ---------------------------------------------------------------------------
# JSON / markdown fence stripping
# ---------------------------------------------------------------------------


def parse_envelope(text: str) -> Dict[str, Any]:
    """Tolerant parse of the Pass 1 JSON envelope.

    - Strips ``json`` / plain code fences if present.
    - Returns ``{}`` on a malformed payload.
    - Always returns a dict with the four list keys present and ``summary``
      as a string, so callers don't have to defensive-set on every read.
    """
    if not text:
        return {}
    s = text.strip()
    if s.startswith("```"):
        m = _JSON_FENCE_RE.search(s)
        if m:
            s = m.group(1).strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        logger.warning("extract pass returned invalid JSON: %s", e)
        return {}
    if not isinstance(data, dict):
        return {}
    data.setdefault("summary", "")
    for k in ("entities", "concepts", "playbooks", "proposed_rules"):
        if not isinstance(data.get(k), list):
            data[k] = []
    return data


def strip_md_fence(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        m = _MD_FENCE_RE.search(s)
        if m:
            return m.group(1).strip()
    return s


# ---------------------------------------------------------------------------
# Pass 1
# ---------------------------------------------------------------------------


def extract(text: str, *, category: str, raw_path: str) -> Dict[str, Any]:
    """Pass 1: one deep-tier LLM call. Returns the parsed JSON envelope."""
    user = EXTRACT_USER_TEMPLATE.format(
        category=category,
        raw_path=raw_path,
        text=text[:EXTRACT_CHAR_BUDGET],
    )
    response = llm_call(
        system=EXTRACT_SYSTEM,
        user=user,
        max_tokens=4096,
        tier="deep",
        json_mode=True,
    )
    return parse_envelope(response)


# ---------------------------------------------------------------------------
# Pass 2 helpers
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slug_safe(s: str) -> str:
    s = _SLUG_RE.sub("-", (s or "").lower()).strip("-")
    return s or "untitled"


def _wiki_page_path(page_type: str, slug: str) -> Path:
    bucket = {"entity": "entities", "concept": "concepts", "playbook": "playbooks"}[page_type]
    return corpus_root() / "wiki" / bucket / f"{slug}.md"


def _read_page(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""


def _write_page(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Pass 2
# ---------------------------------------------------------------------------


def merge_pages(
    *,
    envelope: Dict[str, Any],
    raw_path_rel: str,
    upsert_fn: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """For each entity/concept/playbook with non-empty new_info:

      1. Read the existing wiki page (if any).
      2. Call the LLM to produce the merged page markdown.
      3. Write the page to disk.
      4. Call ``upsert_fn(page_type, slug, page_path_rel, content)`` to
         section-hash + cleanup orphans + embed new sections.

    ``upsert_fn`` defaults to ``solomon.corpus.wiki.embed_and_upsert_page``
    when omitted; tests inject a stub.

    Returns one dict per touched page:
        {"page_type": ..., "slug": ..., "page_path": <relative>,
         "vectors_changed": <int>}
    """
    if upsert_fn is None:
        # Lazy import to avoid a circular dep at module load.
        from . import wiki as wiki_mod
        upsert_fn = wiki_mod.embed_and_upsert_page

    today = date.today().isoformat()
    results: List[Dict[str, Any]] = []
    root = corpus_root().parent  # path that holds "corpus/" — i.e. repo or solomon home

    for kind in ("entities", "concepts", "playbooks"):
        page_type = {"entities": "entity", "concepts": "concept", "playbooks": "playbook"}[kind]
        for item in envelope.get(kind, []) or []:
            new_info = (item.get("new_info") or "").strip()
            if not new_info:
                continue
            slug = slug_safe(item.get("slug") or "")
            if not slug or slug == "untitled":
                continue

            path = _wiki_page_path(page_type, slug)
            existing = _read_page(path)

            try:
                relative = str(path.relative_to(root))
            except ValueError:
                relative = str(path)

            user = PAGE_MERGE_USER_TEMPLATE.format(
                slug=slug,
                page_type=page_type,
                page_path=relative,
                today=today,
                raw_path=raw_path_rel,
                existing=existing or "(empty — create from scratch)",
                new_info=new_info,
            )
            try:
                merged_raw = llm_call(
                    system=PAGE_MERGE_SYSTEM,
                    user=user,
                    max_tokens=4096,
                    tier="deep",
                )
            except Exception:  # noqa: BLE001
                logger.exception("page merge LLM call failed for %s", relative)
                continue
            new_md = strip_md_fence(merged_raw)
            if not new_md.strip():
                logger.warning("page merge returned empty content for %s", relative)
                continue

            _write_page(path, new_md)

            try:
                vectors_changed = upsert_fn(
                    page_type=page_type,
                    slug=slug,
                    page_path_str=relative,
                    new_content=new_md,
                )
                # upsert_fn may return an int or a (int, list) tuple — accept either.
                if isinstance(vectors_changed, tuple):
                    vectors_changed = vectors_changed[0]
            except Exception:  # noqa: BLE001
                logger.exception("wiki upsert failed for %s", relative)
                vectors_changed = 0

            results.append({
                "page_type": page_type,
                "slug": slug,
                "page_path": relative,
                "vectors_changed": int(vectors_changed or 0),
            })
    return results


def append_index(touched: List[Dict[str, Any]]) -> None:
    """Append touched-page paths to corpus/index.md (one line per page).

    Cheapest possible idempotency: skip lines that already appear in the
    file. Index gives the owner a single place to scan recent activity.
    """
    if not touched:
        return
    idx = corpus_root() / "index.md"
    idx.parent.mkdir(parents=True, exist_ok=True)
    existing = idx.read_text(encoding="utf-8") if idx.exists() else ""
    new_lines: List[str] = []
    for t in touched:
        line = f"- [{t['slug']}]({t['page_path']}) ({t['page_type']})"
        if line not in existing and line not in "\n".join(new_lines):
            new_lines.append(line)
    if not new_lines:
        return
    with idx.open("a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("\n".join(new_lines) + "\n")
