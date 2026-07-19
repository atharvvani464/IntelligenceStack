"""Synthetic telemetry generator for the landing zone.

Emits newline-delimited JSON batches into the configured landing zone, standing
in for enterprise Kafka streams or cloud-storage file arrival that Auto Loader
would ingest. The customer population is deliberately bounded so that customers
recur across events and accumulate a statistically meaningful history — the
shape a real behavioural-analytics workload actually has.

A small cohort (`ANOMALY_COHORT`) is biased toward high latency so the anomaly
signal the agent surfaces is genuinely present in the data rather than asserted.

Run continuously for a live-streaming demo:
    python src/ingestion/synthetic_generator.py

Or generate a fixed, reproducible dataset and exit:
    python src/ingestion/synthetic_generator.py --seed-batch 120
"""

import argparse
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone

from faker import Faker

from src.settings import LANDING_ZONE

fake = Faker()

EVENT_TYPES = ["click", "purchase", "search", "error"]

# Bounded customer population: ~60 customers means events accumulate into
# histories rather than scattering one-per-id.
CUSTOMER_POOL = [f"CUST_{i:03d}" for i in range(400, 460)]

# Customers whose traffic is biased toward high latency. CUST_404 is the
# headline anomaly referenced throughout the documentation and demo.
ANOMALY_COHORT = {"CUST_404", "CUST_417", "CUST_431"}


def setup_directory() -> None:
    LANDING_ZONE.mkdir(parents=True, exist_ok=True)
    print(f"Landing zone verified at: {LANDING_ZONE}")


def generate_event(event_time: datetime | None = None) -> dict:
    customer_id = random.choice(CUSTOMER_POOL)

    if customer_id in ANOMALY_COHORT:
        # High-latency profile, present ~70% of the time for this cohort so the
        # signal is strong but not perfectly separable.
        latency = round(
            random.gauss(480, 60) if random.random() < 0.7 else random.gauss(60, 12), 2
        )
    else:
        latency = round(random.gauss(50, 10), 2)

    is_pii = random.random() < 0.5
    return {
        "customer_id": customer_id,
        "timestamp": (event_time or datetime.now(timezone.utc)).isoformat(),
        "event_type": random.choice(EVENT_TYPES),
        "context": {
            "latency": max(1.0, latency),
            "pii": is_pii,
        },
        # PII-bearing payloads carry card-like data so the silver-layer masking
        # rule has something real to hash.
        "payload": fake.credit_card_number()
        if is_pii and random.random() < 0.3
        else fake.sentence(),
    }


def write_batch(events: list[dict], batch_num: int) -> str:
    filename = LANDING_ZONE / f"telemetry_batch_{int(time.time())}_{batch_num}.json"
    with open(filename, "w") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
    return str(filename)


def seed_dataset(num_batches: int, batch_size: int = 20, seed: int = 42) -> None:
    """Generate a fixed, reproducible dataset spread over a recent time window."""
    random.seed(seed)
    Faker.seed(seed)
    setup_directory()

    base_time = datetime.now(timezone.utc) - timedelta(hours=num_batches)
    for batch_num in range(1, num_batches + 1):
        batch_time = base_time + timedelta(hours=batch_num)
        events = [
            generate_event(batch_time + timedelta(seconds=i * 3)) for i in range(batch_size)
        ]
        path = write_batch(events, batch_num)
        print(f"Seeded {batch_size} events -> {os.path.basename(path)}")
    print(f"\nSeed complete: {num_batches * batch_size} events across {num_batches} batches.")


def run_generator(batch_size: int = 10, sleep_interval: int = 1) -> None:
    """Continuously stream batches until interrupted."""
    setup_directory()
    batch_num = 1
    try:
        while True:
            events = [generate_event() for _ in range(batch_size)]
            path = write_batch(events, batch_num)
            print(f"Generated {batch_size} events in {os.path.basename(path)}")
            batch_num += 1
            time.sleep(sleep_interval)
    except KeyboardInterrupt:
        print("\nGenerator stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="IntelligenceStack synthetic telemetry generator"
    )
    parser.add_argument(
        "--seed-batch",
        type=int,
        metavar="N",
        help="Generate N reproducible batches and exit (for a fixed demo dataset).",
    )
    parser.add_argument("--batch-size", type=int, default=20, help="Events per batch.")
    args = parser.parse_args()

    if args.seed_batch:
        seed_dataset(num_batches=args.seed_batch, batch_size=args.batch_size)
    else:
        print("Starting synthetic telemetry generator (streaming mode)...")
        run_generator()
