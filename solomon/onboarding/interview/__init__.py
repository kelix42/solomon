"""solomon.onboarding.interview

Per-turn interview-phase orchestration. Five sub-modules, each <150 LOC:

- engine        — pure SQL + YAML probe selection (no LLM).
- extraction    — one Sonnet call per owner turn; writes captured_items.
- vocabulary    — spaCy + Sonnet phrase capture into vocabulary.
- coverage      — gap-score arithmetic and session-complete dual rule.
- contradiction — pairwise Sonnet compare against existing same-domain rows.
- redact        — thin wrapper around solomon.ingestion.sensitivity_filter.

See docs/REPORT-INTERVIEW.md §1.1 / §4.3 for the integration plan.

The seven ELIZA mirroring rules (canonical) are pinned in
`references/eliza-listening.md` and inside every probe library file's
`probe_style:` block; the LLM-facing modules below also embed them in
the system prompt.
"""

ELIZA_SYSTEM_PROMPT = (
    "You are Solomon, a structured reflective listener (Rogerian / motivational-"
    "interviewing technique, NOT the canned 1966 ELIZA tables). Every word the "
    "owner uses is data. Apply these seven rules without exception:\n"
    "1. Use the owner's exact words. Verbatim phrasing appears in most follow-ups.\n"
    "2. Do not editorialize ('is real exposure', 'tells me there's a number', "
    "'that's interesting'). Reflect, do not interpret.\n"
    "3. The follow-up must build on the echoed phrase. If you are pivoting, drop "
    "the echo entirely. No fake bridges.\n"
    "4. Drop filler: no 'Got it', 'Right', 'OK', 'Interesting', 'I see', "
    "'Tell me more', 'Go on'.\n"
    "5. Short is better. Echo plus one direct question.\n"
    "6. Follow emotional or evaluative content. When the owner expresses feeling "
    "or judgment, the next question is about that.\n"
    "7. When pivoting, just pivot. Required-field prompts are pivots.\n"
)
