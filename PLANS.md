# Living implementation plan

Status: implementation in progress. P00 through P02, P04, P05, P06's local
acceptance, and P07's local
acceptance slice are complete. P03's
local implementation and AT-022–AT-029 gates pass; its stage exit is blocked only
by the explicitly configured live-provider AT-030. P04 was implemented and tested
independently without substituting sandbox evidence for that missing model evidence;
P05 likewise does not treat its deterministic effect driver as a live model or remote
service integration. P06 has a tested local official-SDK/official-server adapter,
request-scoped trusted integration composition, and issuer-pinned OAuth
client-credentials fixtures; independent remote and managed-secret evidence
remain outside its local compatibility claim.
P08 has local evaluation-recording and operations slices plus an opt-in
real-model four-arm echo entrypoint. Its entrypoint passed a real-PostgreSQL,
mock-model/local-MCP wiring test, not a live run. Live four-arm results,
production worker/deployment integration, and release gates remain incomplete.
An opt-in single-principal development worker process now exercises
API→PostgreSQL→worker with a controlled model fixture, but this is not
production identity or live-model evidence.
The original design files remain specifications, and only behavior named in
implementation reports is implemented.

## North star
An independent HTTP-native harness with one durable state owner, controlled side effects and evidence-backed completion. MCP is an adapter, not the core protocol.

## Progress

| Stage | Status | Evidence |
|---|---|---|
| P00 | complete | `reports/implementation/P00.md`; AT-001/002 passed |
| P01 | complete | `reports/implementation/P01.md`; AT-003–011 passed on PostgreSQL |
| P02 | complete | `reports/implementation/P02.md`; AT-012–021 passed on PostgreSQL |
| P03 | blocked_environment | `reports/implementation/P03.md`; AT-022–029 passed; AT-030 blocked |
| P04 | complete | `reports/implementation/P04.md`; AT-031–038 passed on PostgreSQL and real Docker where required |
| P05 | complete | `reports/implementation/P05.md`; AT-039–052 passed on PostgreSQL |
| P06 | complete_local | `reports/implementation/P06.md`; AT-053–059 passed within the reported local interoperability/security scope |
| P07 | complete_local | `reports/implementation/P07.md`; AT-060–064 passed with scripted model and real PostgreSQL; no live TypeSafe claim |
| P08 | blocked_environment | `reports/implementation/P08.md`; local machinery and AT-066/067 pass; AT-065/068 require live model/remote service evidence |

## Current task

P08 local implementation and repeatable evidence machinery are complete within
the available environment. Its stage exit is blocked on AT-065/068 live evidence;
do not invent it. P06's local OAuth
issuer guard now passes, but a managed credential backend, refresh drill and
independent remote interoperability remain future production-hardening work.
P08 still needs a production-configured model worker,
four live comparable model/downstream arms, raw live task runs, deployment and CI
exercise, and complete observability/operational gates. The environment has no
`HNH_DEEPSEEK_API_KEY` or independent MCP/OAuth endpoint. DeepSeek is now the
configured live provider; TypeSafe remains an optional disabled P07 adapter.
The opt-in live echo entrypoint has four fixed input variants of one read-only
capability. A separate three-task read/transform/artifact suite is now
implemented for AT-068 and passed only with a mocked model. Neither entrypoint
has produced real-model raw data; echo alone cannot prove the broader gate.
The local P07 integration tests and P08 backup/readiness tests pass, but scripted
providers and controlled executors are not live evidence. Keep `reports/implementation`
and `reports/acceptance_status.json` synchronized with each completed gate.
Evaluation deadline checks now occur after each synchronous Runner call as
well as before it. Late evidence is recorded with the committed Run state and
counted as timeout, not an on-time success or an unsupported claim of active
request preemption.
A bounded worker now resolves a current actor via an injected trusted port,
renews/fences `advance_run` and `cancel_run` leases, handles native reads and
two ledger-backed local writes, and confirms cancellation only after local
child/action evidence permits it.
An explicit `--cancel-only` mode can process those confirmed-stop jobs without
model credentials and never claims ordinary advancement jobs. It can now also
read an already committed PostgreSQL receipt for an `artifact.create` or
conditional file write whose Action result was lost, and confirm cancellation
without re-dispatching the write. Missing/mismatched receipts remain unresolved.
The opt-in `hnh-dev-worker` entrypoint is connected to the API's shared
PostgreSQL in a single-principal development topology, but not to a
production identity source or supervisor. Leased MCP, Python/session and
remote writes remain fail-closed until their effects can be fenced/reconciled.
Unresolved external effects still require adapter-specific stop/reconciliation;
the cancellation worker does not prove remote cancellation.

## Required per-stage plan format
Purpose and observable behavior; current repository facts; dependencies; exact files to change; implementation steps; commands and expected results; real results; AT coverage; decisions and deviations; environmental blockers; safe next action. Keep each active plan self-contained enough to resume after context loss.

## Discoveries / decision log

- 2026-09-22: the supplied directory was a design-only package and not a Git
  repository. A repository and Python 3.12/uv baseline were created.
- 2026-09-22: P00 passed 103 design checks and 17 runtime/contract tests. The ASGI
  app exposes only `/healthz`; all 23 target operations remain unadvertised.
- 2026-09-22: `mcp==2.2.0` imports `Client` from `mcp.client` and `MCPServer` from
  `mcp.server.mcpserver`; package-root `MCPServer` import failed. Upstream still
  lists Tasks as unimplemented, so P06 must not infer support from protocol date.
- 2026-09-22: Docker client 29.1.3 is installed but its daemon is stopped. Local
  PostgreSQL 14.18 binaries exist, so P01 will use an isolated temporary cluster
  if it can be started safely.
- 2026-09-22: initial P01 PostgreSQL run failed 9/10 because SQLAlchemy attempted a
  Budget FK insert before its independently mapped Run. Explicitly flushing the Run
  root inside the same transaction fixed ordering without weakening the FK; targeted
  P01 then passed 10/10 and the full suite passed 63/63.
- 2026-09-22: P01 migration upgrade/downgrade/upgrade and `alembic check` passed on
  temporary PostgreSQL 14. The current runtime advertises three Run operations and
  keeps the other 20 target operations absent.
- 2026-09-22: P02 added a scope-filtered stable Capability registry, schema export,
  ResourceDispatcher/ActionGateway, immutable PostgreSQL artifacts, and transactional
  workspace version pointers. Direct HTTP effects are represented by one Action in a
  terminal implicit Run; read-only GETs bind/authorize without creating business state.
- 2026-09-22: concurrent If-Match writes use a deterministic PostgreSQL advisory lock
  plus row lock; two writers against v1 produced one 200 and one 412. The P02 migration
  round-trip and Alembic drift check passed. Runtime OpenAPI now advertises 13 of 23
  target operations and no unexpected operations.
