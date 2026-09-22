# P08 local operations and recovery runbook

This is a **single-node development deployment**, not a production release.
The API admits durable Runs. An opt-in, separate `hnh-dev-worker` process can
advance Runs using the same PostgreSQL database and the one configured
development principal. Do not expose the development bearer token or PostgreSQL directly
to the internet. A configured Docker broker is needed for Python execution; no
broker means an explicit failure, never host `exec`.

`hnh.application.run_worker.RunWorker` is a bounded library component for
native reads and two ledger-backed local writes: `artifact.create` and
conditional `workspace.file.write`. The development entrypoint resolves the current
actor from the local token configuration at each claim; the Run's saved scope
ceiling is not an identity provider. This local configuration is static across
processes and does **not** provide production revocation or OIDC. Restart
both API and worker after changing it. Lease-bound MCP, Python/session and
remote writes are rejected before Action admission, so this process cannot run the full demo's publishing
step. Do not substitute an ad-hoc polling loop around `Runner`.

## Start and inspect

Use Python 3.12 and a PostgreSQL database created specifically for this Harness.
Set secrets in the host environment or a local secret manager, not in this file.

```bash
uv sync --locked --all-extras --dev
export HNH_DATABASE_URL='postgresql+psycopg://localhost/hnh'
export HNH_DEV_TOKEN='<random local token>'
export HNH_BLOB_ROOT='/absolute/path/to/hnh-blobs'
uv run alembic upgrade head
uv run uvicorn hnh.transport.http.app:app --host 127.0.0.1 --port 8080
```

To opt into a separate local worker, configure the **same** database,
`HNH_DEV_TOKEN`, `HNH_DEV_TENANT`, and `HNH_DEV_SUBJECT` as the API process.
Provide `HNH_OPENAI_API_KEY` and `HNH_OPENAI_MODEL` through the local secret
environment, not a checked-in file. In a second terminal:

```bash
uv run hnh-dev-worker
```

When only cancellation confirmation is needed, start
`uv run hnh-dev-worker --cancel-only` with the same database and development
principal; this mode does **not** need model credentials or claim ordinary
`advance_run` jobs. It can confirm Runs with no unresolved effects or children.
For two supported local writes, it can also verify an already committed
PostgreSQL receipt without re-dispatch; missing or mismatched receipts leave
the Run `cancelling`. It is not an external stop/reconciliation service.

`uv run hnh-dev-worker --once` claims at most one due Run job for inspection.
The default job lease is 30 seconds; `--lease-seconds` exists for controlled
fault drills and is not a deployment-tuning recommendation without load tests.
The worker uses native reads plus the two PostgreSQL-ledger-backed local writes
for model actions; unsupported leased effects block the Run before dispatch.
For supported local writes, retry after a lost Action receipt uses the same
Action business key; this does not assert exactly-once for external APIs or
the Blob filesystem. Configure the **same** `HNH_BLOB_ROOT` for API and worker
so worker-created Artifacts remain readable by the API. It also claims
`cancel_run` jobs and can confirm a child, then its parent, when neither has
unresolved Actions. An unknown remote effect or local write without a matching
receipt leaves the cancellation job deferred pending evidence; it is never
treated as a confirmed remote stop. The local receipt proves the database-side
effect, not indefinite availability of an external Blob file.
A missing database or token causes exit code 2; normal advancement mode also
requires model configuration. Missing values are not logged as secrets. The API still
returns 202 only after admission commits; the worker may be absent, in which
case the Run remains queued. There is no externally supervised restart policy,
production actor resolver, managed secret refresh, or general side-effect
recovery scheduler in this deployment.

`GET /healthz` is only liveness. `GET /readyz` checks actual DB connectivity
and reports whether model, isolated broker, blob store, and MCP integrations are
configured. A `configured` result is **not** proof the remote service is alive.
`GET /metrics` requires `metrics:read`; labels are route templates, methods,
and status classes only. Its counters reset on process restart. Each HTTP
response has a W3C-style `traceparent` header; export is opt-in.
PostgreSQL Events, not those process metrics, are execution truth.

