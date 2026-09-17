"""Column-level masking: the third axis of least privilege, alongside the
function-level grant (`policy.py`) and the row-level scope (`identity.py`).

Unlike every other control in this project, a column mask does not refuse a
request -- it lets the call proceed and transforms *part of* what comes back.
That is a deliberate, different shape, and it mirrors the production target
exactly: a Unity Catalog column mask does not deny a query either, it rewrites
specific column values for the querying principal while the rest of the row
passes through untouched.

The one property that makes this honest rather than cosmetic: masking is
applied once, immediately after the governed function returns, *before* the
row reaches either the caller's payload or the agent's synthesis step (see
`agent_core.MosaicAnalyticsAgent.run`). If it only redacted the payload table
and left the narrated answer to read the real numbers, the mask would be
theatre -- the exact figure would simply leak out through prose instead of a
column. Masking upstream of synthesis is what keeps that from happening.
"""

# What a masked cell renders as. A string, not `None`, so a masked column is
# visibly *redacted* rather than indistinguishable from a genuinely absent
# value -- the two are different findings for an auditor.
MASK_SENTINEL = "•••"


def mask_rows(rows: list[dict], masked_columns: frozenset[str]) -> list[dict]:
    """Redact `masked_columns` in every row, leaving everything else intact.

    Returns new dicts; the rows passed in are never mutated, so a caller
    holding a reference to the original result set is not surprised by it
    changing underfoot.
    """
    if not masked_columns:
        return rows
    return [
        {key: (MASK_SENTINEL if key in masked_columns else value) for key, value in row.items()}
        for row in rows
    ]