- 2026-09-22: P03 added complete-response-first ModelCall persistence, immutable
  context snapshots, bounded format repair/no-progress termination, stable model
  Action slots, concurrent budget reservations, CompletionGate records, and committed
  event history/SSE replay. The new migration passed empty upgrade, full downgrade,
  re-upgrade, and Alembic drift checks on PostgreSQL 14.
- 2026-09-22: P03 full PostgreSQL regression collected 84 tests: 83 passed and the
  live-provider AT-030 was skipped with `blocked_environment`; branch-aware coverage
  was 85%. Runtime OpenAPI advertises 15 of 23 target operations with no unexpected
  operations or operation-ID mismatch.
- 2026-09-22: P04 added fail-closed execution ports, a trusted-profile Docker broker,
  durable execution session/generation/cell records, per-session serialization,
  explicit environment-loss handling, and content-addressed atomic filesystem blobs.
  The broker fixes the image by digest and applies non-root, no-network, read-only-root,
  capability, CPU, memory, PID, file-descriptor, timeout, output, artifact, and workspace
  limits without accepting caller-supplied Docker flags or mounts.
- 2026-09-22: AT-031–AT-038 passed with real PostgreSQL and, for AT-032–AT-036,
  Docker Engine using the locally cached pinned Python image. The complete P00–P04
  run collected 94 tests: 93 passed and only live-provider AT-030 was skipped;
  branch-aware coverage was 84%. Migration round-trip/drift checks passed, runtime
  OpenAPI advertises 18 of 23 target operations, and no managed container remained.
- 2026-09-22: P05 added durable exact-bound approval/clarification requests,
  one-response CAS and expiry, dispatch-time authorization rechecks, four-class effect
  recovery, persistent attempt/reconciliation receipts, lease epochs/fencing, cautious
  cancellation, checkpoint compatibility blocking, centralized redaction, and a
  destination-pinning egress policy that revalidates DNS and redirects.
- 2026-09-22: AT-039–AT-052 passed against real PostgreSQL with a deterministic
  in-process fault effect driver. The complete P00–P05 run collected 111 tests:
  110 passed and only live-provider AT-030 was skipped; branch-aware coverage was
  82%. Migration round-trip/drift checks passed and runtime OpenAPI advertises 21 of
  23 target operations. The fault driver and deterministic DNS resolver are not
  represented as a real remote service, packet capture, or production egress broker.
- 2026-09-22: P06 added an optional official-SDK MCP adapter with explicit modern
  2026-07-28 and legacy 2025-11-25 profiles, private capability import, complete
  result classification, durable MRTR continuation/Tasks handles, and a local
  issuer/resource/subject credential binding guard. In-process official MCPServer
  and Streamable HTTP ASGI interoperability tests pass. The installed SDK has no
  bundled Tasks client, so the project supplies a formal draft extension instead
  of claiming upstream Tasks implementation.
- 2026-09-22: P06 local tests passed 10/10; full P00–P06 regression collected
  121 tests: 120 passed, AT-030 skipped; branch-aware coverage 83%. PostgreSQL
  migration round-trip and drift checks passed. A stale task poll worker was fenced
  before remote request, and an initially terminal task committed exactly once.
  AT-059 remains incomplete: there is no real OAuth issuer metadata evidence or
  production credential backend, and the default ASGI app does not yet compose
  configured MCP integrations dynamically.
- 2026-09-22: P06 credential follow-up added a trusted credential-provider port,
  expiry/revocation checks, and an injectable MCP HTTP transport for controlled
  interoperability. Local ASGI HTTP tests confirmed per-resource bearer forwarding,
  no cross-origin redirect follow, and private catalog cache partitions by tenant,
  subject, scope and policy revision. Credential lookup also requires the exact
  tenant, closing cross-tenant subject-ID reuse. The P06 suite now passes 14/14; the full
  regression collected 125 tests, 124 passed and AT-030 skipped, at 83% coverage.
  This does not supply production OAuth, independent issuer metadata testing, or
  connection-pinned egress.
- 2026-09-22: P06 trusted request-scoped `MCPIntegrationManager` now composes
  approved bindings into both catalog and execution paths. The local suite passed
  15/15. AT-059 remains incomplete because issuer-pinned OAuth metadata/refresh,
  a production secret store, and an independent remote matrix are absent.
- 2026-09-22: P07 added an optional guarded decision port, SHA-pinned allowlisted
  skills, and transactionally admitted child Runs with scope intersection and
  finite quota reservations. Parent cancellation propagates and terminal guards
  prevent an active child from being orphaned. Cost and remaining wall-time
  bounds are required when the parent has them. The targeted real-PostgreSQL
  suite passed 9/9; AT-060–064 have local evidence. No live TypeSafe claim.
- 2026-09-22: P08 added strict four-arm/off-on evaluation recorders and honest
  denominator/usage summaries, scoped low-cardinality metrics and readiness,
  an offline DB+Blob backup/empty-target restore path, and separate CI integration
  gates. A real second-PostgreSQL-database restore of active/terminal Runs, a Blob
  and an unsafe unknown Action passed; ready-to-running redispatch was refused.
  AT-066/067 pass locally; AT-065/068 are blocked_environment and unrun
  pending actual live arms.
  The full suite before the final P07/ops assertions had 141 collected, 140
  passed, AT-030 skipped, one warning, and 83% coverage. Targeted tests passed
  after those assertions; repeat the full suite for a final baseline.
- 2026-09-22: P06 OAuth follow-up used the installed official SDK
  `ClientCredentialsOAuthProvider` with a pinned issuer, exact actor/resource
  credential vault port and token storage port. An official MCPServer was
  discovered through a local OAuth challenge; separate mock-transport tests
  rejected an evil advertised issuer and mismatched issuer metadata before
  sending client secrets. Targeted P06 integration passed 16/16 and OAuth unit
  tests passed 4/4. AT-059 is accepted only for this local scope, not for a
  managed remote issuer, refresh, or production secret storage.
- 2026-09-22: P08 added a native Responses `function` tool view generated from
  the same authorized Capability schemas as HTTP-semantic operations. The
  provider decodes completed calls only; Runner persists the call before the
  common ActionGateway dispatches. Ten unit tests with MockTransport and
  one real-PostgreSQL function-call integration test passed. This is not a live
  model or four-arm benchmark. Both Responses adapters now reject incomplete
  outputs before action dispatch. The final P00–P08 regression collected 161
  tests: 160 passed, AT-030 skipped for missing live model configuration,
  one Starlette deprecation warning, 83% branch-aware coverage. Migration
  round-trip/drift, 103 static design checks, Ruff, mypy (58 source files),
  and the 21-implemented/2-unimplemented runtime contract check passed.
