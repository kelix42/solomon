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

- **Verify Hermes API names against a real running Hermes.** [open]
  - Names used in `adapter.py`: `pre_llm_call`, `post_llm_call`, `on_session_start` hooks; `register(ctx)` entry-point; `plugins.enabled` config key.
  - Source: walkthrough Q2, 2026-05-25.
  - Why it matters: if any name is wrong, the plugin loads silently but tools/commands/hooks never fire. Easy to spot once we load Solomon into live Hermes. Only `adapter.py` needs the fix.

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
