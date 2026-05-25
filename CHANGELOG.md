# Changelog

## 1.0.0 — 2026-05-25

The first release of the rebuilt Solomon. Everything is new.

### What it is

A Hermes plugin that turns the LLM into a specialist for one owner's business. After onboarding, every Hermes conversation is run through the Solomon role. The LLM speaks in the owner's voice using the owner's captured rules. When an external message arrives (email, SMS, voice transcript), Solomon proactively analyzes it and proposes an action.

### What's in this release

- One-line install (`curl ... | bash`)
- Seven foundation interview sessions (`/onboard`)
- Active weekly mentoring (`/mentor`)
- Document ingestion (drop in `~/.hermes/solomon/inbox/`, processed nightly)
- Proactive inbound flow with two-pass thinking
- Three nightly cron jobs: reflection, weekly compression, weekly check-in
- Nine LLM tools, eight slash commands, four skill files
- Built-in PII redaction
- Hard off-switches: `/private` per conversation, `/solomon-off` globally
- Health-check command: `solomon doctor`
- Structured JSON logs at `~/.hermes/solomon/logs/`
- All state in one folder, git-tracked
- 127 passing tests (unit + integration)

### What's not in this release

By design, this version is built from a "bare bones" spec. Features kept out for simplicity (and addable later if a real need shows up): vector embeddings, semantic search, a separate decision pipeline, predictions/counterfactuals, fragility tracking, web UI, MCP server, multi-tenant support, model-tier selection.

The previous Solomon (the eight-thousand-line build with thirty-five database tables) is preserved on the `archive-v0` branch of this repo as historical reference. It is not maintained.

### Build

The full architecture is documented in [SPEC.md](SPEC.md).
