# Next session — chat-driven onboarding (route the interview through the Hermes gateway)

Read `/root/projects/solomon/BUILD-STATE.md` first, then this whole file
end-to-end before writing any code. Tests should be at 351/351 green at the
start. Do not break them.

Repo: `/root/projects/solomon`
Python: `/usr/local/lib/hermes-agent/venv/bin/python3`
Tests: `pytest tests/ -v`
Branch / push target: `main` on `https://github.com/kelix42/solomon`

**Do it yourself. No subagents.** This crosses the conductor (per-turn hot
path) AND adds new modules — a subagent will run out of Opus budget halfway
through and leave a half-wired router behind. Sessions A/B/C already paid
that cost; don't repeat it.

---

## What you're building

Today `solomon onboard session_0` is a CLI that reads from stdin and writes
to stdout. The interview engine itself is already decoupled from the
terminal — `_process_owner_turn(...)` takes a string in and returns a
string out — but the only entry point is the terminal loop in
`solomon/onboarding/session_runner.py::run_session`.

This session adds a **second entry point**: a Hermes slash command,
`/onboard <session_key>`, that runs the same 5-stage flow over the gateway
(Telegram, Discord, anything Hermes is connected to). The owner types
`/onboard session_0` in Telegram, Solomon replies with the first question.
The owner answers in Telegram, Solomon replies with the next question.
Repeat until the session completes and the foundation YAML is written.

State lives in the DB between messages (it already does — the interview
engine is checkpointed). What you're adding is the routing layer.

---

## Reading order — do this before writing code

1. `BUILD-STATE.md` — confirm 351 green, confirm no other dangerous work
   is mid-flight.
2. `docs/REPORT-INTERVIEW.md` §4 — the integration plan for the interview
   subsystem. The chat runner is the chat-shaped version of what §4
   describes.
3. `solomon/onboarding/session_runner.py` end-to-end. The functions you'll
   reuse verbatim: `_ensure_tenant`, `_open_or_resume_session`,
   `_seed_coverage`, `_process_owner_turn`, the Stage A reflector, the
   Stage D intent classifier, the Stage E foundation-render call. You'll
   build a new top-level orchestrator alongside `run_session`, not a
   replacement.
4. `solomon/onboarding/interview/engine.py` — `select_next_probe_with_meta`
   is the one you call to pick the next question. Returns
   `(probe, field_id_or_None)`. Tag captures with `field:<id>` when
   `field_id` is not None.
5. `solomon/private/mode.py` — read this top-to-bottom. It's the canonical
   example of a Solomon slash command (`register_command`, the
   `_handle_*_command(args, session_id, **kwargs)` signature, the
   `is_active(session_id)` predicate the conductor reads on every turn).
   Your `/onboard` command follows the same shape.
6. `solomon/conductor.py::_pre_llm_call` (lines ~253–578). You're adding
   ONE early-return branch here, before the existing pipeline runs. If
   there's an active onboarding session for this Hermes `session_id`, the
   owner's message is an interview answer — handle it, push the next
   question into `messages`, and bail out of `_pre_llm_call` before the
   10-stage pipeline fires. The pipeline runs on free-form decisions; an
   onboarding turn is not a decision.
7. `references/eliza-listening.md` — the seven rules. The chat runner
   uses the same Stage A reflector as the CLI, which already pins this
   doc. You don't re-pin it; you reuse the existing call path.

---

## Constraints — non-negotiable

- **Kill-switch env var: `SOLOMON_CHAT_ONBOARDING_DISABLE`.** Truthy
  (`1`/`true`/`yes`/`on`) skips the new branch in `_pre_llm_call`
  entirely. Recovery: `echo SOLOMON_CHAT_ONBOARDING_DISABLE=1 >> ~/.hermes/.env && hermes restart`.
  Read the env on every turn, no module-load caching (mirror the
  `SOLOMON_PIPELINE_DISABLE` shape).
