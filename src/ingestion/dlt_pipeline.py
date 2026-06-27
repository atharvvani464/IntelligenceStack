import dlt
from pyspark.sql.functions import col, to_date, current_timestamp, when, sha2

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
        .withColumn("timestamp_date", to_date(col("timestamp")))
        .withColumn("ingest_time", current_timestamp())
        .withColumn("masked_payload", when(col("context.pii") == True, sha2(col("payload"), 256)).otherwise(col("payload")))
    )

@dlt.table(
    name="gold_customer_analytics",
    comment="Materialized Golden Layer tailored for agentic feature scaling and immediate lookup queries."
)
def gold_customer_analytics():
    return (
        dlt.read("silver_telemetry_cleaned")
        .groupBy("customer_id", "timestamp_date", "event_type")
        .agg(
            count("masked_payload").alias("event_count"),
            avg(col("context.latency")).alias("mean_latency_ms")
        )
    )