Set `HNH_OTLP_TRACES_ENDPOINT` to an administrator-controlled OTLP/HTTP
`/v1/traces` URL to enable batched span export. HTTPS is required except for
loopback HTTP during local tests; URLs with embedded credentials, query strings,
or fragments are rejected. The exporter does not follow redirects. Spans use
route templates, method and status, never raw paths, queries, tokens or bodies.
When unset, the API still returns a correlation `traceparent` but emits no
exported spans. The local receiver test proves protobuf delivery; no external
collector, dashboard or cross-service trace has been exercised.

## Optional single-node Compose topology

`deploy/dev/compose.yaml` provides a reproducible **development-only**
PostgreSQL + one-shot migration + API + bounded worker topology. The Python
3.12.14 and PostgreSQL 14.18 base images use verified multi-platform digests;
the app dependencies come from `uv.lock`. API traffic binds only to host
`127.0.0.1` (default port 8080); PostgreSQL is not published. API, worker and
migration containers run without root, with a read-only root filesystem,
`no-new-privileges`, dropped Linux capabilities and no Docker socket mount.
The broker is deliberately absent; code execution therefore fails closed.

Copy `deploy/dev/.env.example` to `deploy/dev/.env`, replace every placeholder
with local values, and keep the file outside version control. The DB password
must use URL-safe ASCII because Compose interpolates it into the connection
URL. Docker administrators can inspect container environment values; do not
use this layout for production credentials or multi-tenant/public deployment.

```bash
docker compose --env-file deploy/dev/.env -f deploy/dev/compose.yaml up --build -d --wait
curl -fsS http://127.0.0.1:8080/readyz
docker compose --env-file deploy/dev/.env -f deploy/dev/compose.yaml ps --all
docker compose --env-file deploy/dev/.env -f deploy/dev/compose.yaml down
```

Plain `down` retains the database and Artifact volumes. `down --volumes`
**deletes their contents** and should only be used for explicitly disposable
test stacks. The migration container must exit successfully before the API
starts, and the worker starts only after API readiness. The worker remains
the same one-principal development component described
above; Compose does not add OIDC, MCP lease dispatch, real model validation,
or production supervision.

Only the worker receives the model API key and model configuration. The API
container deliberately has no model credential; its `/readyz` response reports
`model_configuration=missing` while database-backed core readiness can still
be ready. Do not interpret that API-local model check as worker health or as
evidence of a successful model call.

`bash scripts/check_dev_compose.sh` runs a separate, self-cleaning smoke
project with placeholder model credentials. It builds the image, starts the
four services, checks DB-backed readiness and authorized capability discovery,
then removes only its own containers/network/test volumes. It **does not**
create a model Run or call OpenAI. The same smoke runs in CI after the
PostgreSQL/Docker regression. It passed locally on Docker Desktop; the
GitHub-hosted workflows also passed this smoke on runs `35765430976` and
`35766185495`; the latter used Node 24 actions pinned by full commit SHA. These
runs used placeholder model configuration and remain deployment wiring
evidence, not live provider or production deployment results.

## Back up and restore

1. Stop API writes and all workers before backup. The helper cannot freeze an
   independently running writer and therefore does not claim online consistency.
2. Preserve the **database and Blob root together**. Never copy only one.
3. Run a backup into a new path; it refuses to overwrite an existing backup:

```bash
uv run python scripts/backup_harness.py \
  --blob-root '/absolute/path/to/hnh-blobs' \
  --destination '/absolute/path/to/backups/hnh-YYYYMMDD'
```

4. Provision a new, empty PostgreSQL database and an empty Blob directory.
   Set `HNH_RESTORE_DATABASE_URL` for that new database, then restore:

```bash
export HNH_RESTORE_DATABASE_URL='postgresql+psycopg://localhost/hnh_restore'
uv run python scripts/restore_harness.py \
  --source '/absolute/path/to/backups/hnh-YYYYMMDD' \
  --blob-root '/absolute/path/to/hnh-restored-blobs'
```

5. Keep workers stopped. Review the reported active Runs and
   `outcome_unknown` Actions. Reconcile unknown external effects from durable
   handles/receipts before any retry. Do not automatically reissue unsafe writes.
6. Check the restored `alembic_version`, read representative Run/event history
   and Blob content, then point a private API instance at the restored database
   and Blob root. Test readiness before reopening admissions.

The backup format contains a custom `pg_dump`, content-addressed Blob files,
and SHA-256 checksums. Checksums detect accidental corruption, not malicious
tampering; encrypt and control access to the backup using the host's backup
system. The helper does not back up externally managed credentials or a live
Python process. Restored execution sessions may need environment-loss handling.

## Upgrade and incident response

Quiesce new requests, finish or pause actions at durable boundaries, back up,
run `uv run alembic upgrade head`, then run schema drift and acceptance checks
before restarting API/worker components. Test migration downgrade only in a
temporary database, not on production data. If the database is unavailable,
new Run admission must fail. If a side effect's response is lost, retain
`outcome_unknown` and require downstream query/idempotency proof or operator
reconciliation. Closing SSE does not cancel a Run.

The P08 `admitted_scopes` migration assigns pre-existing Runs an empty scope
ceiling. Do not start an automatic worker that treats those Runs as authorized
to perform tools. Review or recreate them through a trusted admission path;
the stored ceiling is never a replacement for fresh actor authentication.
Context snapshots created before the scope-fingerprint guard cannot be resumed
as if their old catalog were still authorized; the Runner returns
`migration_required` for that turn. Review it and start a newly authorized Run
instead of editing the stored snapshot or silently broadening its scopes.

The tested recovery drill is
`scripts/test_with_postgres.sh tests/integration/test_p08_ops.py -q --tb=short`.
It creates a second temporary PostgreSQL database, restores active/terminal Run
state and a Blob, verifies an unknown unsafe Action cannot be re-dispatched,
and removes only that test-created database afterward.

## Local CI evidence gate

The GitHub workflow runs the complete regression with `--junitxml`, then runs
`scripts/check_ci_test_report.py` against `reports/acceptance_status.json` and
the exact 68-ID specification in `eval/acceptance_cases.yaml`.
The checker requires every passed AT's mapped tests and every blocked AT's
partial tests to be collected and passing; only explicitly mapped blocked
live tests may be skipped. A missing mapping, unexpected skip, failed test,
or stale 68-case summary makes the gate fail. It preserves the JUnit report
as a CI artifact. It does not turn blocked live AT-030/065/068 into passes.
On tag refs, a separate strict release invocation adds
`--require-release-ready`; it must fail while any AT remains blocked.

To reproduce locally, use a fresh JUnit filename under a writable temporary
directory, then run:

```bash
scripts/test_with_postgres.sh --cov --cov-report=term \
  --junitxml=/absolute/path/to/fresh-junit.xml
uv run python scripts/check_ci_test_report.py \
  /absolute/path/to/fresh-junit.xml reports/acceptance_status.json \
  --spec eval/acceptance_cases.yaml
```

This gate has been exercised locally and on GitHub-hosted runs `35765430976`
and `35766185495`.
The hosted runner pulled the digest-pinned amd64 Python sandbox image, completed
the PostgreSQL/Docker regression, passed the 68-ID acceptance execution gate,
the runtime API contract check and the development Compose smoke. The strict
release gate remains tag-only and is expected to reject a release while
AT-030/065/068 are blocked.

## Opt-in live evaluation (not production readiness)