- 2026-09-22: P08 added a test-only localhost HTTP effect server. It persisted
  a publication in its own ledger before closing the first TCP response. A
  real-PostgreSQL Action became blocked/outcome_unknown, could not be
  redispatched after rebuilding RunController, and was reconciled by querying
  exactly one remote effect. Targeted network test passed 1/1; full P00–P08
  regression collected 162 tests: 161 passed, AT-030 skipped, one Starlette
  warning, 83% branch-aware coverage; migrations round-trip/drift passed. This
  strengthens fault evidence but does not close AT-065/068 live evaluation.
- 2026-09-22: A controlled four-arm PostgreSQL integration test passed 1/1:
  native-function and HTTP-semantic model interfaces each dispatched through
  native echo and an official in-process MCP Server echo, with identical goal,
  model name, output limit, scopes, input and normalized read-only result.
  The model was MockTransport-backed and MCP was local; AT-065 remains unrun
  until an actual model and independent service produce raw evaluation rows.
- 2026-09-22: After the four-arm test, the latest full PostgreSQL/Docker
  regression collected 163 tests: 162 passed, AT-030 live model skipped,
  one Starlette warning, 83% branch-aware coverage. Migration round-trip and
  drift checks, Ruff, mypy, 103 design checks and runtime contract checks pass.
- 2026-09-22: P08 now has an opt-in `eval/run_live_echo.py` entrypoint for an
  actual Responses model and independently configured HTTPS/OAuth MCP echo
  server. It creates four separate read-only Runs, routes each through the
  common gateway, checks a committed echo Action and CompletionGate, and
  records provider-reported usage when present. Missing credentials fail
  closed before any raw file is created. A mocked Responses/local official
  MCPServer test passed 1/1 with real PostgreSQL; this is entrypoint wiring,
  not AT-065/068 live evidence. The subsequent full regression collected 165
  tests: 164 passed, AT-030 skipped for missing real model configuration, one
  Starlette warning, 83% branch coverage; migration round-trip/drift passed.
  Ruff, mypy (59 source files), 103 static design checks and the runtime
  contract check (21 implemented/2 unimplemented) also passed.
- 2026-09-22: Safe-worker prerequisites added: Run admission now persists a
  trusted scope ceiling, applied on every effective-context calculation and
  inherited by child Runs. A new migration gives pre-existing Runs an empty
  ceiling instead of granting future worker privileges. ModelCall begin,
  complete, failure and processing commits can now be fenced to the current
  `advance_run` job lease; a real-PostgreSQL test rejected the old worker's
  late response after lease reclaim and accepted worker two's new attempt.
  This is not a runnable worker: the Runner/Gateway, trusted current-actor
  lookup, renewal and recovery scheduling are not yet integrated. The full
  regression collected 167 tests: 166 passed, AT-030 skipped for missing
  live model config, one Starlette warning and 83% branch coverage; migration
  round-trip/drift checks passed.
- 2026-09-22: Run-job fencing now reaches the ActionGateway's Action admission,
  ready-to-running CAS and result commit for the read-only native slice.
  `advance_run` jobs are checked against their Run; existing action-specific
  job fencing remains intact. A reclaimed lease rejects stale admission and
  late result commits, while a new worker cannot redispatch a running Action.
  Lease-bound non-read-only and MCP operations fail closed until their adapters
  support leased effect receipts. Context snapshots now pin the effective
  scope fingerprint and refuse reuse after scope revocation. These are tested
  worker prerequisites, not an automatic worker or AT-065/068 live evidence.
  Full regression collected 169 tests: 168 passed, AT-030 skipped for absent
  real model settings, one Starlette warning, 82% branch coverage. Eight-step
  migration round-trip/drift, Ruff, mypy (59 files), 103 static design checks
  and runtime contract check (21 implemented/2 unimplemented) passed.
- 2026-09-22: P08 added a bounded `RunWorker` over real PostgreSQL jobs. It
  resolves current actor identity through an injected trusted resolver,
  heartbeat-renews its lease across blocking model calls, advances one turn,
  and durably defers retries instead of hot-looping. A scope-change mismatch
  blocks the Run; an unsupported leased write now blocks before Action
  admission rather than being swallowed as an ordinary tool failure. Six
  targeted PostgreSQL worker tests pass. This is not wired into an API/worker
  deployment, does not provide a production identity source, and cannot
  dispatch leased MCP or writes. Live AT-065/068 remain unrun.
  The final full regression collected 178 tests: 177 passed, AT-030 skipped
  for absent live credentials, one Starlette warning, 83% branch-aware
  coverage. PostgreSQL migration round-trip/drift, Ruff, mypy (60 source
  files), 103 design checks and runtime contract checks passed. The targeted
  worker suite passed 6/6.
- 2026-09-22: The worker's ContextSnapshot/skill pin and model/tool budget
  reserve/settle/release transactions now take the same `advance_run` lease
  fence before mutating Postgres. A reclaimed worker was denied each kind of
  late write; the successor committed normally. Targeted worker/fencing
  integration passed 12/12 on PostgreSQL with migration round-trip/drift.
  Final full regression collected 179 tests: 178 passed, AT-030 skipped for
  missing live model settings, one Starlette warning, 83% branch-aware
  coverage. Migration round-trip/drift, Ruff, mypy (60 source files), 103
  design checks and the runtime contract check passed.
- 2026-09-22: Added an opt-in `hnh-dev-worker` CLI using the same local
  development principal and database configuration as the API. It refuses
  startup without DB/token/model settings and remains limited to native
  read-only effects. A real PostgreSQL test admitted via API and completed
  via a separate worker OS process against a localhost mock Responses HTTP
  service; the three targeted entrypoint tests passed (one Starlette warning).
  `uv sync --locked --all-extras --dev` installed the entrypoint from the
  existing 71-package lock; running it without configuration exited 2.
  This is a local development topology, not live model evaluation or
  production actor revocation, MCP, or write recovery.
- 2026-09-22: An additional real-process integration launched Uvicorn API
  and `hnh-dev-worker` as separate OS processes with a real PostgreSQL
  database. Localhost HTTP observed queued→succeeded after one mock Responses
  call. Targeted test passed 1/1, migration round-trip/drift passed. This
  strengthens the single-node development deployment evidence but is still
  not real model or independent MCP evidence.
- 2026-09-22: A real worker OS process was terminated after model request
  dispatch but before response persistence. A new process reclaimed the
  three-second test lease, recorded a second model attempt and completed the
  Run with no Action replay. The process-topology suite passed 2/2 against
  real PostgreSQL and a localhost mock model service (not a live provider).
  Final full regression collected 184 tests: 183 passed, AT-030 skipped for
  absent real model configuration, one Starlette warning, 83% branch-aware
  coverage. Migration round-trip/drift, Ruff, mypy (61 source files), 103
  design checks, runtime contract check and 68-case status consistency passed.