- **try/except around the new code path in `_pre_llm_call`.** Any
  exception in the chat-runner branch logs + falls through to the existing
  pipeline path. A chat-onboarding crash MUST NOT break Hermes for normal
  conversations.
- **Single-session-per-tenant invariant.** Only one onboarding session can
  be open per Hermes `session_id` at a time. If `/onboard session_0` is
  invoked while a session is already open, reply with "you're already
  mid-interview on session_X — type /endinterview to abandon it first."
  The DB already enforces this — `sessions` table PK is `session_id` and
  `status='open'` is the resume signal. The slash command just needs to
  surface it cleanly.
- **`/endinterview` slash command** to abandon a stuck session. Sets the
  row's `status='abandoned'`, drops the in-memory state, replies with one
  line of confirmation. Symmetric with `/endprivate`.
- **No new dependencies.** Everything you need is already in
  `pyproject.toml`.

---

## Build order — bite-sized files, one at a time

### 1. `solomon/onboarding/chat_runner.py` — NEW

The chat-shaped orchestrator. One function in, one string out per turn.

Public surface (this is the contract the slash command + conductor depend
on; pick these names exactly so the tests stay stable):

```python
def start_session(
    session_key: str,             # e.g. "session_0" -> probe library "industry"
    *,
    hermes_session_id: str,       # the Hermes session_id from the gateway
    tenant_id: Optional[str] = None,
) -> StartResult:
    """Open or resume an onboarding session for this Hermes session.

    Returns the first/next question to send to the owner, plus the
    DB session_id (so the conductor can store it in its in-memory
    active-session map). Calls _ensure_tenant if tenant_id is None.

    If a session for this hermes_session_id is already open on a
    DIFFERENT domain, returns StartResult(error=...) without
    opening anything.
    """

def handle_turn(
    db_session_id: str,           # solomon-side session row, NOT hermes_session_id
    owner_message: str,
) -> TurnResult:
    """Process one owner message in an active onboarding session.

    Drives the 5-stage flow exactly as run_session does, but:
      - reads the owner's reply from `owner_message` instead of input()
      - returns the assistant's reply text instead of printing it
      - returns done=True when Stage E renders the foundation YAML

    Reuses _process_owner_turn (Stage A→C), the Stage D intent
    classifier, and the Stage E render call from session_runner. Do
    NOT duplicate that logic — import and call.
    """

def abandon_session(db_session_id: str) -> None:
    """Mark the session row status='abandoned'."""

@dataclass
class StartResult:
    db_session_id: Optional[str]   # None when error
    first_message: str             # the question/greeting to send
    error: Optional[str] = None

@dataclass
class TurnResult:
    reply: str                     # what to send back to the owner
    done: bool = False             # True after Stage E
    yaml_path: Optional[str] = None  # path to the rendered foundation YAML when done
```

Reusing the existing helpers:
- `_ensure_tenant()` from `session_runner.py` (NOT
  `get_or_create_tenant_id()` from `storage/decisions.py` — that one
  still uses `%s` placeholders and crashes on SQLite per the BUILD-STATE
  pitfall section).
- `_open_or_resume_session(tenant_id, domain, attempt, library_version)`.
- `engine.load_library(domain)`.
- `_seed_coverage(tenant_id, db_session_id, domain, library)`.
- `engine.select_next_probe_with_meta(db_session_id, domain, last_answer_text)`.
- `_process_owner_turn(db_session_id, domain, owner_text, library, extra_keyword_tag)`.
- The Stage E foundation-render call (whatever `run_session` calls at
  the end of Stage E — find it and reuse).

**Session-key → domain map.** The CLI accepts `session_0` through
`session_6` and maps to the seven probe libraries. Copy that map into
chat_runner verbatim (don't import from `session_runner` if it's a
module-private dict — duplicate it with a comment pointing back).

