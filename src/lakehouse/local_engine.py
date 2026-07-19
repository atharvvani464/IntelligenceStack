"""Local execution engine standing in for a Databricks Serverless SQL Warehouse.

This module materialises the same medallion topology declared in
`src/ingestion/dlt_pipeline.py` -- bronze (raw landing zone), silver (validated
and PII-masked), gold (aggregated per customer) -- and exposes the Unity Catalog
function `get_customer_anomaly_score` over the gold layer.

Two properties matter for the architecture this project argues for:

1. The function is invoked with **bound parameters**, never string
   interpolation. The caller cannot influence SQL structure, only supply a
   value. This is the mechanism that makes the governance boundary real rather
   than advisory.
2. The anomaly definition lives here, in one place, and is consumed by both the
   agent and the telemetry dashboard -- so the number the agent reports and the
   number on the dashboard cannot drift apart.

Swapping DuckDB for `databricks-sql-connector` against a Serverless SQL
Warehouse changes the connection, not the query or the calling convention.
"""

import logging
from dataclasses import dataclass

import duckdb

from src.settings import CATALOG, LANDING_ZONE, SCHEMA

logger = logging.getLogger("LocalEngine")

# An event is anomalous when its latency exceeds the population baseline by this
# many standard deviations. Statistical, rather than a magic millisecond value,
# so the definition survives a change in the underlying traffic profile.
ANOMALY_SIGMA = 3.0

# The single fully-qualified name of the governed function. The agent may only
# reference this identifier; it never composes one.
ANOMALY_FUNCTION = f"{CATALOG}.{SCHEMA}.get_customer_anomaly_score"


@dataclass(frozen=True)
class AnomalyScore:
    """One row of the `get_customer_anomaly_score` result set."""

    customer_id: str
    total_events: int
    total_anomalies: int
    mean_latency_ms: float
    risk_factor: float

    def as_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "total_events": self.total_events,
            "total_anomalies": self.total_anomalies,
            "mean_latency_ms": round(self.mean_latency_ms, 2),
            "risk_factor": self.risk_factor,
        }


