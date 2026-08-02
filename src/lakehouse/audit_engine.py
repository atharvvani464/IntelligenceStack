"""Analytical view over the governance audit trail.

The audit log is append-only JSON Lines (see `src/governance/audit.py`). This
module reads it with DuckDB -- the same "raw files in, SQL out" pattern the
medallion pipeline uses for telemetry -- so the evidence is queryable rather
than merely stored.

Queries are re-read from disk on each call rather than cached, because the log
grows while the service runs and a stale answer to "what has the agent done?"
is worse than a slightly slower one.
"""

import logging

import duckdb

from src.settings import AUDIT_LOG

logger = logging.getLogger("AuditEngine")

EMPTY_SUMMARY = {
    "total_actions": 0,
    "granted": 0,
    "denied": 0,
    "denial_rate": 0.0,
    "executed": 0,
    "distinct_requests": 0,
    "first_recorded": None,
    "last_recorded": None,
}


class AuditEngine:
    """Reads the append-only audit log and answers governance questions."""

    def __init__(self, log_path=AUDIT_LOG):
        self.log_path = log_path
        self._con = duckdb.connect(database=":memory:")

    # ------------------------------------------------------------------ #

    def _load(self) -> bool:
        """Materialise the current log into a queryable relation.

        Returns False when there is nothing recorded yet, so callers can render
        an honest empty state instead of a fabricated zero-row summary.
        """
        if not self.log_path.exists() or self.log_path.stat().st_size == 0:
            return False

        self._con.execute(
            """
            CREATE OR REPLACE TABLE audit_log AS
            SELECT * FROM read_json_auto(?, format='newline_delimited')
            """,
            [str(self.log_path)],
        )
        return True

    # ------------------------------------------------------------------ #

    def summary(self) -> dict:
        """Headline governance posture across everything recorded."""
        if not self._load():
            return dict(EMPTY_SUMMARY)

        row = self._con.execute(
            """
            SELECT
                COUNT(*),
                COUNT(*) FILTER (WHERE allowed),
                COUNT(*) FILTER (WHERE NOT allowed),
                COUNT(*) FILTER (WHERE executed),
                COUNT(DISTINCT request_id),
                MIN(recorded_at),
                MAX(recorded_at)
            FROM audit_log
            """
        ).fetchone()

        total = row[0] or 0
        denied = row[2] or 0
        return {
            "total_actions": total,
            "granted": row[1] or 0,
            "denied": denied,
            "denial_rate": round(100.0 * denied / total, 2) if total else 0.0,
            "executed": row[3] or 0,
            "distinct_requests": row[4] or 0,
            # DuckDB parses the ISO timestamps into datetimes; normalise back to
            # strings so the API contract is a predictable JSON shape.
            "first_recorded": str(row[5]) if row[5] else None,
            "last_recorded": str(row[6]) if row[6] else None,
        }

    def by_control(self) -> list[dict]:
        """Decision counts grouped by the control that made them."""
        if not self._load():
            return []
        rows = self._con.execute(
            """
            SELECT control,
                   COUNT(*)                        AS decisions,
                   COUNT(*) FILTER (WHERE NOT allowed) AS denials
            FROM audit_log
            GROUP BY control
            ORDER BY denials DESC, decisions DESC
            """
        ).fetchall()
        return [{"control": r[0], "decisions": r[1], "denials": r[2]} for r in rows]

    def by_function(self) -> list[dict]:
        """How often each governed function was invoked, and how often refused."""
        if not self._load():
            return []
        rows = self._con.execute(
            """
            SELECT COALESCE(function, '(no matching function)') AS fn,
                   COUNT(*)                            AS decisions,
                   COUNT(*) FILTER (WHERE allowed)     AS granted,
                   COUNT(*) FILTER (WHERE NOT allowed) AS denied
            FROM audit_log
            GROUP BY fn
            ORDER BY decisions DESC
            """
        ).fetchall()
        return [
            {"function": r[0], "decisions": r[1], "granted": r[2], "denied": r[3]}
            for r in rows
        ]

    def top_denied_prompts(self, limit: int = 5) -> list[dict]:
        """The questions most often refused -- what users try that they may not do."""
        if not self._load():
            return []
        rows = self._con.execute(
            """
            SELECT prompt, control, COUNT(*) AS refusals
            FROM audit_log
            WHERE NOT allowed
            GROUP BY prompt, control
            ORDER BY refusals DESC, prompt
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [{"prompt": r[0], "control": r[1], "refusals": r[2]} for r in rows]

    def recent(self, limit: int = 25) -> list[dict]:
        """Most recent decisions, newest first."""
        if not self._load():
            return []
        rows = self._con.execute(
            """
            SELECT recorded_at, request_id, prompt, function, allowed, control,
                   executed, rows_returned, citations_returned
            FROM audit_log
            ORDER BY recorded_at DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [
            {
                "recorded_at": r[0],
                "request_id": r[1],
                "prompt": r[2],
                "function": r[3],
                "outcome": "GRANTED" if r[4] else "DENIED",
                "control": r[5],
                "executed": r[6],
                "rows": r[7],
                "citations": r[8],
            }
            for r in rows
        ]