- 2026-09-22: The P08 2×2 recorder now rotates the four arms by case under a
  fixed seed, recording case/arm positions. Four cases place each arm in each
  position once. The summarizer now reports per-arm denominators, outcomes,
  provider-usage samples and latency p50/p95 with sample counts. A truncated
  or duplicate case-arm block sets `design_complete=false`; the CLI exits 2.
  P08 evaluation unit tests passed 8/8, and the controlled PostgreSQL/MCP
  four-arm integration tests passed 2/2. This improves validity controls but
  supplies no live model evidence; the echo-only entrypoint remains
  insufficient for varied AT-068 task-suite coverage.
- 2026-09-22: Full regression after the evaluation-method changes collected
  188 tests: 187 passed, 1 skipped (AT-030 live model absent), one Starlette
  deprecation warning, 83% branch-aware coverage in 58.87s. PostgreSQL
  migration upgrade/downgrade/re-upgrade and Alembic drift checks passed.
  Ruff, mypy (61 source files), 103/103 static design checks, runtime
  contract check (21 implemented, 2 unimplemented, none unexpected), and
  acceptance-status JSON consistency also passed. These remain local/mock
  results, not live AT-065/068 evidence.
- 2026-09-22: The raw 2×2 summarizer now rejects a nominally complete four-arm
  set if control hashes, case oracles, case indices, seed/order positions, or
  labeled/unlabeled rows disagree. The opt-in echo fixture now has four fixed
  input variants, yielding 16 case-arm Runs. Evaluation/configuration unit
  tests passed 12/12; its real-PostgreSQL, mocked-Responses/in-process-MCP
  integration passed 1/1 with migration round-trip and drift checks. This
  remains a single read-only capability family and not live AT-065/068 data.
- 2026-09-22: Full regression after the four-case fixture and raw-data
  integrity gate collected 191 tests: 190 passed, 1 skipped (AT-030 real
  model absent), one Starlette deprecation warning, 83% branch-aware
  coverage in 55.47s. PostgreSQL migration round-trip/drift, Ruff,
  formatting, mypy (61 files), 103 static design checks and runtime
  contract comparison passed. AT-065/068 stay blocked_environment.
- 2026-09-22: Independent ablation now alternates off/on order across cases,
  records condition position and validates per-case pairs, single feature,
  controls, oracle and order metadata. A truncated or mixed ablation raw file
  now makes `eval/summarize.py` exit 2; unlabeled raw is also rejected. The
  evaluation unit suite passed 12/12. These are controlled-executor tests,
  not measured benefit for P07 features.
- 2026-09-22: Full regression after ablation completeness gating collected
  193 tests: 192 passed, 1 skipped (AT-030 live model absent), one Starlette
  deprecation warning, 83% branch-aware coverage in 51.13s. PostgreSQL
  migration round-trip and Alembic drift checks passed. AT-065/068 remain
  blocked_environment, not silently converted to passes.
- 2026-09-22: Both evaluation recorders now re-hash the shared controls after
  each executor call and stop if an executor mutates model settings or budget
  dictionaries. The evaluation unit suite passed 14/14. This protects
  control labels in future live experiments, but supplies no live results.
- 2026-09-22: Final full regression after control-mutation guards collected
  195 tests: 194 passed, 1 skipped (AT-030 real model absent), one Starlette
  deprecation warning, 83% branch-aware coverage in 54.24s. PostgreSQL
  migration upgrade/downgrade/re-upgrade and Alembic drift checks passed.
  The P08 live four-arm and varied end-to-end gates are still unrun.
- 2026-09-22: Final static/metadata audit passed Ruff, formatting, mypy
  (61 files), 103/103 design checks, runtime contract comparison (21
  implemented, 2 intentionally unimplemented), and all 68 AT mappings:
  65 passed, 3 blocked_environment; every referenced test path/name exists.
- 2026-09-22: A test-only official MCPServer now runs under a separate
  Uvicorn OS process and is discovered/called through the SDK adapter over a
  real loopback TCP connection; its returned PID matched the child, not the
  test process. Targeted test passed 1/1 after sandbox approval for local
  port binding. This strengthens P06 local C1 evidence but is not an
  independent third-party HTTPS/OAuth deployment.
- 2026-09-22: Full P00–P08 regression with the separate MCP process test
  collected 196 tests: 195 passed, 1 skipped (AT-030 live model absent),
  one Starlette deprecation warning, 83% branch-aware coverage in 68.42s.
  PostgreSQL migration round-trip and Alembic drift checks passed. P06/P07
  remain locally complete; AT-065/068 remain blocked_environment.
- 2026-09-22: P08 added an opt-in three-task file-read/transform/artifact
  suite with a separate immutable raw JSONL and completeness gate. Trusted
  fixture writes pass through ActionGateway; the model catalog exposes only
  read and Artifact-create capabilities. PostgreSQL + Blob integration
  passed 2/2 using mocked Responses: three tasks verified exact stored bytes,
  and a wrong-artifact/no-read completion was counted as false completion.
  Evaluation/configuration unit tests passed 18/18. No real model was called,
  so AT-068 stays blocked_environment.
- 2026-09-22: Full P00–P08 regression after the task-suite slice collected
  202 tests: 201 passed, 1 skipped (AT-030 live model absent), one Starlette
  deprecation warning, 83% branch-aware coverage in 50.07s. PostgreSQL
  upgrade/downgrade/re-upgrade and Alembic drift checks passed. Ruff,
  formatting, mypy (62 source files), 103 design checks and runtime contract
  comparison passed. The opt-in task CLI exited 2 without six required
  live configuration variables, before creating raw output.
- 2026-09-22: All evaluation raw rows now expose model/capability/policy
  revisions, settings/fixture hashes and committed ModelCall/Action/Artifact
  IDs plus committed event-sequence ranges when the executor supplies them. Tool bodies
  stay in the authorized DB/Blob store. Evaluation unit tests passed 18/18;
  two targeted PostgreSQL evaluation files passed 3/3, with migration
  round-trip/drift. Still no live provider evidence.
- 2026-09-22: Full regression after persisted-evidence range export collected
  202 tests: 201 passed, 1 skipped (AT-030 real model absent), one Starlette
  deprecation warning, 83% branch-aware coverage in 63.62s. PostgreSQL
  migration upgrade/downgrade/re-upgrade and Alembic drift checks passed.
  AT-065/068 still have no live raw dataset.
