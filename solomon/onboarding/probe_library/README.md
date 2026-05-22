# probe_library/

Per-domain probe libraries for the interview engine. Each YAML file is read
at runtime by `solomon.onboarding.interview.engine.select_next_probe()`. The
library is data, not code; ship a new version of a file and the engine picks
it up on the next launch with no migration.

## Files

- `industry.yaml` — Session 0 (Industry / sector context). Ported verbatim
  from the Drive Solomon skill pack at
  `solomon-interview-engine/probe_library/industry.yaml`.
- `belief_system.yaml` — Session 1 (worldview, trust baseline, risk).
- `why.yaml` — Session 2 (purpose / mission / vocation).
- `principles.yaml` — Session 3 (operating values, examples).
- `ideal_outcomes.yaml` — Session 4 (the five-year picture).
- `non_negotiables.yaml` — Session 5 (the hard-nos that drive
  `foundation/05-non-negotiables.yaml`, which is the deterministic hard-rule
  source for the decision pipeline Stage 4).
- `scopes.yaml` — Session 6 (the domain / scope map for the decision pipeline).
- `_generic.yaml` — last-resort fallbacks, used when no domain-specific
  keyword fires.

## Schema

Every domain file declares:

- `domain`: matches the `domain` column on `captured_items` and `coverage`.
- `version`: semver string. Bumped per the rules below.
- `priority`: 1–10 weight for this domain when ranking probes across sessions
  (higher wins in cross-domain mentoring).
- `probe_style`: the seven mirroring rules, copy-pasted verbatim. Shipped
  inside every library so each file is self-contained — read-only at runtime
  is the contract.
- `required_fields`: ordered list of fields the session cannot complete
  without. Each has `id`, `prompt`, `accepts`, `satisfied_when`,
  `follow_up_keywords`. Filled either naturally during discovery (Stage B)
  or via direct prompt in Stage C with a hard 2-turn cap. `"I don't know"`,
  `"not applicable"`, and `"decline to answer"` each count as filled and
  produce a `captured_items` row tagged `field:<id>` in `keywords`.
- `keywords`: a map of keyword → ranked template list. Each template uses
  `{phrase}` as a slot, replaced verbatim with the owner's last phrase
  before the question is asked. Lower priority number wins.
- `fallbacks`: cross-keyword forward prompts used when nothing in the
  owner's answer triggers a keyword. Plain pivots, no forced echo.

## Semver bump rules

- **Patch** (0.1.0 → 0.1.1): new probe templates added under existing
  keywords. No reader change needed.
- **Minor** (0.1.0 → 0.2.0): new keywords or new `required_fields` entries.
  Existing sessions keep working; `coverage` will gain new sub-topic rows
  on the next launch.
- **Major** (0.1.0 → 1.0.0): breaking schema changes (field renamed or
  removed, semantics flipped). Requires a code update on the reader side.

Bumping a library version writes a `mentoring_queue` row of source
`probe_library_update` (priority 7) on next launch so the owner can decide
whether to re-probe. **There is no automatic mass re-probe.**

## `{phrase}` substitution rule

The engine extracts the most salient noun- or verb-phrase from the owner's
last answer and substitutes it for `{phrase}` in the chosen template.
Punctuation around `{phrase}` must produce a grammatical sentence — the
template author is responsible for that. The Drive's mirroring discipline
(see `probe_style` above and `references/eliza-listening.md`) makes
verbatim echoing the default; the engine never paraphrases the slot value.

## Cross-references

- Source-of-truth docs: `references/eliza-listening.md`,
  `references/interview-architecture.md`.
- Lifecycle tables consumed: `captured_items`, `coverage`,
  `clarification_queue`, `vocabulary`, `sessions` (all in
  `solomon/storage/schema.sql`).
- Engine: `solomon/onboarding/interview/engine.py`.
- Session runner (Stage A–E orchestration): `solomon/onboarding/session_runner.py`.
