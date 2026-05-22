# Autonomy Spectrum

Solomon's per-scope autonomy ladder. Each scope (pricing, hiring, ops, customer, vendor, finance, …) has a current level in `db.scope_autonomy`. Sleep-Cycle Job 7 (autonomy-reeval) computes promotions/demotions nightly.

| Level | Name | Behaviour |
|---|---|---|
| L0 | Manual | Solomon does nothing automatic; only answers when asked. |
| L1 | Suggested | Solomon proposes; owner approves every action via Telegram. |
| L2 | Drafted | Solomon drafts and ships only after owner one-tap. |
| L3 | Supervised | Solomon ships routine actions; novel / high-stakes still need approval. |
| L4 | Autonomous | Solomon ships everything in scope; daily digest summarizes. |

## Promotion / demotion thresholds

- **Promotion**: ≥20 events in scope (`db.events`) over the trailing 30 days with override rate < 10% AND audit-pass rate > 90% → `level + 1`.
- **Demotion**: override rate > 25% OR a hard-rule violation in scope → `level - 1`.

Each transition writes a `decisions/log.md` entry.

## Owner-state ceiling

Pipeline Stage 9 (owner-state gate) modulates the per-event ceiling:

- **Green** (recovery > 60% AND sleep > 7h): full scope autonomy.
- **Yellow** (recovery 33–60% OR sleep 5–7h): downgrade to L2 ceiling regardless of scope.
- **Red** (recovery < 33% OR explicit stress flag): downgrade to L1 ceiling.
- **Whoop missing** (plugin disabled or stale > 24h): default to Green; one-time warning logged.

The effective autonomy for an event is `min(scope_autonomy.level, owner_state_ceiling)`.
