# Solomon Review Log

A running record of our file-by-file walkthrough of the system, plus everything that's been deferred. Updated every time something is decided, changed, or deferred.

---

## Where we are

Walking the system top-down. Topics covered:

- **Install flow** — 2026-05-25. How the installer works, what it changes in Hermes, why Solomon no longer auto-installs Hermes.
- **v3 implementation** — 2026-05-25. Steps 1–9 of the v3 plan complete: adapter rewrite, plugin.yaml, session_state, hooks/slash/tools/inbound/cron rewrites, install.sh + CLI updates, SPEC.md + README rewrites. 201 tests passing.
- **v3 real-Hermes smoke test** — 2026-05-25. Solomon installs cleanly into the live Hermes at `~/.hermes/hermes-agent/`. Hermes's PluginManager loads Solomon and reports: 19 tools, 3 hooks, 9 commands registered, 0 errors. The 17 cron jobs land in Hermes's cron list with correct schedules. `solomon doctor` reports all green (preferred_channel yellow as expected on fresh install). `solomon ingest` against the live install runs an LLM turn that loads the skill inline, enables the solomon toolset, and returns `[SILENT]` (correct convention). Four fixes landed during the smoke test — see "Decisions made" entries dated 2026-05-25 for the entry-point form, /status rename, gateway race, and CLI adapter wiring.

---

## Open items (things we still need to address)

Each item: **what** / **source** / **state**.

### High priority

- **Hermes plugin contract mismatches.** [RESOLVED 2026-05-25 — all five items fixed in code and spec]

  All five originally-flagged issues are now corrected. Cross-references in case of regression:

  1. **`plugin.yaml` manifest** — created at [solomon/plugin.yaml](solomon/plugin.yaml). Spec coverage: [SPEC.md §14](SPEC.md) "Plugin manifest".
  2. **`register_tool` signature** — [adapter.py:HermesAdapter.register_tool](solomon/adapter.py) now passes `toolset="solomon"` positionally and `schema=` (not `parameters=`). Spec coverage: [SPEC.md §14](SPEC.md) "Tool registration".
  3. **`register_command` handler signature** — [slash.py](solomon/slash.py) handlers all take `(raw_args: str) -> str | None`. The "pending intent" file pattern routes mode-switching needs to the next `pre_llm_call`. Spec coverage: [SPEC.md §14](SPEC.md) "Slash command registration".
  4. **Hook callback signatures** — [hooks.py](solomon/hooks.py) uses kwargs-only signatures matching Hermes's call sites. Spec coverage: [SPEC.md §14](SPEC.md) "Hook signatures".
  5. **Context injection model** — `pre_llm_call` returns `{"context": "..."}`; Hermes splices it into the user message (preserves prompt cache). Spec coverage: [SPEC.md §12](SPEC.md) "How injection actually works in Hermes".

- **Gateway-initiated messages from cron.** [RESOLVED 2026-05-25]
  - Wrapped `tools.send_message_tool.send_message_tool` in `adapter.send_to_owner`. Falls back to `pending_messages.jsonl` on gateway failure; `retry_pending_messages()` re-dispatches on the next cron. Spec coverage: [SPEC.md §14](SPEC.md) "Proactive outbound messages".

- **Reading past conversations from outside a turn.** [RESOLVED 2026-05-25]
  - Wrapped `hermes_state.SessionDB.list_sessions_rich` + `get_messages_as_conversation` in `adapter.read_conversations`. The daily reflection cron consumes it via the `read_conversations` tool. Spec coverage: [SPEC.md §14](SPEC.md) "Conversation history".

### Medium priority

- **No rollback on pip install failure.** [open]
  - If `pip install -e .` fails partway, partial state stays. Re-running install.sh is safe (idempotent) but failure isn't graceful.
  - Source: walkthrough Q1, 2026-05-25.