class LakehouseEngine:
    """Reads the landing zone and serves governed analytical functions."""

    def __init__(self, landing_zone=LANDING_ZONE):
        self.landing_zone = landing_zone
        self._con = duckdb.connect(database=":memory:")
        self._built = False

    # ------------------------------------------------------------------ #
    # Medallion construction
    # ------------------------------------------------------------------ #

    def _source_glob(self) -> str:
        return str(self.landing_zone / "*.json")

    def build(self) -> "LakehouseEngine":
        """Materialise bronze -> silver -> gold from the landing zone.

        Mirrors the transformations declared in the DLT pipeline: quality
        expectations drop invalid rows, PII payloads are hashed, and the gold
        layer aggregates to the grain the agent queries.
        """
        if not self.landing_zone.exists() or not any(self.landing_zone.glob("*.json")):
            raise FileNotFoundError(
                f"No telemetry found in {self.landing_zone}. "
                "Start the synthetic generator to populate the landing zone."
            )

        # Bronze: raw ingestion, schema inferred, exactly as Auto Loader would.
        self._con.execute(
            """
            CREATE OR REPLACE TABLE bronze_telemetry_raw AS
            SELECT * FROM read_json_auto(?, format='newline_delimited')
            """,
            [self._source_glob()],
        )

        # Silver: enforce the same expectations as @dlt.expect_or_drop /
        # @dlt.expect_or_fail, and mask PII-bearing payloads with sha256.
        self._con.execute(
            """
            CREATE OR REPLACE TABLE silver_telemetry_cleaned AS
            SELECT
                customer_id,
                CAST(timestamp AS TIMESTAMP)        AS event_time,
                CAST(timestamp AS DATE)             AS timestamp_date,
                event_type,
                CAST(context.latency AS DOUBLE)     AS latency_ms,
                CAST(context.pii AS BOOLEAN)        AS is_pii,
                CASE WHEN context.pii THEN sha256(payload) ELSE payload END
                                                    AS masked_payload
            FROM bronze_telemetry_raw
            WHERE customer_id IS NOT NULL
              AND event_type IN ('click', 'purchase', 'search', 'error')
            """
        )

        # Gold: per-customer aggregation plus the population baseline used to
        # classify anomalies. Computing the baseline once here is what lets the
        # function stay a pure lookup.
        self._con.execute(
            f"""
            CREATE OR REPLACE TABLE gold_customer_analytics AS
            WITH baseline AS (
                SELECT
                    AVG(latency_ms)    AS pop_mean,
                    STDDEV(latency_ms) AS pop_stddev
                FROM silver_telemetry_cleaned
            )
            SELECT
                s.customer_id,
                COUNT(*)                                   AS total_events,
                AVG(s.latency_ms)                          AS mean_latency_ms,
                COUNT(*) FILTER (
                    WHERE s.latency_ms >
                          b.pop_mean + ({ANOMALY_SIGMA} * COALESCE(b.pop_stddev, 0))
                )                                          AS total_anomalies
            FROM silver_telemetry_cleaned s
            CROSS JOIN baseline b
            GROUP BY s.customer_id
            """
        )

        self._built = True
        logger.info("Medallion layers materialised from %s", self.landing_zone)
        return self

    def _ensure_built(self) -> None:
        if not self._built:
            self.build()

    # ------------------------------------------------------------------ #
    # Governed functions
    # ------------------------------------------------------------------ #

    def get_customer_anomaly_score(self, target_id: str) -> list[dict]:
        """The Unity Catalog function the agent is permitted to invoke.

        `target_id` is passed as a bound parameter. It cannot alter the shape of
        the statement, so a hostile value is inert -- it simply matches no rows.
        """
        self._ensure_built()
        rows = self._con.execute(
            """
            SELECT
                customer_id,
                total_events,
                total_anomalies,
                mean_latency_ms,
                ROUND(100.0 * total_anomalies / NULLIF(total_events, 0), 2) AS risk_factor
            FROM gold_customer_analytics
            WHERE customer_id = ?
            """,
            [target_id],
        ).fetchall()

        return [
            AnomalyScore(
                customer_id=r[0],
                total_events=r[1],
                total_anomalies=r[2],
                mean_latency_ms=r[3],
                risk_factor=r[4] if r[4] is not None else 0.0,
            ).as_dict()
            for r in rows
        ]

    def fleet_summary(self) -> dict:
        """Population-level statistics backing the telemetry dashboard."""
        self._ensure_built()
        row = self._con.execute(
            f"""
            WITH baseline AS (
                SELECT AVG(latency_ms) AS pop_mean, STDDEV(latency_ms) AS pop_stddev
                FROM silver_telemetry_cleaned
            )
            SELECT
                COUNT(*),
                AVG(s.latency_ms),
                COUNT(*) FILTER (
                    WHERE s.latency_ms >
                          b.pop_mean + ({ANOMALY_SIGMA} * COALESCE(b.pop_stddev, 0))
                ),
                COUNT(DISTINCT s.customer_id),
                MAX(b.pop_mean + ({ANOMALY_SIGMA} * COALESCE(b.pop_stddev, 0)))
            FROM silver_telemetry_cleaned s
            CROSS JOIN baseline b
            """
        ).fetchone()

        return {
            "total_events": row[0],
            "mean_latency_ms": round(row[1], 2) if row[1] is not None else 0.0,
            "total_anomalies": row[2],
            "distinct_customers": row[3],
            "anomaly_threshold_ms": round(row[4], 2) if row[4] is not None else 0.0,
        }

    def known_customers(self) -> list[str]:
        """Customer IDs present in the gold layer, most active first."""
        self._ensure_built()
        return [
            r[0]
            for r in self._con.execute(
                "SELECT customer_id FROM gold_customer_analytics ORDER BY total_events DESC"
            ).fetchall()
        ]
