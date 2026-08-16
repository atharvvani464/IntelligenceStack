"""Who is asking, and what they are entitled to see.

Until now the boundary answered one question -- "may this *function* run?" --
which is only half of enterprise data governance. The other half is "may this
*caller* run it, against *this row*?". A control that treats a junior analyst
and an on-call SRE identically is not least privilege, however well it blocks
SQL.

This module supplies the missing principal. Each identity carries:

* the set of governed functions it may invoke -- the per-caller equivalent of
  ``GRANT EXECUTE ON FUNCTION`` in Unity Catalog; and
* a customer scope -- the per-caller equivalent of a Unity Catalog row filter.

The two are deliberately separate, because they fail differently and an auditor
needs to tell them apart: "you may not use that tool at all" is a different
finding from "you may use that tool, but not on that customer".

Callerless requests resolve to `SERVICE_PRINCIPAL`, an explicitly unrestricted
identity. That is stated plainly rather than hidden: it preserves the sandbox's
previous behaviour, and in production the API would reject an unauthenticated
request instead of falling back to it.
"""

import re
from dataclasses import dataclass, field

_CUSTOMER_ID = re.compile(r"^CUST_(\d{3})$")


@dataclass(frozen=True)
class Principal:
    """An authenticated caller and the entitlements attached to it."""

    principal_id: str
    display_name: str
    role: str
    # Governed functions this principal may invoke. Empty means none -- an
    # auditor, for instance, is entitled to the trail but not to customer data.
    allowed_functions: frozenset[str] = field(default_factory=frozenset)
    # Inclusive numeric range of customer identifiers this principal may see.
    # None means unrestricted; the row-level filter equivalent.
    customer_scope: tuple[int, int] | None = None
    # Whether this principal may read the governance audit trail.
    can_view_audit: bool = False
    description: str = ""

    # -- function-level entitlement ---------------------------------- #

    def may_invoke(self, function_name: str) -> bool:
        return function_name in self.allowed_functions

    # -- row-level entitlement --------------------------------------- #

    def scope_label(self) -> str:
        if self.customer_scope is None:
            return "all customers"
        low, high = self.customer_scope
        if high < low:
            # An empty range is a deliberate "no customer data" entitlement,
            # not a misconfiguration -- say so rather than rendering it as a
            # nonsensical identifier range.
            return "no customer data"
        return f"CUST_{low:03d}–CUST_{high:03d}"

    def may_see_customer(self, target_id: str) -> bool:
        """Whether `target_id` falls inside this principal's row scope."""
        if self.customer_scope is None:
            return True
        match = _CUSTOMER_ID.match(target_id)
        if not match:
            # An identifier this principal cannot parse is one it cannot be
            # shown to be entitled to, so it is refused rather than assumed.
            return False
        low, high = self.customer_scope
        return low <= int(match.group(1)) <= high

    def as_dict(self) -> dict:
        return {
            "principal_id": self.principal_id,
            "display_name": self.display_name,
            "role": self.role,
            "allowed_functions": sorted(self.allowed_functions),
            "customer_scope": self.scope_label(),
            "can_view_audit": self.can_view_audit,
            "description": self.description,
        }


_DATA_FUNCTIONS = frozenset(
    {"get_customer_anomaly_score", "get_customer_timeline", "search_knowledge_base"}
)

# The identity used when a request arrives without a caller. Unrestricted, and
# named so that it is obvious in the audit trail when it was used.
SERVICE_PRINCIPAL = Principal(
    principal_id="svc.control-plane",
    display_name="Control Plane Service",
    role="service",
    allowed_functions=_DATA_FUNCTIONS,
    customer_scope=None,
    can_view_audit=True,
    description=(
        "Unauthenticated fallback used when a request supplies no caller. "
        "Unrestricted by design; a production deployment would reject the "
        "request instead of falling back to this identity."
    ),
)

# Demo directory. In production these come from the workspace identity provider
# and their grants live in Unity Catalog; the shape of the decision is the same.
PRINCIPALS: dict[str, Principal] = {
    "sre.okafor": Principal(
        principal_id="sre.okafor",
        display_name="A. Okafor",
        role="Site Reliability Engineer",
        allowed_functions=_DATA_FUNCTIONS,
        customer_scope=None,
        can_view_audit=True,
        description="On-call SRE. Full customer coverage and access to the runbooks.",
    ),
    "analyst.chen": Principal(
        principal_id="analyst.chen",
        display_name="M. Chen",
        role="Customer Analyst",
        allowed_functions=_DATA_FUNCTIONS,
        customer_scope=(400, 419),
        can_view_audit=False,
        description=(
            "Analyst assigned to the CUST_400–CUST_419 book. May use every "
            "analytical tool, but only against customers in that book."
        ),
    ),
    "auditor.silva": Principal(
        principal_id="auditor.silva",
        display_name="R. Silva",
        role="Compliance Auditor",
        allowed_functions=frozenset(),
        customer_scope=(0, -1),  # deliberately empty range
        can_view_audit=True,
        description=(
            "Compliance reviewer. Entitled to the full governance trail and to "
            "no customer data whatsoever -- oversight without exposure."
        ),
    ),
    SERVICE_PRINCIPAL.principal_id: SERVICE_PRINCIPAL,
}


def resolve_principal(principal_id: str | None) -> Principal:
    """Look up a caller, falling back to the service identity.

    An unknown identifier resolves to the *least* privileged outcome the system
    can offer rather than the fallback, so a typo in a caller header can never
    silently escalate to unrestricted access.
    """
    if not principal_id:
        return SERVICE_PRINCIPAL
    known = PRINCIPALS.get(principal_id)
    if known is not None:
        return known
    return Principal(
        principal_id=principal_id,
        display_name=principal_id,
        role="unrecognised",
        allowed_functions=frozenset(),
        customer_scope=(0, -1),
        can_view_audit=False,
        description="Caller is not present in the identity directory.",
    )


def directory() -> list[dict]:
    """The demo identities, for the control plane's principal switcher."""
    return [PRINCIPALS[k].as_dict() for k in ("sre.okafor", "analyst.chen", "auditor.silva")]
