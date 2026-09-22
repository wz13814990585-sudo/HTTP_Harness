# Repository instructions — HTTP-native Harness

## Project
Independent HTTP-native Agent Harness, not MiniCodex. This repository initially contains a design/development package, not a working harness. Build the implementation; do not replace the task with another architecture essay.

Read `docs/01_architecture.md`, `docs/03_runtime_recovery.md`, `docs/04_security.md`, `docs/10_roadmap.md`; consult `contracts/` and the active `prompts/Pxx.md`. Use `PLANS.md` as the living implementation plan. Source/version facts live in `docs/SOURCES.md`; exact dependency versions must be actually verified and locked.

## Non-negotiable invariants
1. Models propose untrusted operations; only the kernel authenticates, authorizes, admits, commits state and declares evidenced completion.
2. All effects pass through the same ActionGateway. In-process calls cannot bypass policy. Dispatching an admitted action must not create another local action recursively.
3. PostgreSQL owns durable run/action/model-call state, events, request idempotency, budgets and wake-up jobs. HTTP logs and chat summaries are not execution truth.
4. Return HTTP 202 only after durable admission commits. Persist a complete model response before dispatching its actions. Never dispatch partial streaming JSON.
5. Unknown external effects stay unknown until reconciled. Leases and fencing do not guarantee exactly-once at a remote API. Never blindly replay unsafe writes after timeout/500.
6. Approval binds the exact action hash, actor/scope, resource/capability/policy versions and expiry; recheck at dispatch. Models cannot approve themselves or forge identity headers.
7. No host exec fallback. Code runs only through a configured isolated broker. No untrusted pickle import, arbitrary Docker flags, host mounts/socket access, or default unrestricted network.
8. Validate paths, query/body schema, SSRF destinations, redirects and credentials. Never log real secrets or put opaque continuation credentials into model context.
9. MCP is an optional edge adapter. Modern/legacy profiles are explicit. Advertise only tested extensions. The core must work without MCP or TypeSafe.
10. Terminal states cannot revive. Cancellation is intent, not proof that effects stopped. Completion requires committed evidence and no unresolved effects.
11. Do not fabricate test/benchmark outcomes, skip counts, production readiness or performance claims. Fake providers and static schema checks are not real integration results.
12. Do not copy upstream code without checking and retaining its license/attribution.

## Implementation style
Python 3.12 compatibility baseline, typed modular monolith, one state owner. Prefer narrow ports and testable services over dozens of empty abstractions. Domain must not import FastAPI/SDK adapters. Migrations and real Postgres tests are required. Keep safety checks deterministic; lifecycle plugins cannot disable them.

The design API is a project contract, not an internet standard. When docs/schema/code disagree, document and fix all affected artifacts before weakening behavior. Unimplemented functionality must fail explicitly and stay out of advertised capability catalogs.

## Work and evidence
Implement one vertical slice at a time. Run the static package checker `python scripts/validate_design.py`; separately run actual unit/contract/integration/security/chaos tests when implemented. Maintain `reports/acceptance_status.json` mapping AT IDs to test functions, commands and outcomes. Record blocked environments without fake success. Keep `PLANS.md` and `reports/implementation/Pxx.md` current, including exact commands, limitations and next steps.
