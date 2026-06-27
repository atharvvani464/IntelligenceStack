import os
import json
import time
import random
from datetime import datetime, timezone
from faker import Faker

fake = Faker()

LANDING_ZONE = "/Users/atharvvani/Downloads/DataBricks/mnt/telemetry/raw_logs"

def setup_directory():
    os.makedirs(LANDING_ZONE, exist_ok=True)
    print(f"Landing zone verified at: {LANDING_ZONE}")

def generate_event():
    event_types = ['click', 'purchase', 'search', 'error']
    customer_id = f"CUST_{random.randint(100, 999)}"
    
    # We deliberately create an anomaly scenario for CUST_404
    if customer_id == "CUST_404":
        latency = round(random.gauss(500, 50), 2)  # High latency
    else:
        latency = round(random.gauss(50, 10), 2)   # Normal latency
        
    return {
        "customer_id": customer_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": random.choice(event_types),
        "context": {
            "latency": max(1.0, latency),
            "pii": random.choice([True, False])
        },
        "payload": fake.sentence() if random.random() > 0.1 else fake.credit_card_number()
    }

def run_generator(batch_size=10, sleep_interval=1):
    setup_directory()
    batch_num = 1
    
    try:
        while True:
            events = [generate_event() for _ in range(batch_size)]
            
            # Write batch to JSON lines file
            filename = os.path.join(LANDING_ZONE, f"telemetry_batch_{int(time.time())}_{batch_num}.json")
            with open(filename, 'w') as f:
                for event in events:
                    f.write(json.dumps(event) + "\n")
                    
            print(f"Generated {batch_size} events in {filename}")
            batch_num += 1
            time.sleep(sleep_interval)
    except KeyboardInterrupt:
        print("\nGenerator stopped.")

if __name__ == "__main__":
    print("Starting synthetic telemetry generator...")
    run_generator()