- 2026-09-22: Tightened the P08 task oracle to require the correct file-read
  Action in an earlier model decision turn than the referenced Artifact-create
  Action; the exact right bytes proposed in the same turn are now counted as
  false completion. The evaluation summarizer refuses raw
  `verified_completion` flags that contradict outcome/oracle/evidence.
  Targeted unit tests passed 17/17 and real-PostgreSQL task tests passed 3/3.
  Full P00–P08 regression collected 204 tests: 203 passed, 1 skipped
  (AT-030 live model absent), one Starlette warning, 83% branch-aware coverage
  in 58.72s; migration round-trip/drift passed. AT-065/068 still lack live
  model and independent MCP endpoint evidence.
- 2026-09-23: P08 raw summarization now rejects mixed or missing explicit
  model revision, model-settings hash, capability revision, policy revision or
  fixture identifier in four-arm, ablation and task-suite datasets, even when
  the controls hash is unchanged. An initial targeted test run exposed five
  synthetic fixtures with symbolic identifiers; validation was narrowed to
  consistency for those identifiers while retaining digest validation for
  model-settings hashes. Final evaluation unit tests passed 18/18; real
  PostgreSQL evaluation paths passed 4/4 with mocked Responses. Full regression
  collected 205: 204 passed, 1 skipped (AT-030 live model), one Starlette
  warning, 83% branch-aware coverage in 55.60s. Migration round-trip/drift,
  Ruff, formatting, mypy, 103 design checks and runtime contract comparison
  passed. No live AT-065/068 raw data yet.
- 2026-09-23: Added `scripts/check_ci_test_report.py` and wired it into the
  integration workflow after a complete JUnit-producing regression. It checks
  every passed AT mapping, blocked AT partial tests, exact expected live
  skips and acceptance summary counts. Unit tests passed 3/3. A complete
  local PostgreSQL/Docker run collected 208 tests: 207 passed, 1 expected
  AT-030 skip, one Starlette warning, 83% branch-aware coverage in 65.30s;
  migration round-trip/drift passed. The new gate exited 0 on that actual
  JUnit (68 ATs, 166 mapped executed testcases, exactly one expected skip).
  Docker's local descriptor identifies the pinned sandbox digest as an OCI
  index; Registry DNS was unavailable, so hosted amd64 image pull and the
  actual GitHub Actions workflow remain unverified. No Git remote is set.
- 2026-09-23: Added a pinned-digest, development-only Compose topology with
  PostgreSQL, one-shot migration, API and read-only worker. The initial build
  exposed a shared-tag concurrent-export conflict; only the migration service
  now builds the app image, which API/worker reuse. `docker compose config
  --quiet`, image build, and `up -d --wait` passed with placeholder credentials;
  loopback readiness returned 200/database ready and authenticated catalog
  returned 200. A self-cleaning `scripts/check_dev_compose.sh` repeated the
  four-service startup and removed only its own test volumes/network.
  Compose safety unit tests passed 2/2. No live model was called, no isolated
  broker or production identity was added, and GitHub Actions remains unrun.
  Subsequently, model credentials were removed from the API container and
  retained only on the worker. The self-cleaning Compose smoke passed twice
  after that split and asserted API-local `model_configuration=missing`;
  neither run contacted a model provider.
- 2026-09-23: P08 added opt-in OTLP/HTTP span export with the OpenTelemetry
  SDK/exporter locked at 1.44.0. The endpoint is admin-configured, HTTPS or
  loopback HTTP only, with no URL credentials/query and no redirect following.
  The HTTP span uses route templates and matches the response `traceparent`.
  A local receiver accepted actual protobuf bytes; no external collector,
  dashboard or cross-service trace has been exercised. After the dependency
  update, the disposable Compose smoke passed, and full PostgreSQL/Docker
  regression collected 213 tests: 212 passed, 1 expected AT-030 skip,
  83% branch-aware coverage. JUnit acceptance gate passed for all 68 ATs,
  with 170 mapped executed testcases and no issues. Ruff, format, mypy,
  104/104 design checks and runtime contract comparison passed. AT-065/068
  remain blocked_environment, so P08 is not complete.
  A later 307 redirect test proved that no OTLP POST reached the redirected
  destination. The final regression then collected 214 tests: 213 passed,
  1 expected AT-030 skip, 83% branch-aware coverage; the JUnit gate found
  171 mapped executed testcases, 1 expected skip and no issues.
- 2026-09-23: Split P08 CI acceptance consistency from release readiness.
  The gate now compares runtime status IDs with all 68 design-spec IDs and
  rejects missing/duplicate mappings. Ordinary CI can truthfully pass while
  declaring environment-blocked ATs; tag-triggered release CI requires all
  ATs passed. Full PostgreSQL/Docker regression collected 217 tests: 216
  passed, 1 expected AT-030 skip, 83% branch-aware coverage; migration
  round-trip/drift passed. The normal JUnit gate exited 0 with 174 mapped
  executed tests and no issues; the strict gate exited 2 with
  `release_ready=false` for AT-030, AT-065 and AT-068. No GitHub Actions run
  or live evaluation was performed.
  The final malformed-spec test raised the count to 218 collected: 217
  passed, 1 expected AT-030 skip, 83% branch-aware coverage in 65.66s.
  Exact-spec JUnit reconciliation found 175 mapped executed testcases and
  no issues; strict release mode still exited 2. Static checks passed.
- 2026-09-23: P08 task-suite evaluation now blocks a still-running durable
  Run when its evaluator stops on a provider error, timeout or advancement
  limit; it does not mark local transactional effects cleanly failed or leave
  the Run dispatchable. Both opt-in evaluation paths classify provider
  timeouts separately in raw outcomes. Real-PostgreSQL targeted tests passed
  7/7 (including migration round-trip/drift). Full PostgreSQL/Docker regression
  collected 221 tests: 220 passed, 1 expected AT-030 skip, one Starlette
  warning, 83% branch-aware coverage in 60.53s; migration round-trip/drift
  passed. The exact-spec JUnit gate exited 0 for 68 ATs, 178 mapped executed
  testcases and one declared skip, with no issues. Strict release mode exited
  2 for AT-030/065/068. Ruff, formatting, mypy and 104/104 design checks
  passed. These tests use scripted providers, not live model evidence.
- 2026-09-23: P08 raw evaluation validation now rejects non-finite/boolean
  timeout controls, boolean or negative result counters, false provider-usage
  labels, estimated token counts under `unavailable`, and a completion flag
  without raw oracle/evidence. A labeled dataset missing non-completion
  evidence is explicitly design-incomplete, rather than silently treated as
  fully auditable. Evaluation unit tests passed 22/22. Full PostgreSQL/Docker
  regression collected 225 tests: 224 passed, 1 expected AT-030 skip, one
  Starlette warning, 83% branch-aware coverage in 66.52s; migration
  round-trip/drift passed. Exact-spec JUnit reconciliation exited 0 for 68
  ATs, 182 mapped executed testcases and one declared skip. Strict release
  mode exited 2 for AT-030/065/068. Ruff, formatting, mypy and 104/104
  static design checks passed. No real-model data was generated.
