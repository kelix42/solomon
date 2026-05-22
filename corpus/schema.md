# Corpus Schema

Owner-editable configuration for Solomon's corpus subsystem. Ported from the
Drive build (see `REPORT-CORPUS.md` §1.11). `solomon.corpus.schema_config`
parses the YAML code blocks from this file on every ingest run.

## Routing extension map

First-tier routing rule is **subfolder hint**: if the inbox path starts with
`sops/`, `emails/`, `messages/`, `docs/`, or `data/`, that's the category.
Second tier is this extension map; third tier is an LLM classifier fallback
for ambiguous text.

```yaml
routing:
  emails: [.eml, .mbox]
  data: [.csv, .tsv, .parquet, .json]
  messages: [.wav, .mp3, .m4a, .flac, .opus, .ogg]
  docs: [.pdf, .docx, .doc, .pptx, .xlsx, .html, .htm, .heic, .png, .jpg, .jpeg]
  llm_classifier: [.txt, .md, .rtf]
```

## File limits

```yaml
limits:
  max_size_bytes: 104857600    # 100 MB
  oversized_path: corpus/inbox/_oversized/
  unsupported_path: corpus/inbox/_unsupported/
```

## Salience threshold

```yaml
salience_min: 0.30
```

## Redaction allowlist

Paths matching these globs bypass the redactor (e.g., the owner's own SOPs
that intentionally include test API keys):

```yaml
redaction_skip:
  - corpus/raw/sops/internal/**
  - corpus/raw/data/test-fixtures/**
```

## Entity allowlist

Named entities (PERSON, ORG, LOC, GPE) **not** redacted in the owner's own
writing. Set during onboarding.

```yaml
entity_allowlist:
  - "Your Company Name Here"
  - "Owner Name Here"
```

## Transcription backend

```yaml
transcription:
  backend: whisper.cpp        # local default; alternative: openai_whisper_api
  model: base.en
  fallback_to_plaud: true
```

## OCR backend

```yaml
ocr:
  backend: pytesseract        # local default
  skip_if_pdf_text_layer: true
```

## Wiki page cleanup

```yaml
wiki_orphan_grace_days: 7
stale_grace_days: 14
```

## Vocabulary normalization

```yaml
vocabulary_normalization:
  lowercase: true
  strip_punctuation: true
  collapse_whitespace: true
  strip_articles: [the, a, an]
  preserve_hyphens: true
  no_stemming: true
```
