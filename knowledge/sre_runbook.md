# SRE Runbook — Customer Latency & Reliability

## Latency service level objectives
The platform targets a p50 request latency of 50 ms and a p95 of 120 ms across the customer fleet. Sustained latency above the fleet mean is the primary early indicator of a degrading customer experience. A customer whose mean latency exceeds three standard deviations of the population baseline is classified as anomalous and must be triaged within one business day.

## Detecting a latency anomaly
Latency anomalies are surfaced by the gold-layer anomaly score, which reports the share of a customer's events that breach the 3σ threshold. A risk factor at or above 50 percent means the majority of that customer's traffic is degraded and should be treated as an active reliability incident, not a passive trend. Risk factors between 1 and 50 percent indicate intermittent degradation that warrants monitoring and a scheduled review.

## Remediation procedure for high-latency customers
When a customer is flagged with a high anomaly rate, follow these steps in order:
1. Confirm the signal: pull the customer's anomaly score and verify the event count is large enough to be meaningful (at least 20 observed events).
2. Isolate the blast radius: determine whether the latency is specific to one event type (for example, checkout) or spread across all of the customer's traffic.
3. Check recent deploys and shared dependencies: correlate the onset of the anomaly with release timestamps and downstream service health.
4. Apply the mitigation: shed load, roll back the implicated release, or fail over to a healthy replica depending on the isolation result.
5. Verify recovery: the incident is resolved only when the customer's rolling anomaly rate returns below 5 percent.

## Escalation thresholds
An anomaly rate above 75 percent, or any anomaly affecting a Platinum-tier customer, is escalated immediately to the on-call SRE lead and the account owner. Anomalies between 25 and 75 percent are handled by the primary on-call engineer during business hours. Below 25 percent, the finding is logged for the weekly reliability review and no page is raised.
