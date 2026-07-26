# Customer Tiering Handbook

## Tier definitions
Customers are assigned to one of three tiers that govern their service commitments and the urgency of their incidents. Platinum customers are strategic accounts with contractual uptime and latency guarantees. Gold customers are established paying accounts with standard support. Silver customers are self-serve or trial accounts without individual guarantees.

## Latency commitments by tier
Platinum accounts carry a contractual p95 latency ceiling of 100 ms and a same-hour response commitment for any reliability incident. Gold accounts target a p95 of 150 ms with same-business-day response. Silver accounts are served on a best-effort basis with no individual latency guarantee, though they still contribute to the fleet baseline used for anomaly detection.

## What "anomalous" means per tier
The statistical definition of an anomaly — an event beyond three standard deviations of the fleet latency baseline — is identical across tiers, but the response is not. For a Platinum customer, any sustained anomaly is an incident regardless of the percentage. For Gold, an incident opens at 50 percent. For Silver, anomalies are aggregated into the weekly reliability review unless they indicate a shared-dependency failure.

## SLA credits and obligations
When a Platinum or Gold customer experiences a reliability incident that breaches their latency commitment, the account owner evaluates SLA credit eligibility during the postmortem. Credits are calculated from the incident window captured at declaration time, which is why snapshotting the anomaly score and event count at the start of an incident is mandatory.

## Escalation ownership
Each tier maps to an escalation path. Platinum incidents page the SRE lead and the named account owner immediately. Gold incidents page the primary on-call during business hours. Silver findings are triaged asynchronously by the reliability review rota.
