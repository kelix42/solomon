# Solomon

**An AI chief of staff that learns how you make decisions, then gradually makes them for you.**

Solomon is a Hermes plugin. It turns any Hermes installation into a domain-specific decision engine for one business owner. It listens, predicts, audits, acts, and earns trust scope by scope over months. It mirrors how a human brain works: predict, get surprised, sleep, forget the unused, remember what matters.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_GH/solomon/main/install.sh | bash
```

That's it. The installer:
1. Detects or installs Hermes.
2. Installs Solomon as a pip plugin (`hermes_agent.plugins` entry point).
3. Provisions Postgres (local Docker or remote Supabase, your choice).
4. Runs schema migrations.
5. Creates your tenant's GitHub repo for foundation files.
6. Launches the first-run wizard: industry picker, six interview sessions.
7. Enters observe-only mode.

After install, every Hermes session you run is Solomon. The brain learns from everything you do.

## Onboarding (the first thing you do after install)

A new tenant cannot start at zero. The brain needs a foundation. Onboarding has two parts that both have to happen before the brain goes live:

**1. The six-session interview** (~6-10 hours total, spread over 1-2 weeks)

```bash
solomon onboard session_1   # Belief and worldview
solomon onboard session_2   # The why
solomon onboard session_3   # Principles
solomon onboard session_4   # Ideal outcomes
solomon onboard session_5   # Non-negotiables
solomon onboard session_6   # Domain map
```

Each session asks 5-10 structured questions, transcribes voice or accepts text, and writes the answers into a YAML foundation file (`~/.hermes/solomon/foundation/`).

**2. The historical ingestion** (the big batch dump)

This is how the brain gets the years of context the interview alone can't cover. Drop old emails, contracts, transcripts, SOPs, customer feedback, internal docs — anything that contains decisions you've made.

```bash
solomon ingest path/to/old/emails/*.eml \
                path/to/proposals/*.pdf \
                path/to/sops/*.md \
                path/to/transcripts/*.txt

# Anything you don't want indexed at all (medical records, legal, family stuff):
solomon ingest --flag-sensitive path/to/private/file.pdf -- path/to/normal/file.txt
```

The ingestion pipeline:
1. Strips PII (SSN, SIN, credit cards, phones, passports, email addresses) before anything else sees the text
2. Classifies each document (email thread, contract, transcript, SOP, etc.)
3. Chunks it the right way for its type (emails by message, transcripts by speaker turn, contracts by section)
4. Embeds each chunk locally so the brain can search by meaning later (no API calls, nothing leaves your machine for this step)
5. Extracts the decisions inside (situation, options, decision, reasoning, outcome)
6. Mines patterns across all your documents and proposes heuristics ("In 23 of 31 pricing decisions, after-hours work was charged 20-25% above base — looks like an implicit rule")
7. Cross-references documents that reference each other

Then you review what was found:

```bash
solomon ingestion review
```

Walk through each proposed heuristic with approve/reject/defer. Only approved heuristics enter the brain's active rule set.

**After both parts are done, the brain enters observe-only mode for 30 days.** It captures everything, predicts, audits, and logs — but takes no actions. After 30 days of clean track record, scopes can begin moving up the autonomy ladder.

## What you get

- **Capture.** Every message on every channel becomes a `RawEvent`. Gmail, Twilio, Plaud, voice notes, webhooks.
- **Salience scoring.** The brain rates how much each event matters. Stakes, novelty, emotion, owner involvement.
- **Predict before reason.** System 1 (Sonnet, rules only) and System 2 (Opus, full context) both answer. The gap between them is the surprise score, which drives learning.
- **Audit gate.** A separate model call checks every proposed action against your principles and non-negotiables before it ships.
- **Autonomy ladder.** Four levels per scope: watch → suggest → act with approval → act alone. Trust earned through track record, lost on overrides.
- **Sleep cycle.** Every night, eight jobs run: hindsight, archival, surprise replay, stress test, conflict detection, working memory cleanup, autonomy re-evaluation, mentoring scheduler.
- **Predictions and counterfactuals.** Every decision logs what we expect to happen and what we'd expect if we'd chosen differently. Calibration improves much faster than outcome-only learning.
- **Heuristic lifecycle.** Rules are versioned, evidence-based, with active/fragile/archived/superseded states. Confidence rises with success, falls with overrides. Time alone never lowers confidence.
- **Onboarding.** Structured six-session interview fills foundation files (beliefs, why, principles, non-negotiables, ideal outcomes, taxonomy).
- **Ingestion.** Bulk-upload years of historical email, contracts, transcripts. The brain extracts decisions, mines heuristics, seeds memory.

## Private mode

Sometimes you want the LLM for something unrelated to the business. Run `/private`. Nothing gets logged, classified, audited, or remembered until you toggle it off or end the session.

Private means private. There's no recovery — if you forget you're in private mode and have a real business conversation, that data is gone. The cost of an occasional forgotten conversation is small. The cost of users not trusting the kill switch is large.

The non-negotiable check still runs in private mode. The kill switch turns off learning, not guardrails.

## How Solomon stays compatible with Hermes

Solomon does not reach into Hermes internals. It only uses the public plugin contract: `register_tool`, `register_command`, and the `pre_llm_call` / `post_llm_call` / `on_session_start` / etc. hooks. Hermes commits to keeping that contract stable.

The one file that touches Hermes is `solomon/adapter.py`. If anything in Hermes ever changes shape, that's the only file we update. The rest of Solomon — the conductor, the sleep cycle, the audit gate, all of it — never knows what Hermes version it's running on.

We also run tests against the adapter on every Hermes release. Anything that breaks shows up in CI before users see it.

## Status

Phase 1 (observe-only mode) is the minimum viable build. The brain captures everything, predicts, audits, logs, but does not act. After 30 days of observe mode, scopes can begin moving up the autonomy ladder.

See `docs/PHASES.md` for what's built, what's scaffolded, and what's planned next.

## License

MIT. See LICENSE.

## Credits

Architected from the Project Solomon design document. Built on top of [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research.