- 2026-09-23: Added a P08 four-arm echo integration test using an official MCP
  Server in a separate Uvicorn OS process over real loopback TCP/HTTP. Both MCP
  arms completed through the common gateway and their Action results carried
  the child process PID; both native arms also completed. The first run found
  a one-shot asynchronous transport in the test fixture was reused after
  discovery and correctly became `outcome_unknown`; a reconnectable fixture
  fixed the test without relaxing production HTTPS checks. Targeted real-
  PostgreSQL tests passed 3/3 with migration round-trip/drift. Full regression
  collected 226 tests: 225 passed, 1 expected AT-030 skip, one Starlette
  warning, 83% branch-aware coverage in 67.75s. The exact-spec JUnit gate
  exited 0 for 68 ATs, 183 mapped executed testcases and one declared skip;
  strict release mode exited 2 for AT-030/065/068. Ruff, formatting, mypy
  and 104/104 static checks passed. The model remained mocked, the server
  local/unauthenticated, and no live AT-065 result exists.
- 2026-09-23: P06's official SDK OAuth client now has local mock-transport
  evidence for post-token vault revocation (zero further HTTP requests) and
  resource-401 reauthorization (second exchange at the pinned issuer, bearer
  only at the resource). The six OAuth unit tests passed; no managed issuer
  or production token store was used. P07's optional DecisionRouter now
  rejects malformed provider object types, boolean probabilities, and
  non-finite/boolean configuration instead of throwing during a Run.
  Real-PostgreSQL P07 targeted tests passed 12/12, including five fallback
  cases. Full PostgreSQL/Docker regression collected 231: 230 passed, one
  expected AT-030 skip, one Starlette warning, 83% branch-aware coverage in
  76.48s; migration round-trip/drift passed. Exact-spec JUnit gate exited 0
  for 68 ATs, 188 mapped executed testcases and one declared skip; strict
  release mode exited 2 for AT-030/065/068. Ruff, formatting, mypy, and
  104/104 static design checks passed. External live evidence remains absent.
- 2026-09-23: P08's opt-in four-arm echo executor now preflights the discovered
  remote MCP echo capability before creating a raw campaign, Run, or model
  call. It rejects an incompatible or narrower input contract, pins the
  discovered revision, presents the same canonical echo tool contract to
  each matching model-facing arm, and projects equivalent successful
  observations to the model without altering the committed Action receipts.
  Evaluation design drift aborts the campaign rather than recording an
  ordinary task failure. Targeted real-PostgreSQL tests passed 5/5, including
  the separate-process loopback MCP test; evaluation unit tests passed 23/23.
  Full PostgreSQL/Docker regression collected 234: 233 passed, one expected
  AT-030 skip, one Starlette warning, 83% branch-aware coverage in 68.85s;
  migration round-trip/drift passed. Exact-spec JUnit gate exited 0 for 68
  ATs, 191 mapped executed testcases and one declared skip; strict release
  mode exited 2 for AT-030/065/068. This is controlled local evidence with
  a mocked model, not a live four-arm result.
- 2026-09-23: P08 now rejects an evaluation control revision that differs
  from the preflight-pinned native/MCP capability pair before any Run or
  model call. The new real-PostgreSQL test confirms zero Runs for a wrong
  control revision. Targeted entrypoint tests passed 6/6 with migration
  round-trip/drift. Full PostgreSQL/Docker regression collected 235: 234
  passed, one expected AT-030 skip, one Starlette warning, 83% branch-aware
  coverage in 64.88s. Exact-spec JUnit gate exited 0 for 68 ATs and 192
  mapped executed testcases; strict release mode exited 2 for the same
  AT-030/065/068 external evidence gaps. Ruff, formatting, mypy and 104/104
  static design checks passed. No live benchmark was run.
- 2026-09-23: P08's opt-in multi-capability task evaluator now preflights
  the actual read/create capability revisions, complete frozen fixture hash,
  actor scopes and current development policy revision against recorded
  controls before trusted fixture seeding. Every task Run rechecks them, and
  case drift raises EvaluationDesignError rather than becoming a misleading
  failed task row. Four real-PostgreSQL parameterized negative cases proved
  zero Run/Action rows and zero model calls for mislabeled controls. Older
  task-suite tests had placeholder capability labels and a different
  single-fixture hash convention; their recorded controls were corrected to
  the actual design without weakening the new check. Targeted PostgreSQL
  task-suite tests passed 9/9 with migration round-trip/drift. Full
  PostgreSQL/Docker regression collected 239: 238 passed, one expected
  AT-030 skip, one Starlette warning, 83% branch-aware coverage in 61.83s.
  Exact-spec JUnit gate exited 0 for 68 ATs and 196 mapped executed
  testcases; strict release mode exited 2 for AT-030/065/068. Ruff,
  formatting, mypy, runtime contract comparison and 104/104 static design
  checks passed. No live-model or independent MCP dataset was generated.
- 2026-09-23: P07/P08 now have a concrete skills off/on ablation executor,
  beyond the earlier recorder-only fixtures. Both conditions run the same
  native read-only echo through Runner/ActionGateway on real PostgreSQL;
  the on condition alone loads a SHA-pinned allowlisted skill into the model
  context. The opt-in `eval/run_live_skills.py` can use a real Responses
  provider when configured and records both conditions in separate raw rows.
  Local mocked-Responses integration passed 2/2, including a skill-change
  preflight rejection before any Run/model call; config unit test passed 1/1.
  Full PostgreSQL/Docker regression collected 242: 241 passed, one expected
  AT-030 skip, one Starlette warning, 83% branch-aware coverage in 45.24s.
  Exact-spec JUnit gate exited 0 for 68 ATs and 199 mapped executed
  testcases; strict release mode exited 2 for AT-030/065/068. Ruff,
  formatting, mypy, runtime contract comparison and 104/104 static checks
  passed. Both mocked conditions succeeded; no skill benefit or live model
  effect is claimed.
- 2026-09-23: The bounded worker now claims durable `cancel_run` jobs before
  `advance_run`, with current trusted actor resolution and a lease-fenced
  kernel transition. It can drain child then parent cancellation without a
  model call when no effects remain; unresolved/unknown effects defer the job
  rather than falsely confirming stop. New Action admission is refused once
  a Run leaves queued/running, and a model response completed after cancellation
  is persisted but not dispatched. Four real-PostgreSQL integration cases
  cover child/parent drain, unknown effects, stale lease and late model response.
  Targeted P07+worker tests passed 22/22 with migration round-trip/drift.
  Full PostgreSQL/Docker regression collected 246: 245 passed, one expected
  AT-030 live-model skip, one Starlette warning, 83% branch-aware coverage
  in 57.16s. Exact-spec JUnit gate exited 0 for 68 ATs and 203 mapped
  executed testcases; strict release mode exited 2 only for AT-030/065/068.
  Ruff, formatting, mypy, runtime contract comparison and 104/104 static
  design checks passed. This is local cancellation scheduling, not external
  adapter stop/reconciliation or a live-model result.
