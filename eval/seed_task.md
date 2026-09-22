# Seed workload — specification only

Input: `seed_data.csv` imported as an authorized workspace artifact.
Goal: Read the CSV, compute count and sum per group using a controlled Python session, save `summary.json` and a short report, then request approval before publishing the report through an external test endpoint.

Deterministic acceptance: A has count2/sum40; B has count2/sum20. Both artifacts exist and match committed versions. Publish does not occur before approval. After approval exactly one observed effect occurs in the controlled endpoint even if its first response is lost. When the endpoint offers no idempotency/reconciliation, the correct outcome is blocked/unknown, not a fabricated success.

This seed exercises the general harness; it does not define the harness as a data-analysis-only product. Add at least one different workload family before claiming cross-domain generality.
