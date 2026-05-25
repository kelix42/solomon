# Solomon Review Log

A running record of our file-by-file walkthrough of the system, plus everything that's been deferred. Updated every time something is decided, changed, or deferred.

---

## Where we are

Walking the system top-down. Topics covered:

- **Install flow** — 2026-05-25. How the installer works, what it changes in Hermes, why Solomon no longer auto-installs Hermes.

---

## Open items (things we still need to address)

Each item: **what** / **source** / **state**.

### High priority

- **Hermes plugin contract mismatches.** [open — verified against live Hermes 2026-05-25]

  Confirmed against `~/.hermes/hermes-agent/` source. These are real bugs that prevent Solomon from loading or functioning:

  1. **Missing `plugin.yaml` manifest.** `hermes_cli/plugins.py:19` requires every directory plugin to have one. The pip entry-point path may work without it, but the canonical shape includes one. Add: `solomon/plugin.yaml` with name/version/description/hooks list.
  2. **`register_tool` signature wrong** ([adapter.py:31-44](solomon/adapter.py#L31-L44)). Hermes signature is `register_tool(name, toolset, schema, handler, ...)`. Our adapter passes `parameters=` (Hermes uses `schema=`) and never passes `toolset` (required, no default). Need to pick a toolset name like `"solomon"` for all our tools.
  3. **`register_command` handler signature wrong** ([slash.py](solomon/slash.py)). Hermes calls `handler(raw_args: str) -> str | None`. Our handlers take `(args: dict, session=None)` and return a dict with `system_prompt`/`message` keys. The `system_prompt` field is completely ignored by Hermes. Need to: (a) accept raw_args string, (b) return just the response text, (c) move system-prompt injection out of slash handlers into pre_llm_call.
  4. **Hook callback signature wrong** ([hooks.py](solomon/hooks.py)). Hermes invocation: `pre_llm_call(*, session_id, user_message, conversation_history, is_first_turn, model, platform, **_)`. Our `pre_llm_call(messages, session)` won't even bind.
  5. **Context injection model is different** (`hermes_cli/plugins.py:1495-1529`). Hermes hooks **return** context, they don't mutate a messages list. Return value `{"context": "..."}` or a plain string is injected into the **user message** (not the system prompt — for prompt-cache preservation). This is arguably cleaner than our design; we need to switch to it.

- **No public Hermes API for gateway-initiated messages.** [open — verified 2026-05-25]
  - The proactive notification flow ([inbound.dispatch_pending_notifications](solomon/inbound.py)) and the weekly check-in cron ([checkin.run](solomon/checkin.py)) both need to push a message to the owner from a cron context. I searched `gateway/` and `hermes_cli/` and found only `PluginContext.inject_message()` which works in CLI but not gateway mode (per its docstring).
  - Alternatives to investigate: (a) Hermes-side message queue we write to, (b) using `pre_gateway_dispatch` somehow, (c) accepting that proactive-from-cron is out of scope without a Hermes-side API.

- **No public Hermes API for reading past conversations from outside a turn.** [open — verified 2026-05-25]
  - The daily reflection cron ([daily.reflect_step](solomon/daily.py)) needs to read yesterday's Hermes conversations. The `conversation_history` is passed *to* the pre_llm_call hook, but I couldn't find a way for an external script (cron) to read session history.
  - Alternatives: (a) read Hermes's SQLite session DB directly (path: `~/.hermes/sessions/` or similar — need to find it), (b) accept reflection-cron is per-turn rather than per-day, (c) Solomon maintains its own per-turn log via pre_llm_call returns.

### Medium priority

- **No rollback on pip install failure.** [open]
  - If `pip install -e .` fails partway, partial state stays. Re-running install.sh is safe (idempotent) but failure isn't graceful.
  - Source: walkthrough Q1, 2026-05-25.

- **Windows has no automatic scheduling.** [open]
  - Cron isn't available on native Windows. install.sh skips cron with a warning if `crontab` isn't on PATH. WSL works (it's Linux). On native Windows the owner has to run `solomon daily`/`weekly`/`checkin` manually or set up Task Scheduler themselves.
  - Source: walkthrough Q6, 2026-05-25.

- **Spec says "Restart Hermes if running" — install.sh doesn't do this.** [open]
  - SPEC.md Section 3 Step 11 says the installer restarts Hermes. The script doesn't. We should either implement it (`hermes gateway restart`) or remove it from the spec.
  - Source: spec audit during walkthrough Q2.

### Low priority (deferred features, not bugs)

- **Real-LLM smoke tests.** Scaffolding exists at `tests/smoke/` but the actual tests aren't written. Each costs real LLM tokens; weekly CI when wired.
- **CLI version of `/onboard`.** For users who haven't set up Hermes yet. Lowers first-run friction further.
- **PyPI publication.** `pip install solomon-brain` from PyPI doesn't work — package isn't published. The installer works because it uses `pip install -e .` from a checkout or `pip install git+...` from the repo.

---

## Decisions made

- **Don't auto-install Hermes.** [2026-05-25]
  - install.sh exits cleanly with instructions if Hermes isn't found. Rationale: fewer failure modes, owner ends up with a working Hermes they understand before Solomon touches anything.
  - Lives in: [install.sh:39-92](install.sh#L39-L92), [SPEC.md](SPEC.md) Section 3 Step 2.

- **Three preflight checks before any work happens.** [2026-05-25]
  - OS guard (Windows native bash), git check, Python 3.10+ check. All fail fast with plain-English messages, exit 1.
  - Lives in: [install.sh:38-105](install.sh#L38-L105) and [install.sh:129-159](install.sh#L129-L159), [SPEC.md](SPEC.md) Section 3 Step 1.

---

## Reading order if picking this up cold

1. This file — to see what's still open and what's been decided.
2. [SPEC.md](SPEC.md) — the binding spec.
3. The file or area we're walking next.
