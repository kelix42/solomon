# Corpus

Solomon's bulk knowledge layer, adapted from Karpathy's LLM-Wiki pattern.
See `docs/REPORT-CORPUS.md` §1 for the design narrative and §4 for the
integration plan.

## Three-layer pattern

- **`inbox/`** — drop zone. Subfolder names act as routing hints
  (`sops/`, `emails/`, `messages/`, `docs/`, `data/`). The
  `solomon.workers.corpus_inbox_watcher` worker picks up new files and
  triggers ingest within ~30 seconds.
- **`raw/`** — immutable, redacted copies of the original files. The LLM
  reads these but never edits them.
- **`wiki/`** — LLM-maintained synthesized pages: `entities/`, `concepts/`,
  `playbooks/`. Built by `solomon.corpus.llm_passes` via the two-pass
  Karpathy pattern.
- **`schema.md`** — owner-editable configuration (routing, limits,
  redaction allowlists, transcription/OCR backends).

## What gets embedded

All four logical "namespaces" live in the same `embeddings` table with a
`source_table` discriminator and per-namespace weights applied at
retrieval time (`solomon.corpus.NAMESPACE_WEIGHTS`):

- Wiki pages → `source_table='corpus_wiki'` (weight 0.40 — highest)
- Captured items → `source_table='captured_items'` (0.30)
- Raw chunks → `source_table='corpus_raw'` (0.20)
- Decisions → `source_table='decisions'` (0.10)

## Forgetting

`solomon.corpus.forget` runs the deterministic cascade: hard-delete entity
pages, LLM-rewrite concept/playbook pages that mention the forgotten
entity, quarantine raw files to `_forgotten/`, and remove the matching
embedding rows.
