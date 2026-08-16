# Data Governance Policy

## Scope and intent
This policy governs how customer telemetry and derived analytics are stored, accessed, and exposed to automated systems, including AI agents. Its purpose is to ensure that the value of enterprise data is available for analysis without widening the surface through which sensitive data can leave the perimeter.

## PII handling and masking
Any event payload that carries personally identifiable information is hashed with SHA-256 at the silver layer during ingestion. Raw PII is never materialised into the gold analytical layer and is never returned to an AI agent. Analytical questions are answered from aggregated, de-identified features only.

## Agent access model
Automated agents operate under least privilege. An agent is never granted direct table access or the ability to compose arbitrary SQL. It may invoke only the pre-approved functions registered in the catalog allowlist, and every invocation is checked against that allowlist, its declared parameter schema, and a control-sequence filter before execution. A request that does not map to an approved function is refused at the boundary and recorded.

## Entitlement of the requesting principal
Least privilege applies to the person as well as the agent. Every request carries an authenticated principal, and that principal's entitlements are evaluated independently of the agent's allowlist: the caller must hold a grant on the specific function, and the record requested must fall inside the caller's assigned scope. An analyst assigned a book of accounts may use every analytical tool against that book and none outside it. A compliance reviewer may hold full access to the governance trail while holding no access to customer data at all. An unrecognised principal is refused every capability rather than defaulted to a shared identity.

## Retrieval of unstructured knowledge
Enterprise documents made available for retrieval-augmented answers are treated as governed sources. Retrieval returns only document passages that have been approved for internal circulation, and every answer that draws on a document must cite the source it used. Retrieval queries are bound values used solely for ranking; they are never interpreted as commands.

## Audit and retention
Every agent action — the resolved intent, the governance decision, and the outcome — is recorded to support audit. Governance denials are retained as first-class evidence of controls operating as designed. Telemetry is retained according to the standard analytical retention window, after which it is aggregated and the underlying events are expired.
