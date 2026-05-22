# Sleep Cycle — 12 nightly jobs

Hermes' gateway cron registers all 12 jobs at install time via `/cron add`. Default schedule: `0 3 * * *` owner-local time. Each job runs as a fresh isolated agent session with the corresponding skill attached. Failures don't block other jobs.

| # | Name | Purpose |
|---|---|---|
| 1 | hindsight | Reviews past 24h decisions; writes audit rows |
| 2 | archival | Moves stale items to long-term storage |
| 3 | surprise-replay | Surfaces unexpected outcomes → mentoring_queue priority 4 |
| 4 | stress-test | Simulated edge cases against current rules |
| 5 | conflict-detection | Cross-heuristic conflicts → mentoring_queue priority 3 |
| 6 | working-memory-cleanup | Trims `db.working_memory` past TTL |
| 7 | autonomy-reeval | Updates `db.scope_autonomy` per promotion/demotion thresholds |
| 8 | mentoring-scheduler | Triggers a mentoring session if priority ≤ 4 queued OR 7d elapsed |
| 9 | corpus-lint | `solomon-corpus-lint` (contradictions, stale, orphans, near-duplicates) |
| 10 | corpus-backup | Snapshots db + corpus, encrypts, ships to BACKUP_DEST_LOCAL (and optional Drive) |
| 11 | embed-pending | Picks up `embedded_at IS NULL` rows in captured_items + decisions, batch-embeds via OpenAI, upserts to Pinecone |
| 12 | yaml-reconcile | Re-renders all 7 foundation YAMLs from captured_items; queues drift to mentoring_queue priority 5 |

## Owner overrides

- `/solomon-sleep-now` — runs all 12 in sequence
- `/solomon-sleep-job <name>` — runs one
- `/solomon-sleep-skip <name>` — defers to tomorrow
- `/cron list` — Hermes-built-in; shows all registered jobs
