"""Part 26 — token budget tracking for ingestion.

Ingestion can call the LLM many times per document (classify, extract,
summarize, embed). Without a cap, a single bad batch — a leaked archive
of 50,000 emails, say — would burn through a tenant's monthly model
spend in an afternoon. Solomon's answer is a hard per-tenant cap with
notify-and-pause semantics:

  - default cap: ~$50/month, expressed as a token budget
    (configurable via SOLOMON_INGESTION_MONTHLY_CAP_TOKENS).
  - `can_spend()` is consulted before every LLM call.
  - When the cap is hit, the ingestion worker pauses the job and the
    notification pipeline tells the owner.

Phase 1 is intentionally coarse: we don't have a per-tenant token
counter table yet, so `tokens_used_this_month` returns 0 and `record_spend`
just logs. The interface is locked in now so callers can be written
against it; the storage swap is a one-table migration.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("solomon.ingestion.budget")

# ~$50/month at roughly $5 / 1M input tokens. Tune via env once we have
# real per-tenant numbers from Phase 2.
_DEFAULT_MONTHLY_CAP_TOKENS = 1_000_000
_ENV_CAP_KEY = "SOLOMON_INGESTION_MONTHLY_CAP_TOKENS"


def tokens_used_this_month(tenant_id: str) -> int:
    """Return tokens spent on ingestion for `tenant_id` this calendar month.

    Phase 1: we don't have per-tenant ingestion token accounting yet.
    `cycle_log.total_tokens` is global to the sleep cycle, and
    `audit_log` doesn't carry token counts. Until the
    `ingestion_budget_log` table exists, return 0 — meaning every
    `can_spend` check passes until the cap is bumped down to 0.

    TODO(part-26): once `ingestion_budget_log(tenant_id, tokens,
    spent_at)` exists, sum tokens where spent_at >= date_trunc('month',
    NOW()).
    """
    # TODO(part-26): real query against ingestion_budget_log.
    logger.debug(
        "tokens_used_this_month: tenant=%s returning 0 (Phase 1 stub)",
        tenant_id,
    )
    return 0


def monthly_cap_tokens(tenant_id: str) -> int:
    """Return the monthly ingestion token cap for `tenant_id`.

    Phase 1: read from env. Phase 2 should consult an adapter config
    key like `solomon.ingestion.monthly_cap_tokens` so per-tenant
    overrides become possible without restarting.
    """
    raw = os.getenv(_ENV_CAP_KEY)
    if raw is None:
        return _DEFAULT_MONTHLY_CAP_TOKENS
    try:
        cap = int(raw)
        if cap < 0:
            logger.warning(
                "monthly_cap_tokens: env %s=%r is negative; using default",
                _ENV_CAP_KEY,
                raw,
            )
            return _DEFAULT_MONTHLY_CAP_TOKENS
        return cap
    except ValueError:
        logger.warning(
            "monthly_cap_tokens: env %s=%r is not an int; using default",
            _ENV_CAP_KEY,
            raw,
        )
        return _DEFAULT_MONTHLY_CAP_TOKENS


def can_spend(tenant_id: str, estimated_tokens: int) -> bool:
    """Return True if spending `estimated_tokens` more would stay within cap.

    Cap-of-zero means "no ingestion allowed" and is honored. Negative
    `estimated_tokens` is treated as zero (defensive).
    """
    if estimated_tokens < 0:
        estimated_tokens = 0
    used = tokens_used_this_month(tenant_id)
    cap = monthly_cap_tokens(tenant_id)
    ok = (used + estimated_tokens) <= cap
    if not ok:
        logger.warning(
            "budget: tenant=%s would exceed cap (used=%d + est=%d > cap=%d)",
            tenant_id,
            used,
            estimated_tokens,
            cap,
        )
    else:
        logger.debug(
            "budget: tenant=%s can spend est=%d (used=%d cap=%d)",
            tenant_id,
            estimated_tokens,
            used,
            cap,
        )
    return ok


def record_spend(tenant_id: str, tokens: int) -> None:
    """Record that `tokens` were spent for `tenant_id`.

    Phase 1: log only. Phase 2 should INSERT into ingestion_budget_log.

    TODO(part-26): add `ingestion_budget_log(tenant_id, tokens,
    spent_at)` table and INSERT here so `tokens_used_this_month` has
    real numbers to sum.
    """
    if tokens < 0:
        logger.warning(
            "record_spend: tenant=%s negative tokens=%d ignored",
            tenant_id,
            tokens,
        )
        return
    # TODO(part-26): INSERT INTO ingestion_budget_log ...
    logger.info(
        "budget: tenant=%s spent %d tokens (not yet persisted)",
        tenant_id,
        tokens,
    )