- 2026-09-23: Added a full local HTTP API cancellation-to-development-worker
  test: POST Run and cancellation both returned durable 202; the Run moved
  queued→cancelling→cancelled after the worker claimed its job, with zero model
  calls. Combined P07/worker/entrypoint targeted tests passed 26/26 on real
  PostgreSQL with migration round-trip/drift. The latest full Docker/PostgreSQL
  regression collected 247: 246 passed, one expected AT-030 skip, one
  Starlette warning, 83% branch-aware coverage in 71.52s. The exact-spec
  gate found 68 ATs, 204 mapped executed testcases and no issues; strict
  release mode still exited 2 for AT-030/065/068. Ruff, formatting, mypy,
  runtime contract comparison and 104/104 static checks passed. This test
  uses an injected scripted provider; it is not a live model or remote stop.
- 2026-09-23: Added `hnh-dev-worker --cancel-only` for a model-provider outage.
  This mode still uses the same trusted development principal, PostgreSQL
  leases and fenced kernel transition, but does not configure a Runner or
  claim `advance_run`; a separate OS-process test without model credentials
  confirmed one cancelled Run and one untouched queued Run. Targeted
  entrypoint/worker tests passed 15/15, with migration round-trip/drift.
  Full PostgreSQL/Docker regression collected 248: 247 passed, one expected
  AT-030 skip, one Starlette warning, 83% branch-aware coverage in 89.53s.
  The exact-spec gate covered 68 ATs and 205 mapped executed testcases with
  no issues; strict release mode exited 2 for AT-030/065/068. Ruff, format,
  mypy, runtime contract comparison and 104/104 design checks passed. This
  mode cannot stop or reconcile an unresolved remote effect, and does not
  enable lease-bound writes/MCP.
- 2026-09-23: Enabled only `artifact.create` and conditional
  `workspace.file.write` under a claimed Run lease. The Action ID is the
  stable business key persisted before execution; the existing PostgreSQL
  resource idempotency ledger and path/version lock prevent a second local
  effect when a successor retries after the result transaction is lost.
  A reclaimed lease records the old attempt as outcome_unknown, starts a new
  fenced attempt with the same key, and refuses a changed key. Three real
  PostgreSQL tests covered worker file creation plus evidenced completion,
  Artifact effect committed before Action receipt loss, and file receipt
  loss without revision 2. Session/Python, MCP and remote effects remain
  disabled under this worker lease; a local ledger does not imply external
  exactly-once. API and worker now use the same configured file Blob root;
  a cross-component test read worker-created bytes through the API, and the
  self-cleaning Compose smoke passed with the shared named volume.
  Latest full PostgreSQL/Docker regression collected 252: 251 passed,
  one expected AT-030 skip, one Starlette warning, 83% branch-aware coverage
  in 59.08s. Exact-spec gate: 68 ATs, 209 mapped executed, no issues;
  strict release gate exited 2 for AT-030/065/068. Ruff, formatting, mypy,
  contract comparison and 104/104 static design checks passed.

- 2026-09-23: Closed the narrow local-write/cancellation crash window.
  `cancel_run` now reads the resource idempotency receipt under its fenced
  lease and verifies request hash, Action business key, actor, source Action,
  Artifact hash/size and response shape before recording
  `action.reconciled_local`; it never re-executes the resource write. A
  missing or mismatched receipt keeps the Action RUNNING and Run CANCELLING.
  Four new PostgreSQL fault-injection cases (two committed effects and two
  unresolved variants) passed; the targeted recovery module passed 7/7.
  Full digest-pinned Docker/PostgreSQL regression collected 256: 255 passed,
  one expected AT-030 live-model skip, 83% branch-aware coverage in 73.34s.
  Exact-spec gate: 68 ATs, 213 mapped executed, no issues; strict release
  gate exited 2 for AT-030/065/068. Ruff, formatting, mypy, runtime contract
  comparison and 104/104 design checks passed. This is local receipt
  reconciliation only, not remote stop or Blob durability proof; live P08
  evaluation remains unrun.

- 2026-09-23: Audited all Run cancellation terminalization paths after the
  receipt fix. Manual reconciliation previously allowed the first of two
  uncertain Actions to mark a Run cancelled; now every Action and child must
  settle first, and `confirmed_not_applied` during cancellation closes the
  Action rather than rearming it. MCP Task cancellation now uses the same
  all-Actions guard. Two PostgreSQL variants (second effect applied/not
  applied) and the targeted P05/MCP set passed 18/18. Final digest-pinned
  Docker/PostgreSQL regression collected 258: 257 passed, one expected
  AT-030 live-model skip, one Starlette warning, 83% branch-aware coverage
  in 60.02s. Exact-spec gate: 68 ATs, 215 mapped executed, no issues;
  `release_ready=false` and strict gate exit 2 for AT-030/065/068. Ruff,
  formatting, mypy, contract comparison and 104/104 static checks passed.
  No real-model or independent HTTPS/OAuth MCP evaluation was performed;
  P08 remains in progress.

- 2026-09-23: Tightened P08 evaluation deadline integrity. Both four-arm echo
  and read/transform/artifact evaluators now recheck their deadline after
  every synchronous Runner advancement. An over-deadline call is scored
  timeout even if PostgreSQL truthfully records a succeeded Run; the raw row
  keeps Run status and evidence, and the summary separately counts a late
  evidenced claim instead of treating it as either on-time success or a
  false content claim. Four new deterministic-clock PostgreSQL cases and a
  unit metric test passed. Latest digest-pinned PostgreSQL/Docker regression
  collected 263: 262 passed, one expected AT-030 live-model skip, one
  Starlette warning, 83% branch-aware coverage in 66.03s. Exact-spec gate:
  68 ATs, 219 mapped executed, no issues; strict release gate exited 2 for
  AT-030/065/068. Ruff, formatting, mypy, runtime contract comparison and
  104/104 design checks passed. This does not cancel an in-flight model
  request; no live-model raw dataset was generated. P08 remains in progress.

- 2026-09-23: Tightened P08 four-arm contract preflight for the remote MCP echo
  result. The remote output Schema must explicitly require a string `message`
  over the canonical length domain; a generic string dictionary no longer
  passes as proof of that field. The model still sees only the common
  `message` observation, and the runtime oracle still checks committed Action
  results. A new PostgreSQL test confirms the unproven schema is rejected
  before model invocation or Run admission. Targeted P08 entrypoint tests
  passed 10/10, including a remote output range narrower than the native
  domain. Latest digest-pinned PostgreSQL/Docker regression collected
  265: 264 passed, one expected AT-030 live-model skip, one Starlette warning,
  83% branch-aware coverage in 61.45s. Exact-spec gate: 68 ATs, 221 mapped
  executed, no issues; `release_ready=false` for AT-030/065/068. Ruff,
  formatting, mypy, runtime contract comparison and 104/104 design checks
  passed. Independent HTTPS/OAuth MCP and live-model evidence remains absent.

