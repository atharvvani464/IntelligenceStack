"""Resource-quota governance: bounds how much a request may cost, not just
what it may touch.

`policy.py`'s four controls answer "may this function run, with these
parameters, for this caller, against this row?". They say nothing about *how
much*: a caller entitled to a function is, absent this module, entitled to
call it as many times, and in as large a fan-out, as a single question can
express. In an agent that turns one prompt into N governed calls (see
`agent_core.ModelServingClient.plan`), that gap is a real cost and
availability risk in production, not a hypothetical one -- a rushed "compare
every customer" prompt issues one governed call, and one Photon/warehouse
charge, per customer named.

This is checked once per *request*, not once per *call*, which is why it does
not live inside `policy.enforce`: fan-out is a property of the whole proposed
plan, and `enforce` is deliberately pure and call-scoped. Two independent
limits, because an auditor needs to be able to tell them apart:

* **Fan-out** -- the number of governed calls a single question's plan would
  issue, checked against `Principal.max_fanout` before any of them run.
* **Rate** -- the number of *granted and executed* calls this principal has
  made in the last rolling minute, checked with a SQL aggregate over the
  durable audit trail (`AuditEngine.count_granted_since`) so the budget holds
  across requests and process restarts, not just in one agent's memory.

Both fields on `Principal` default to `None` (unrestricted), so a caller with
no declared budget behaves exactly as the sandbox did before this control
existed.
"""

from datetime import datetime, timedelta, timezone

from src.governance.identity import Principal
from src.governance.policy import GovernanceDecision
from src.lakehouse.audit_engine import AuditEngine

# The rolling window a per-minute rate limit is measured over.
RATE_WINDOW = timedelta(minutes=1)


def enforce_quota(
    principal: Principal, proposed_call_count: int, audit_log_path
) -> GovernanceDecision | None:
    """Refuse a request that would exceed `principal`'s resource budget.

    Returns `None` when the request is within budget -- "no objection" --
    mirroring how a granted `policy.enforce` decision is told apart from a
    denial by its caller checking `.allowed`, rather than by a sentinel.
    """
    if principal.max_fanout is not None and proposed_call_count > principal.max_fanout:
        return GovernanceDecision(
            allowed=False,
            control="RESOURCE_QUOTA",
            detail=(
                f"{principal.display_name} ({principal.role}) is budgeted for at most "
                f"{principal.max_fanout} governed call(s) per request; this question "
                f"would issue {proposed_call_count}. Ask about fewer customers at once."
            ),
            parameters={"proposed_call_count": proposed_call_count},
            principal_id=principal.principal_id,
        )

    if principal.rate_limit_per_minute is not None:
        recent = AuditEngine(log_path=audit_log_path).count_granted_since(
            principal.principal_id, datetime.now(timezone.utc) - RATE_WINDOW
        )
        if recent >= principal.rate_limit_per_minute:
            return GovernanceDecision(
                allowed=False,
                control="RESOURCE_QUOTA",
                detail=(
                    f"{principal.display_name} ({principal.role}) has reached the "
                    f"budget of {principal.rate_limit_per_minute} governed call(s) per "
                    f"minute ({recent} in the last 60s). Retry shortly."
                ),
                parameters={"proposed_call_count": proposed_call_count},
                principal_id=principal.principal_id,
            )

    return None
