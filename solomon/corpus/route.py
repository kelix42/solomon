"""Route an inbox file to a corpus category.

See REPORT-CORPUS.md §1.2 + §4. Ported from
/root/projects/solomon-from-drive/corpus_ingest/route.py.

Three tiers:
  1. Subfolder hint:  corpus/inbox/<category>/...  -> <category>
  2. Extension map (from schema.md)
  3. Plain-text fallback to 'docs' for .txt/.md/.rtf/.htm

The LLM classifier third tier from the Drive plan is deferred (our
``solomon.ingestion.classifier`` already does the classify-by-LLM pass
once content has been extracted).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .schema_config import corpus_root, routing_map

logger = logging.getLogger("solomon.corpus.route")

VALID_CATEGORIES = {"sops", "emails", "messages", "docs", "data"}
SUBFOLDER_HINTS = {"sops", "emails", "messages", "docs", "data"}

# Extensions that fall back to 'docs' when schema.md doesn't route them.
TEXT_EXTENSIONS = {".txt", ".md", ".rtf", ".htm"}


def category_from_subfolder(path: Path) -> Optional[str]:
    """If the file lives under corpus/inbox/<hint>/..., return <hint>."""
    inbox = corpus_root() / "inbox"
    try:
        rel = path.resolve().relative_to(inbox.resolve())
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if parts and parts[0] in SUBFOLDER_HINTS:
        return parts[0]
    return None


def category_from_extension(path: Path) -> Optional[str]:
    """Walk the schema.md routing map for a matching extension."""
    suffix = path.suffix.lower()
    try:
        rmap = routing_map()
    except Exception as e:  # noqa: BLE001
        logger.warning("routing_map() failed: %s", e)
        rmap = {}
    for category, exts in rmap.items():
        if category == "llm_classifier":
            continue
        if not isinstance(exts, (list, tuple)):
            continue
        if suffix in (str(e).lower() for e in exts):
            return category
    return None


def route(path: Path) -> Optional[str]:
    """Return one of VALID_CATEGORIES or None if unrouted."""
    cat = category_from_subfolder(path)
    if cat:
        return cat
    cat = category_from_extension(path)
    if cat in VALID_CATEGORIES:
        return cat
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return "docs"
    return None
