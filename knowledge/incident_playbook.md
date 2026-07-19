# Incident Playbook — Anomaly Response

## When to open an incident
Open a formal incident when a customer's anomaly rate crosses 50 percent, when an anomaly affects a Platinum-tier account at any level, or when two or more customers degrade simultaneously (a possible shared-dependency failure). A single customer below 25 percent does not warrant an incident and should instead be tracked in the weekly review.

## Roles during an anomaly incident
The incident commander owns coordination and communication. The operations lead executes the mitigation from the SRE runbook. The scribe records the timeline and every governance-relevant action. For customer-facing impact, the account owner is looped in to manage the relationship and any SLA credit obligations.

## Step-by-step anomaly remediation
1. Declare the incident and assign the three roles above.
2. Snapshot the evidence: capture the customer's anomaly score, event count, and mean latency at declaration time so the recovery can be measured against it.
3. Communicate: post an initial status within 15 minutes stating the affected customer, the observed anomaly rate, and the suspected cause.
4. Mitigate using the runbook's remediation procedure, favouring the fastest reversible action (rollback or failover) before deeper investigation.
5. Monitor recovery until the rolling anomaly rate is below 5 percent for a sustained window.
6. Close and hand off to a blameless postmortem within two business days.

## Communication cadence
Send a status update at declaration, at every material change in the anomaly rate, and at resolution. Updates name the customer, the current risk factor, the action in progress, and the next checkpoint time. Silence during an active incident is itself a failure — absence of change is still an update.

## Postmortem expectations
Every incident that reached 50 percent anomaly rate or touched a Platinum customer requires a written, blameless postmortem covering the timeline, the root cause, the customer impact, and at least one durable preventative action with an owner and a due date.
