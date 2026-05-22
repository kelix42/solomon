# Interview Architecture — distilled §0 + §1 + §2.1 + §2.7 of SOLOMON-PLAN.md

Solomon's interview phase has 5 skills + 3 lifecycle tables.

## Skills (all `phase: interview`)

1. `solomon-interview-engine` — orchestrator; reads probe_library + clarification_queue, picks next probe.
2. `solomon-extraction` — parses each owner answer into captured_items rows.
3. `solomon-vocabulary-capture` — pulls phrases via spaCy + LLM; writes vocabulary rows.
4. `solomon-coverage-tracker` — tracks gap_score per sub-topic; decides session-complete.
5. `solomon-contradiction-check` — real-time conflict detection; writes to clarification_queue (NOT mentoring_queue).

## Tables read/written

- `db.captured_items` — primary owner-rule store.
- `db.coverage` — what's been probed, including `last_probed_version` for probe-library migration.
- `db.vocabulary` — owner's voice as data (SQL-only, not embedded).
- `db.clarification_queue` — same-session contradictions (interview-engine reads this BEFORE every probe).
- `db.sessions` — onboarding/mentoring resumption flag.
- `db.proposed_rules` — rules surfaced from corpus extraction; mentoring confirms before they reach captured_items.

## Probe library

`skills/interview/solomon-interview-engine/probe_library/<domain>.yaml`. One file per domain. Each declares a semver `version`. Lower priority number = higher priority. Slot `{phrase}` for verbatim insertion.

## Two-phase rule

Interview phase writes captured_items / coverage / vocabulary. Decision phase only reads them. The boundary is enforced by SKILL.md `phase:` front-matter and CI tests.