- **Windows has no automatic scheduling.** [open]
  - Cron isn't available on native Windows. install.sh skips cron with a warning if `crontab` isn't on PATH. WSL works (it's Linux). On native Windows the owner has to run `solomon daily`/`weekly`/`checkin` manually or set up Task Scheduler themselves.
  - Source: walkthrough Q6, 2026-05-25.

- **Spec says "Restart Hermes if running" — install.sh doesn't do this.** [RESOLVED 2026-05-25]
  - Removed from SPEC.md §3 (Step 11 deleted). `hermes plugins enable solomon` is enough; Hermes loads plugins lazily on next conversation/cron.

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

- **14 staggered weekly compression jobs instead of one iterating job.** [2026-05-25]
  - Compressing all 14 playbooks in one LLM turn either blows the context window or suffers "lost in the middle" degradation. Separate Hermes-cron jobs scheduled 5 minutes apart on Sunday (03:00–04:05) give each compression a fresh context. Profile-summary regen is a 15th job at 04:10. Total: 15 weekly + 1 daily + 1 checkin = 17 crons.
  - Lives in: [solomon/weekly.py](solomon/weekly.py), [SPEC.md §10.2](SPEC.md).

- **Pending-intent file for slash → next-pre_llm_call hand-off.** [2026-05-25]
  - Hermes does NOT pass `session_id` to slash handlers, but `/onboard`, `/mentor`, `/private`, `/endprivate` need to set per-session state. The handler writes `~/.hermes/solomon/.pending_intent.json` with a 60-second TTL; the next `pre_llm_call` (which DOES carry `session_id`) claims and applies it atomically.
  - Lives in: [solomon/session_state.py](solomon/session_state.py), [solomon/slash.py](solomon/slash.py), [solomon/hooks.py](solomon/hooks.py), [SPEC.md §8 and §14](SPEC.md).

- **PII redaction at write-time only.** [2026-05-25]
  - SSN/SIN/credit-card (Luhn-validated)/phone/email/passport patterns are replaced with placeholders before any file write. The LLM still sees raw text in the live turn; only the persisted playbooks/queues/profile keep redacted versions. Backups of `~/.hermes/solomon/` are safe to copy.
  - Lives in: [solomon/profile.py](solomon/profile.py) `redact()`, [SPEC.md §7 "PII redaction at write time"](SPEC.md).

- **Tool dedupe in `propose_addition`.** [2026-05-25]
  - Same `(file, section, content, status=pending)` tuple returns the existing pending item ID rather than appending a duplicate. Makes the daily reflection cron safe to retry without producing duplicates if it dies mid-run.
  - Lives in: [solomon/tools.py](solomon/tools.py) `propose_addition`, [SPEC.md §7.4](SPEC.md).

- **Entry-point form: module reference, not function reference.** [2026-05-25, found during Step 10 smoke test]
  - `pyproject.toml`'s `[project.entry-points."hermes_agent.plugins"]` was `solomon = "solomon.plugin:register"`. Hermes calls `ep.load()` then `getattr(module, "register")`, so the entry-point value must resolve to the MODULE, not the function. Changed to `solomon = "solomon.plugin"`.
  - Lives in: [pyproject.toml](pyproject.toml). Without this fix, Hermes loads the plugin then warns "no register() function" and skips it.

- **Slash command `/status` collides with Hermes's built-in.** [2026-05-25, found during Step 10 smoke test]
  - Hermes already registers `/status`; plugins that re-register it are dropped at registration time with a warning. Renamed to `/solomon-status` so the symmetry with `/solomon-off` / `/solomon-on` makes it discoverable.
  - Lives in: [solomon/slash.py](solomon/slash.py) `COMMANDS`, [SPEC.md §8.3](SPEC.md), [README.md](README.md) commands table.

- **Plugin shim at ~/.hermes/plugins/solomon/.** [2026-05-25, found during Step 10 smoke test]
  - Hermes's `hermes plugins enable` CLI command only validates directory-based plugins, even though its PluginManager loads entry-point plugins. install.sh now writes a tiny 2-file shim directory (plugin.yaml + an __init__.py that imports `register` from the pip-installed package) so the CLI enable step succeeds.
  - Lives in: [install.sh](install.sh) Section 7. Solomon is still primarily discovered via the entry-point; the shim is purely to satisfy the CLI gate.

- **Re-confirm plugins.enabled after register-crons.** [2026-05-25, found during Step 10 smoke test]
  - When a Hermes gateway is running concurrently with `install.sh`, it may rewrite `config.yaml` from its in-memory snapshot mid-install — overwriting our `plugins.enabled` change. install.sh now re-issues `hermes plugins enable solomon` after cron registration to make the final on-disk state authoritative. Also detects a running gateway PID and prints a "restart for Solomon to be picked up" warning at the end.
  - Lives in: [install.sh](install.sh) Section 9.

- **CLI `solomon ingest`/`daily`/`weekly`/`checkin` need an adapter.** [2026-05-25, found during Step 10 smoke test]
  - The CLI runs outside a Hermes plugin context, so `tools._adapter` is None at import time and `daily.run_now()` returns `{ok: False, reason: 'no adapter'}`. Fixed by having cli.py construct a ctx-less `HermesAdapter` via `_build_adapter()` and pass it explicitly to `run_now(adapter=...)` in every cron-firing subcommand.
  - Lives in: [solomon/cli.py](solomon/cli.py).

---

## Reading order if picking this up cold

1. This file — to see what's still open and what's been decided.
2. [SPEC.md](SPEC.md) — the binding spec.
3. The file or area we're walking next.