**Stage A reflection on the FIRST turn** (the cold open): there's no
prior owner answer, so the reflector returns empty and you skip straight
to Stage B (the first required-field question). The CLI handles this
the same way — mirror its logic.

### 2. `solomon/onboarding/commands.py` — NEW

The slash-command handlers. Mirror `solomon/private/mode.py` shape.

```python
class OnboardingCommands:
    def __init__(self, adapter, conductor) -> None: ...

    def register_command(self) -> None:
        self.adapter.register_command(
            name="onboard",
            aliases=["interview"],
            description="Start or resume an onboarding interview. Usage: /onboard session_0",
            handler=self._handle_onboard_command,
        )
        self.adapter.register_command(
            name="endinterview",
            aliases=["endonboard"],
            description="Abandon the current onboarding interview.",
            handler=self._handle_endinterview_command,
        )

    def _handle_onboard_command(self, args: str = "", session_id: str = "", **kwargs) -> str: ...
    def _handle_endinterview_command(self, args: str = "", session_id: str = "", **kwargs) -> str: ...
```

`_handle_onboard_command` parses `args` (the session key, default
`"session_0"`), calls `chat_runner.start_session(...)`, registers the
mapping `hermes_session_id -> db_session_id` on the conductor (via a
small `register_active_onboarding(hermes_session_id, db_session_id)`
method you add to `Conductor` in step 3), and returns the first
question as the slash command's response.

Validation: only `session_0` through `session_6`. Anything else returns
a one-line error listing the valid keys.

### 3. `solomon/conductor.py` — MODIFY (the dangerous file)

Three small surgical edits. Do these last, after steps 1–2 are tested
and green.

**Edit 3a.** Add an in-memory active-onboarding map to `Conductor.__init__`:

```python
self._active_onboarding: dict[str, str] = {}  # hermes_session_id -> db_session_id
```

Plus two helpers:

```python
def register_active_onboarding(self, hermes_session_id: str, db_session_id: str) -> None:
    self._active_onboarding[hermes_session_id] = db_session_id

def clear_active_onboarding(self, hermes_session_id: str) -> None:
    self._active_onboarding.pop(hermes_session_id, None)
```

On startup, repopulate from the DB — query `sessions WHERE status='open'`
and seed the map. Otherwise a Hermes restart mid-interview leaves the
session stuck. (Edge case: we don't store `hermes_session_id` on the
sessions row today. That's fine — leave the map empty after restart;
the owner can `/onboard session_X` again and `_open_or_resume_session`
will resume from where they left off. Document this limitation in the
commit message; durable hermes↔db session mapping is a follow-up.)

**Edit 3b.** New early-return branch at the top of `_pre_llm_call`,
AFTER the kill-switch checks for the pipeline but BEFORE the events
INSERT. Pseudocode:

```python
def _pre_llm_call(self, session_id: str = "", messages=None, **kwargs):
    # ... existing kill-switch checks ...

    if not _chat_onboarding_disabled():
        try:
            db_session_id = self._active_onboarding.get(session_id)
            if db_session_id is not None and messages:
                owner_msg = _extract_last_user_message(messages)
                if owner_msg:
                    result = chat_runner.handle_turn(db_session_id, owner_msg)
                    _inject_reply(messages, result.reply)
                    if result.done:
                        self.clear_active_onboarding(session_id)
                    return  # bypass the 10-stage pipeline for onboarding turns
        except Exception as e:
            logger.exception("chat onboarding turn failed; falling through: %s", e)

    # ... existing pipeline body unchanged ...
```

Notes:
- `_extract_last_user_message` walks `messages` from the end looking for
  the most recent `{"role": "user", "content": ...}`. Helper goes in
  `solomon/conductor.py` or `solomon/utils/messages.py`.
- `_inject_reply` appends `{"role": "assistant", "content": result.reply}`
  to `messages` AND sets a flag that tells Hermes "skip the LLM call,
  this is the final response." Check the Hermes plugin contract for the
  exact mechanism — `solomon/adapter.py` should already expose it; if not,
  this is a TODO and we fall back to letting the LLM see the reply as a
  pre-written assistant turn (cheaper than re-asking the model and good
  enough for v1).
