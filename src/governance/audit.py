"""Durable audit trail for every action the agent takes.

The governance boundary in `policy.py` decides what may run. This module makes
those decisions *evidence*: each one is appended to a durable log that outlives
the request, the process, and the demo. Without it, "a per-action audit trail"
is a claim; with it, the question auditors actually ask -- "show me everything
the AI did, and prove nothing else happened" -- has a queryable answer.

Three design choices matter:

**Append-only.** Records are appended to a JSON Lines file and never rewritten.
An audit log that can be edited in place is not evidence. Append-only also means
concurrent writers never corrupt earlier records, and the file can be shipped to
cold storage or a SIEM by tailing it.

**One record per governance decision, not per request.** A hybrid question
produces two decisions, so it produces two records. The HTTP response can only
carry one `governance` object, but the audit trail loses nothing.

**Recording must never break the agent.** A failure to write is logged and
swallowed. An audit sink that can take the service down is a liability, not a
control -- the correct failure mode here is to keep serving and surface the gap.

In production this is a Delta table with retention and access policy applied;
the record shape is unchanged.
"""

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from src.settings import AUDIT_DIR, AUDIT_LOG

logger = logging.getLogger("GovernanceAudit")

# Appends are serialised within a process so two concurrent requests cannot
# interleave partial lines.
_WRITE_LOCK = threading.Lock()


def new_request_id() -> str:
    """Correlation id shared by every decision from a single user request."""
    return uuid.uuid4().hex[:12]


@dataclass
class AuditRecord:
    """One governance decision, and what happened as a result."""

    request_id: str
    prompt: str
    function: str | None
    allowed: bool
    control: str
    detail: str
    parameters: dict = field(default_factory=dict)
    # Who asked. An audit trail that records what happened but not who did it
    # cannot answer an auditor's first question.
    principal_id: str = ""
    principal_role: str = ""
    executed: bool = False
    rows_returned: int = 0
    citations_returned: int = 0
    serving_mode: str = ""
    # Wall-clock cost of this governed call, in milliseconds. Recorded so the
    # question "is this fast enough?" is answered by measurement rather than
    # assertion -- and so a slow tool can be identified after the fact.
    duration_ms: float = 0.0
    # Columns redacted in this call's result before it was returned -- see
    # `governance/masking.py`. Empty means nothing was masked.
    masked_columns: list[str] = field(default_factory=list)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    recorded_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_dict(self) -> dict:
        return asdict(self)


class AuditTrail:
    """Append-only sink for governance decisions."""

    def __init__(self, log_path=AUDIT_LOG):
        self.log_path = log_path

    def record(self, record: AuditRecord) -> bool:
        """Append one decision. Returns False if it could not be written.

        Never raises: an audit failure must not fail the user's request.
        """
        try:
            with _WRITE_LOCK:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                # Opened in append mode per write so an external process can
                # rotate or ship the file without holding a stale handle.
                with open(self.log_path, "a") as handle:
                    handle.write(json.dumps(record.as_dict()) + "\n")
            return True
        except Exception as exc:  # noqa: BLE001 - deliberately swallowed
            logger.warning("Audit record could not be written: %s", exc)
            return False

    def entry_count(self) -> int:
        """Number of records currently on disk."""
        if not self.log_path.exists():
            return 0
        with open(self.log_path) as handle:
            return sum(1 for line in handle if line.strip())


def ensure_audit_dir() -> None:
    """Create the audit directory up front so the first write cannot fail."""
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
