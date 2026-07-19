import dlt
from pyspark.sql.functions import (
    avg,
    col,
    count,
    current_timestamp,
    sha2,
    stddev,
    to_date,
    when,
)
from pyspark.sql.functions import sum as spark_sum

# Reference configuration mappings natively
source_dir = "/mnt/telemetry/raw_logs"

@dlt.table(
    name="bronze_telemetry_raw",
    comment="Raw streaming ingestion from cloud storage landing zone via optimized Auto-Loader."
)
def bronze_telemetry_raw():
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "json")
        .option("cloudFiles.inferColumnTypes", "true")
        .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
        .load(source_dir)
    )

@dlt.table(
    name="silver_telemetry_cleaned",
    comment="Cleaned and validated telemetry records with optimized Deletion Vectors enabled.",
    table_properties={
        "delta.enableDeletionVectors": "true",
        "delta.compatibility.writerVersion": "7",
        "delta.compatibility.readerVersion": "3"
    }
)
@dlt.expect_or_drop("valid_customer_id", "customer_id IS NOT NULL")
@dlt.expect_or_fail("valid_event_type", "event_type IN ('click', 'purchase', 'search', 'error')")
def silver_telemetry_cleaned():
    return (
        dlt.read_stream("bronze_telemetry_raw")
        .withColumn("event_time", col("timestamp").cast("timestamp"))
        .withColumn("timestamp_date", to_date(col("timestamp")))
        .withColumn("ingest_time", current_timestamp())
        .withColumn("latency_ms", col("context.latency").cast("double"))
        .withColumn("is_pii", col("context.pii").cast("boolean"))
        .withColumn(
            "masked_payload",
            when(col("context.pii") == True, sha2(col("payload"), 256)).otherwise(col("payload")),  # noqa: E712
        )
    )

@dlt.table(
    name="gold_customer_analytics",
    comment="Materialized Golden Layer at per-customer grain, tailored for agentic lookup queries."
)
def gold_customer_analytics():
    # Population 3-sigma latency threshold, computed once and broadcast, so the
    # governed function stays a pure per-customer lookup. This is the same
    # anomaly definition implemented in src/lakehouse/local_engine.py.
    silver = dlt.read("silver_telemetry_cleaned")
    stats = silver.agg(
        avg("latency_ms").alias("pop_mean"),
        stddev("latency_ms").alias("pop_stddev"),
    ).collect()[0]
    threshold = (stats["pop_mean"] or 0.0) + 3.0 * (stats["pop_stddev"] or 0.0)

    return (
        silver.groupBy("customer_id").agg(
            count("*").alias("total_events"),
            spark_sum(when(col("latency_ms") > threshold, 1).otherwise(0)).alias("total_anomalies"),
            avg("latency_ms").alias("mean_latency_ms"),
        )
    )