- The `return` is critical — onboarding turns must NOT hit the 10-stage
  pipeline. The pipeline classifies free-form decisions; the interview
  is a different conversational mode.

**Edit 3c.** Wire `OnboardingCommands` into the plugin entry point.
In `solomon/plugin.py::register()`, after `conductor.register_tools()`:

```python
from .onboarding.commands import OnboardingCommands
onboarding_commands = OnboardingCommands(adapter, conductor)
onboarding_commands.register_command()
```

### 4. Tests — new file `tests/test_chat_onboarding.py`

Cover, at minimum:

1. **start_session opens a fresh row and returns the cold-open question.**
   Use the `solomon_db` fixture + `_StubLLMClient` from
   `tests/test_session_runner.py`. Assert `db_session_id` is non-None,
   `first_message` matches the `industry_label` probe text.
2. **start_session resumes an existing open session.** Pre-insert an
   `open` sessions row for the same tenant+domain; call again; assert
   the returned `db_session_id` matches the pre-existing one.
3. **start_session refuses cross-domain collision.** Pre-insert an open
   `industry` session; call `start_session("session_1", ...)` (which
   maps to `belief_system`); assert `error` is set and `db_session_id`
   is None.
4. **handle_turn drives Stage A→B→C correctly.** Script three turns of
   owner answers through the stub LLM; assert the captured-items table
   has rows tagged `field:industry_label`, `field:...`, etc.
5. **handle_turn returns done=True when Stage E completes.** Pre-fill
   all required_fields via direct DB inserts; call handle_turn once;
   assert `done=True` and `yaml_path` points to a real file under the
   foundation directory.
6. **abandon_session sets status='abandoned'.** Smoke test.
7. **Conductor branch: active session routes the turn to chat_runner,
   not the pipeline.** Use a stub conductor + a fake `messages` list;
   register an active session via `register_active_onboarding`; call
   `_pre_llm_call`; assert `chat_runner.handle_turn` was called and the
   10-stage pipeline runner was NOT.
8. **Conductor branch: no active session → pipeline runs as before.**
   Same harness, no active session registered; assert
   `chat_runner.handle_turn` was NOT called and pipeline ran.
9. **Kill-switch on: branch is skipped even with an active session.**
   Set `SOLOMON_CHAT_ONBOARDING_DISABLE=1`; assert pipeline ran.
10. **handle_turn raises → conductor falls through to pipeline.**
    Patch `chat_runner.handle_turn` to raise; assert the existing
    pipeline path ran and `_pre_llm_call` did not crash.
11. **OnboardingCommands: /onboard session_0 starts a session and
    returns the first question.** Use a stub adapter (mirror the one
    in `solomon/private/mode.py` tests if it exists, otherwise build a
    minimal one inline).
12. **OnboardingCommands: /onboard with an invalid session_key returns
    a helpful error listing the valid keys.**
13. **OnboardingCommands: /endinterview clears the active map and marks
    the row abandoned.**

Helper patterns to reuse:
- `solomon_db` fixture from `tests/conftest.py`.
- `_StubLLMClient` route-by-system-prompt from
  `tests/test_session_runner.py`.
- For the conductor branch tests, create a minimal `FakeConductor` that
  holds the `_active_onboarding` dict and exposes `_pre_llm_call` — you
  don't need a real Hermes ctx.

---

## Quality bar

- All 351 existing tests still green. Run them BEFORE you write any code
  to confirm the baseline. Run them AFTER each of steps 1, 2, 3 (don't
  batch — catch regressions at the file boundary).
- Every new module gets a corresponding test file or section in
  `tests/test_chat_onboarding.py`.
- Pool API only. `?` placeholders, never `%s` (per the
  `solomon-project` skill pitfalls).