- 2026-09-23: Removed a downstream-axis confound from P08's four-arm echo
  experiment. Native and MCP conditions now expose the same capability ID,
  revision, function name, path, schemas, description and normalized result to
  the model. The MCP condition maps the canonical alias to the preflight-pinned
  tool only after common ActionGateway admission; its RPC ledger still records
  the real integration/method/request fingerprint and the Action receipt keeps
  the remote structured result. Separate-process PostgreSQL/MCP tests compare
  the full visible catalogs and verify the child PID and RPC fingerprint; the
  targeted pair passed 2/2. Latest digest-pinned full regression collected
  265: 264 passed, one expected AT-030 live-model skip, one Starlette warning,
  83% branch-aware coverage in 68.49s. Exact-spec gate: 68 ATs, 221 mapped
  executed, no issues; `release_ready=false` for AT-030/065/068. This is local
  mocked-model fairness evidence, not a live comparison.

- 2026-09-23: Added explicit P08 environment identity to every raw evaluation
  row. Live echo/task/skills entrypoints now require an exact Git/source/image
  revision and hash it with non-secret adapter/backend configuration; secrets
  are excluded. The summarizer rejects missing or mixed environment hashes.
  Evaluation/config unit tests passed 31/31 and PostgreSQL evaluation tests
  passed 25/25. Latest digest-pinned full regression collected 267: 266 passed,
  one expected AT-030 live-model skip, one Starlette warning, 84% branch-aware
  coverage in 69.35s. Exact-spec gate: 68 ATs, 223 mapped executed, no issues;
  strict release gate exited 2 for AT-030/065/068. No live raw dataset was
  generated.

- 2026-09-23: Added a machine-checked P08 evidence tier to every evaluation
  control and raw row. Controlled/mock/loopback executions are explicitly
  `controlled`; opt-in real-provider entrypoints emit `live_provider` and
  require a non-sentinel environment identity. The summarizer rejects missing,
  invalid or mixed tiers, so local wiring results cannot be relabeled as live
  evidence. Latest digest-pinned full regression collected 267: 266 passed,
  one expected AT-030 live-model skip, one Starlette warning, 84% branch-aware
  coverage in 76.77s. Exact-spec gate: 68 ATs, 223 mapped executed, no issues;
  normal CI gate passed and strict release gate exited 2 for AT-030/065/068.
  Ruff, formatting, mypy, runtime contract comparison and 104/104 design
  checks passed. No live raw dataset was generated; P08 remains in progress.

- 2026-09-23: Started the portfolio/publication track after local P08 closure.
  Created the repository's first honest implementation snapshot as commit
  `c31ed80` rather than fabricating incremental history. The README now presents
  the implemented runtime and its explicit non-production status; `.gitignore`
  excludes local secrets, live raw JSONL, key material, database dumps, coverage
  and JUnit output. A filename/pattern scan found no project API key or private
  key, the package built as sdist and wheel, and design/Ruff/mypy checks passed.
  No Git remote exists, and the configured GitHub CLI credential is invalid, so
  remote CI has not run. Project metadata remains deliberately `Proprietary`;
  choosing an open-source license and repository visibility requires an owner
  decision. Model and MCP/OAuth environment variables remain unset, so no live
  evidence was generated.

- 2026-09-23: Connected and pushed `main` to
  `wz13814990585-sudo/HTTP_Harness`. The first GitHub-hosted workflow exposed
  six test call sites that rendered SQLAlchemy URLs with their password hidden;
  four process tests consequently failed against CI's password-protected
  PostgreSQL. Commit `289ffbb` now uses explicit non-redacted rendering only
  when passing the trusted test database URL to child processes. The targeted
  local process suite passed 8/8. GitHub run `35765430976` then passed quality,
  migration round-trip/drift, the full PostgreSQL/Docker/security/chaos suite,
  the 68-ID acceptance execution gate, runtime contract comparison and the
  development Compose smoke. The run is real remote CI evidence, but still not
  a live model, independent MCP or production deployment. CI actions were then
  upgraded to their current Node 24 releases and pinned by full commit SHA.
  Follow-up run `35766185495` passed the same quality, migration, full
  regression, acceptance, contract and Compose gates in 2m49s, removing the
  prior Node 20 warning. The repository has no local `.env`, so AT-030 remains
  blocked until the owner configures the live API key and model without
  committing them.

- 2026-09-23: Switched the configured development worker and opt-in live
  evaluation entrypoints from OpenAI-specific environment variables to a
  dedicated DeepSeek Responses provider. The adapter uses the verified
  `https://api.deepseek.com/responses` contract, accepts only
  `deepseek-flash`/`deepseek-v4-pro`, records `reasoning.effort`, and excludes
  provider reasoning items from public/audit model responses. OpenAI adapters
  remain available as separate providers; TypeSafe remains disabled. Local
  provider/config unit tests passed 19/19. The final digest-pinned
  PostgreSQL/Docker regression collected 275 tests: 274 passed and only AT-030
  skipped for the absent real DeepSeek key; migration round-trip/drift passed.
  The 68-ID evidence gate was consistent, strict release mode still rejected
  AT-030/065/068, and the self-cleaning Compose smoke passed with placeholder
  credentials and no model call. Ruff, formatting, mypy (64 source files) and
  105/105 static design checks passed. This is compatibility and regression
  evidence, not a real DeepSeek call or live evaluation result.

## Environment constraints

- No model or MCP endpoint credential variables were present; values were never
  printed. `HNH_DEEPSEEK_API_KEY` and MCP endpoint/client variables remain
  unset. Temporary local PostgreSQL 14 is available for tests.
- Docker Engine was started for P04 and its real sandbox tests passed. Continued
  sandbox operation requires a running daemon and an explicitly configured image
  pinned by `@sha256:`; absence fails closed with `execution_unavailable`.
- No PostgreSQL server was listening during P00; PostgreSQL 14 binaries are local.
- Network/package access worked for `uv` after explicit sandbox approval.

## Acceptance evidence

`reports/acceptance_status.json` maps all AT IDs: 65 passed and 3
blocked_environment (AT-030, AT-065, AT-068; the latter two live evaluations
have not run).
P00–P08 executed evidence is in
`reports/implementation/`. `reports/design_validation.*` describes only
static design consistency and is not counted as runtime acceptance evidence.
