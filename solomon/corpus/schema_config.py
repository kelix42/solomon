"""Load owner config from corpus/schema.md.

See REPORT-CORPUS.md §1.11. Ported from
/root/projects/solomon-from-drive/corpus_ingest/config.py with Pinecone
constants stripped (we use ``source_table`` discriminators instead).

The corpus root is configurable via ``SOLOMON_CORPUS_ROOT`` env var; the
default is ``<repo>/corpus`` for development and
``$HERMES_HOME/solomon/corpus`` for installed deployments.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("solomon.corpus.schema_config")

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

# Repo-relative default (used by the in-tree dev workflow + tests).
_REPO_ROOT = Path(__file__).resolve().parents[2]


def corpus_root() -> Path:
    """Return the active corpus root.

    Resolution order:
      1. ``SOLOMON_CORPUS_ROOT`` env var, if set.
      2. ``$HERMES_HOME/solomon/corpus`` if HERMES_HOME is set.
      3. ``<repo>/corpus`` (the in-tree default — useful for dev + tests).
    """
    env = os.getenv("SOLOMON_CORPUS_ROOT", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    hh = os.getenv("HERMES_HOME", "").strip()
    if hh:
        return Path(os.path.expanduser(hh)) / "solomon" / "corpus"
    return _REPO_ROOT / "corpus"


def schema_path() -> Path:
    return corpus_root() / "schema.md"


# ---------------------------------------------------------------------------
# Chunk-size defaults (the Drive's sliding-window fallback)
# ---------------------------------------------------------------------------

CHUNK_SIZE_TOKENS = 800
CHUNK_OVERLAP_TOKENS = 100
CHARS_PER_TOKEN = 4  # crude approximation; tiktoken would be exact


# ---------------------------------------------------------------------------
# YAML parsing
# ---------------------------------------------------------------------------

_YAML_BLOCK_RE = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)


def _yaml_blocks(md_text: str):
    """Yield every ```yaml ... ``` block parsed."""
    try:
        import yaml  # type: ignore
    except ImportError:  # pragma: no cover — PyYAML is in core deps
        logger.warning("PyYAML missing; corpus/schema.md will be ignored.")
        return
    for match in _YAML_BLOCK_RE.finditer(md_text):
        try:
            parsed = yaml.safe_load(match.group(1))
            if isinstance(parsed, dict):
                yield parsed
        except Exception as e:  # noqa: BLE001
            logger.warning("Bad YAML block in schema.md: %s", e)
            continue


def load_schema() -> Dict[str, Any]:
    """Merge every YAML block in corpus/schema.md into one dict. Returns
    an empty dict if the file is missing or unparseable.
    """
    sp = schema_path()
    if not sp.exists():
        return {}
    try:
        text = sp.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read %s: %s", sp, e)
        return {}
    merged: Dict[str, Any] = {}
    for block in _yaml_blocks(text):
        merged.update(block)
    return merged


def routing_map() -> Dict[str, List[str]]:
    return load_schema().get("routing", {}) or {}


def file_limits() -> Dict[str, Any]:
    return load_schema().get("limits", {"max_size_bytes": 100 * 1024 * 1024})


def entity_allowlist() -> List[str]:
    return load_schema().get("entity_allowlist", []) or []


def redaction_skip_globs() -> List[str]:
    return load_schema().get("redaction_skip", []) or []


def salience_min() -> float:
    return float(load_schema().get("salience_min", 0.30))