Use a separately migrated PostgreSQL database and a fresh raw JSONL path for
each campaign. Configure `HNH_OPENAI_API_KEY`, `HNH_OPENAI_MODEL`,
`HNH_EVAL_TENANT`, `HNH_EVAL_SUBJECT`, and `HNH_DATABASE_URL` in a local secret
environment; do not paste keys into commands, reports, or chat. Also set
`HNH_EVAL_IMPLEMENTATION_REVISION` to the exact Git commit, source archive
digest, or deployed image digest being evaluated. It permits only a compact
non-secret identifier; values such as `latest` or an uncommitted description
are not useful reproduction evidence. The raw rows contain a SHA-256
`environment_hash`, not the revision text or any credential. They also contain
`evidence_tier=live_provider`; the live entrypoints refuse the all-zero
controlled-test environment sentinel. Local mocked/loopback fixtures write
`evidence_tier=controlled`, and the summarizer rejects mixed tiers so controlled
evidence cannot be reported as a live campaign. For the
three-task file-read/transform/artifact suite, also set
`HNH_EVAL_TASK_OUTPUT=/absolute/new/path/tasks.raw.jsonl`. Optionally set
`HNH_EVAL_BLOB_ROOT` to a dedicated Blob directory. Then run:

```bash
uv run python eval/run_live_tasks.py
uv run python eval/summarize.py "$HNH_EVAL_TASK_OUTPUT"
```

The task entrypoint seeds three managed files through the trusted Gateway,
admits three model Runs, and grades committed read/create Actions plus exact
stored Artifact bytes. The matching read must precede artifact creation by a
separate model decision turn. A zero exit code from the entrypoint means raw records
were written, **not** that the model passed all tasks. Inspect the summary's
`design_complete`, denominators, false completions, failures, timeouts,
blocked cases, and provider usage samples. A missing/duplicate/mixed task row
makes the summarizer exit nonzero; a `verified_completion` value inconsistent
with the raw outcome and evidence is rejected. Existing raw output is never overwritten.
Capability revisions, fixture hash, actor scopes and policy revision are
preflight-checked before fixture seeding and again before each task Run;
design drift stops the campaign instead of counting as a model failure.
If the evaluator stops after admitting a task Run, it records a blocked Run
with `evaluation_stopped` rather than leaving it dispatchable or claiming that
any committed local effect was rolled back. Review its Run/Action history
before starting a new campaign; do not resume it with the development worker.

For an independent skills off/on ablation over the native read-only echo,
also set `HNH_EVAL_SKILLS_OUTPUT=/absolute/new/path/skills.raw.jsonl` and run:

```bash
uv run python eval/run_live_skills.py
uv run python eval/summarize.py "$HNH_EVAL_SKILLS_OUTPUT"
```

The on condition loads only the checked-in allowlisted
`eval/fixtures/echo_guidance.md`; the off condition loads no skill. Both use
the same model interface, goal, scope, budget and ActionGateway. The skill
content hash is pinned in the shared controls. Inspect both condition
denominators, all failures and provider-reported usage; a successful local
mocked run does not establish a real-model skill benefit. This one skills
ablation does not evaluate classifier or child-run benefit.

The separate four-arm echo comparison additionally requires an independent
HTTPS MCP echo endpoint and issuer-pinned OAuth client configuration as
described by `eval/run_live_echo.py`. The discovered remote tool must accept
the canonical native echo domain: one required string `message` of 1–100
characters, and must explicitly declare the corresponding required string
output. Preflight rejects an
incompatible or narrower contract before creating a raw file, Run or model
call and pins the discovered revision for the campaign. Successful
model-visible capability IDs, functions, paths, schemas and echo observations
are the same across downstream adapters. The MCP alias is resolved only after
Action admission; persisted RPC fingerprints and Action receipts retain the
real remote binding and protocol result. Neither
local mocked test is a live benchmark, and neither evaluation entrypoint is
a supervised production worker.
The campaign also refuses a control record whose capability revision differs
from that preflight pin before admitting any Run.