- Conductor edits are SURGICAL — three additions, no refactoring of the
  existing pipeline path. The legacy behaviour must be byte-identical
  when `SOLOMON_CHAT_ONBOARDING_DISABLE=1` or no session is active.
- Commit at logical breakpoints:
  - C1: `chat_runner.py` + its tests
  - C2: `commands.py` + its tests
  - C3: conductor wire-up + its tests + plugin.py registration

---

## End-of-session protocol

1. `pytest tests/ -q` from the repo root with the hermes venv. All
   tests must pass.
2. Update `BUILD-STATE.md`:
   - Add a new section under "What's done" describing the chat
     onboarding routing.
   - Move "First live foundation interview" out of "Remaining items"
     because it's now actually possible.
   - Note the two new env vars (`SOLOMON_CHAT_ONBOARDING_DISABLE`) and
     the recovery paste.
   - Bump the date in the H1.
   - List the files added/modified.
3. `git add -A && git commit` (per-step commit messages from the build
   order above; if you batched, one merge-style commit with a body that
   walks through each file).
4. `git push origin main`.

## Smoke-test checklist to give Kekeli after landing

This is the per-`critical-path-prompt-template.md` checklist for
dangerous-file work. Include it verbatim in the BUILD-STATE update so
he sees it without having to ask.

1. `pytest tests/ -q` — confirm green from HEAD.
2. Hermes restart so the plugin picks up `OnboardingCommands`.
3. On Telegram: `/onboard session_0`. Confirm the first question lands
   ("What industry are you in?").
4. Answer it. Confirm the next question lands and looks coherent.
5. Tail `~/.hermes/logs/` while answering 2–3 more turns. Look for
   `WARN` lines about the chat-onboarding branch.
6. If anything weird: `echo SOLOMON_CHAT_ONBOARDING_DISABLE=1 >> ~/.hermes/.env && hermes restart`.
   Confirm normal Hermes Q&A still works with the kill-switch on.

---

## Pitfalls — pinned from the skill, do not relearn these

- `tests/__init__.py` exists; shared test helpers go in
  `tests/_chat_helpers.py` (underscore prefix so pytest doesn't collect
  it), imported as `from tests._chat_helpers import X`.
- The `engine._LIBRARY_CACHE` is module-global. Tests that
  monkeypatch `PROBE_LIBRARY_DIR` must clear it (autouse fixture).
- The Stage A reflector + Stage D intent classifier route by
  `tier="fast"` with distinct system prompts. The `_StubLLMClient` in
  `tests/test_session_runner.py` discriminates by inspecting the
  `system=` argument — reuse that pattern.
- Rich strips `[label]` markup from CLI output. The slash-command
  reply text is plain (no Rich), but if you write any debug prints
  with Rich, escape brackets with `\[...\]`.
- `mirror_event_to_decision` is now idempotent on `event_id`; no
  worry about double-inserts. But onboarding turns do NOT mirror to
  decisions — they're not decisions. The early-return bypasses both
  `mirror_event_to_decision` and the events INSERT for onboarding
  turns, which is the correct behaviour.

---

## Confidence breakdown (per critical-path template)

- First-try clean: ~60%. The conductor edit is small and isolated, but
  the `_inject_reply` Hermes-contract piece may need a feedback loop
  with the adapter to find the right "skip the LLM" mechanism.
- Clean after one iteration: ~85%. If `_inject_reply` doesn't have a
  clean hook, the fallback (let the LLM see the assistant reply as a
  pre-written turn) is straightforward and good enough for v1.
- Recovery floor: ~99%. The kill-switch + try/except + bit-for-bit
  legacy path mean a failed deploy is one `.env` line and a restart
  away from full recovery.

---

## Drive reference (read-only, do not modify)

`/root/projects/solomon-from-drive/onboarding/` if you want to see how
the original design framed chat-mode onboarding. Adapt patterns;
don't import or copy files.
