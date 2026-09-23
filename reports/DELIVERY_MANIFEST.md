# Delivery status

The original ZIP manifest described the initial design-only package. It is no
longer a file-integrity manifest for this implemented repository; Git commits
and the remote CI artifacts now provide source and test provenance. Do not use
the original design-package file count or hashes to assess the current tree.

Current status on 2026-09-23:

- typed Python 3.12 modular-monolith implementation with eight Alembic
  migrations;
- HTTP API, PostgreSQL kernel, ActionGateway, model loop, recovery,
  isolated-execution adapter, MCP adapter, optional TypeSafe routing and P08
  evaluation/operations tooling implemented;
- 67 of 68 acceptance cases passed, zero failed, AT-065 marked
  `blocked_environment` because no independent HTTPS/OAuth MCP service was
  used;
- AT-030 has real DeepSeek + PostgreSQL evidence;
- AT-068 has immutable real-provider task-suite and skills-ablation datasets,
  including failures in their denominators;
- GitHub-hosted CI has exercised quality, migration, PostgreSQL/Docker,
  security/chaos, acceptance-mapping, contract and development Compose gates;
- this remains a research/portfolio implementation, not a production-ready
  multi-tenant service.

Authoritative current artifacts:

| Purpose | File |
|---|---|
| Machine-readable acceptance status | `reports/acceptance_status.json` |
| Living implementation history | `PLANS.md` |
| P08 implementation evidence | `reports/implementation/P08.md` |
| Live evaluation interpretation | `reports/evaluation/P08.md` |
| Static design validation | `reports/design_validation.json` |
| Runtime API contract | `contracts/openapi.yaml` |
| Exact acceptance specification | `eval/acceptance_cases.yaml` |

Static design checks, controlled fixtures, live-provider evidence and an
independent remote integration are deliberately reported as different evidence
tiers. None may be relabeled as another tier to make a release gate pass.
