"""Solomon onboarding v2 — skill-driven interview.

This is the second take on onboarding. v1 (in ``solomon.onboarding``) was a
Python state machine that picked the next question from a deterministic
resolution chain. It worked but felt robotic — the LLM only saw individual
sub-tasks (reflect, classify intent, render YAML), never the whole
interview.

v2 inverts the control flow. The conductor injects the relevant SKILL.md
(``solomon-onboarding-00-industry``, etc.) + the probe library YAML + the
current state as a system message on every turn during an active
interview. The LLM reads the skill, follows the steps, and calls a small
set of database tools (``solomon_onboarding_capture``, etc.) to record
captures and complete the session.

The owner's skills live in ``~/.hermes/skills/solomon-onboarding/`` —
copied from the Drive design.

Public surface:
  - ``commands.OnboardingCommands`` — registers ``/onboard`` and
    ``/endinterview`` slash commands.
  - ``tools.register_tools`` — registers the LLM-callable database tools.
  - ``session.OnboardingSessionRegistry`` — in-memory map of which
    Hermes session_ids currently have an open interview.

No question-selection logic here — the LLM picks the next question by
following the skill. We're the storage and orchestration layer only.
"""
