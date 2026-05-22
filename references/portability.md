# Portability — how Solomon ports off Hermes

`portable: true` SKILL.md flag means a port to a non-Hermes agent is a documented swap, not a rewrite.

## Portable file-by-file

| Solomon file/folder | Portable as-is? | Notes |
|---|---|---|
| `SOUL.md`, `MEMORY.md`, `USER.md` | ✅ Markdown | Map to target agent's identity / memory primitives |
| `skills/` (markdown) | ✅ agentskills.io standard | YAML front-matter + body translates to most agent runtimes |
| `foundation/` YAMLs | ✅ Plain YAML | |
| `corpus/` (raw + wiki + index + log) | ✅ Plain markdown + raw bytes | |
| `db/schemas/*.sql` | ✅ Standard SQLite | Run on the target host |
| `references/` | ✅ | |

## Hermes-specific (must rebind)

| File/folder | Hermes binding | Port target |
|---|---|---|
| `hermes-plugins/` | `register(ctx)` API (verified §2.4.6) | Target's tool/hook registration API |
| `workers/` | launchd/systemd; reads `db/solomon.db` | Same — workers are plain Python services, no Hermes dependency |
| Telegram adapter | Hermes gateway built-in | Target's messaging adapter / native bot framework |
| `orchestrator/sleep-cycle/` registration | Hermes `/cron add` | Target's scheduler |
| Hermes `dispatch_tool` calls in plugins | `ctx.dispatch_tool` | Target's tool-call API |

## Port checklist

1. Implement an equivalent of `register(ctx)` for the target runtime.
2. Map Hermes hooks (pre_llm_call, etc.) to target hooks.
3. Replace Telegram gateway adapter with target's messaging surface.
4. Re-register the 12 sleep-cycle jobs in target's scheduler.
5. Run `pytest tests/test_phase_loading.py` against the new runtime to confirm phase rules survive.

The 22 reference docs and the 27 skill bodies survive port intact. Only the integration glue changes.
