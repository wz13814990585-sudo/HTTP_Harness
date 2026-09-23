# HTTP-native Agent Harness

[![CI](https://github.com/wz13814990585-sudo/HTTP_Harness/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/wz13814990585-sudo/HTTP_Harness/actions/workflows/ci.yml)

**持久化、策略受控、以执行证据为完成依据的 Agent 运行内核。**

版本：`0.1.0.dev0` · 更新：2026-09-23 · **68 项验收中 67 项通过，0 项失败，
1 项因外部环境阻塞。** P00–P07 已完成；P08 已有真实 DeepSeek 与远端 CI
证据，唯一未关闭项是需要独立 HTTPS/OAuth MCP 服务的 AT-065。本项目是可运行的
研究/作品集版本，不是生产就绪版本。

```text
Client / CLI
     │
HTTP API + SSE
     │
Harness Kernel ── RunController / Runner / ActionGateway / CompletionGate
     │                         │
PostgreSQL                  Execution adapters
Run / Action / Event        Native / HTTP / MCP / isolated Python
     │                         │
Immutable Artifact store   Restricted execution broker
```

核心边界：模型只提出不可信操作；内核负责身份、授权、准入、状态提交、恢复和
完成判定；HTTP、MCP 与 Python 都是执行适配器。

## 验收快照

| 证据 | 当前结果 | 边界 |
|---|---|---|
| P00–P07 | 全部阶段验收通过 | 不代表生产多租户安全 |
| AT-030 真实模型闭环 | DeepSeek + PostgreSQL 两次通过 | 不是大规模模型质量评估 |
| AT-068 真实任务/skills | 前两次三任务集均 1/3；决策约束改进后一次 3/3；skills off/on 均 4/4 | 小样本，不宣称稳定质量或 skills 收益 |
| GitHub Actions | 质量、迁移、PostgreSQL/Docker、安全/chaos、Compose smoke 通过 | 使用占位模型，不是 live 运行 |
| AT-065 | `blocked_environment` | 缺独立 HTTPS MCP/OAuth 服务；不以 localhost 冒充 |

机器可读状态以 [`reports/acceptance_status.json`](reports/acceptance_status.json) 为准；
阶段证据见 [`reports/implementation/`](reports/implementation/) 和
[`reports/evaluation/P08.md`](reports/evaluation/P08.md)。

当前已实现真实 PostgreSQL 支撑的 Run 创建、查询、取消意图、鉴权和请求幂等。
同时已实现授权 Capability 目录、统一 ActionGateway/ResourceDispatcher、受管版本文件、
不可变 Artifact、本地确定性 adapter、持久化模型循环、完成门禁、可续传事件读取，
以及经真实 Docker 测试的隔离 Python 持续会话和外部 Blob 存储。
P05 还加入了持久化审批/澄清、未知外部结果对账、租约 fencing、取消竞态、
checkpoint 兼容性门禁、统一脱敏和出站目的地校验。P06 已有可选官方 SDK MCP 适配器、
现代/旧版本地互操作、MRTR/Tasks 持久化和按请求授权的组合入口，但 OAuth 与独立远端验收未完成。
P07 的分类提示、按需 skills 与有界子 Run 已通过本地测试。P08 已有评估记录器、原生函数工具视图、readiness/metrics、可选的 OTLP HTTP trace 导出、
离线数据库加 Blob 恢复演练，以及需显式配置真实模型和独立 MCP 服务的四组 × 四个固定输入的只读 echo 评估入口
`eval/run_live_echo.py`；该入口目前只完成模拟模型/本地 MCP 的接线验证，且四个输入仍属单一 echo 能力，不是跨能力任务集。另有支持原生读取及受管文件/Artifact 本地事务写入的有界 worker 和显式启用的 `hnh-dev-worker` 单机进程入口，已验证 API→PostgreSQL→独立 worker 进程→完成门禁、租约续期、身份解析、写入回执丢失后的同键恢复与失效保护。真实 DeepSeek 任务集和 skills ablation 已运行；独立 MCP/OAuth 四组实验与生产 worker 部署仍未完成。以 `PLANS.md`、
`reports/implementation/` 和 `reports/acceptance_status.json` 为真实状态依据。

P08 另有 `eval/run_live_tasks.py`：三个固定的“读受管文件→转换→创建
不可变 Artifact”任务，使用实际产物字节和已提交 Action 评分。前两次真实
DeepSeek 小样本均为 1/3；在显式强调单一决策和依赖操作分轮后，第三次为 3/3。
所有原始成功、失败和错误完成记录都保留。这是改进信号，不是公开基准、统计结论
或生产部署证明。

另有 `eval/run_live_skills.py`：在相同只读 echo 任务、模型界面、权限和
预算下独立比较 skill 关闭/开启。两组均经过持久化内核与 ActionGateway；
真实 DeepSeek 小样本中两组均为 4/4 完成，但开启组使用更多 token，不能
据此宣称收益或统计显著性。

## 本地录屏演示

仓库提供一个自清理的本地演示：启动临时 PostgreSQL、API 和独立 worker，
通过真实 HTTP 写入文件和提交 Run，调用 DeepSeek，最后核验不可变 Artifact
字节以及已提交的 Action/Event 证据。它不需要云服务器或自有域名，但会消耗少量
DeepSeek API 配额，并要求本机 Docker Engine 正在运行。

```bash
uv sync --locked --all-extras --dev
set -a
source .env
set +a
uv run python scripts/run_portfolio_demo.py
```

脚本为本次演示生成随机数据库密码和开发 token，不打印密钥；结束后只删除本次
演示创建的容器、网络和临时卷。逐镜头录制说明和失败处理见
[`docs/PORTFOLIO_DEMO.md`](docs/PORTFOLIO_DEMO.md)。已有运行中的本地服务也可直接运行
`uv run hnh-demo`。

## 销售报告与故障恢复演示

另有一条不依赖模型 API 的可交互业务演示：生成并读取销售 CSV，在固定 digest、
禁网的 Python 沙箱中计算汇总并创建 Markdown Artifact，等待操作者批准发布，随后
让独立本地 HTTP 发布服务在提交效果后故意丢失响应。Harness 会把 Action 保留为
`outcome_unknown`、阻塞 Run、查询下游账本完成对账，并验证同一 Action 的再次调用
不会产生第二次发布。

```bash
uv sync --locked --all-extras --dev
uv run python scripts/run_sales_demo.py
```

在提示处输入 `approve` 才会发布；自动化验证可显式增加 `--auto-approve`。成功输出
必须同时显示 `verified=true`、`publish_requests=1`、`publication_count=1` 和
`replay_without_redispatch=true`。样例数据、逐步说明和边界见
[`docs/SALES_DEMO.md`](docs/SALES_DEMO.md)。这是本地受控故障演练，不是独立远端
生产服务或 AT-065 证据。

## 当前可运行切片

需要 Python 3.12、uv 和 PostgreSQL。开发服务默认只监听由启动命令指定的地址；
请使用随机开发 token，不要将此模式暴露到公网。

```bash
uv sync --locked --all-extras --dev
export HNH_DATABASE_URL='postgresql+psycopg://localhost/hnh'
export HNH_DEV_TOKEN='replace-with-a-random-local-token'
# 可选；必须按 digest 固定。未配置时执行接口会明确失败关闭。
export HNH_SANDBOX_IMAGE='python@sha256:<verified-digest>'
export HNH_BLOB_ROOT='/absolute/path/to/hnh-blobs'
uv run alembic upgrade head
uv run uvicorn hnh.transport.http.app:app --host 127.0.0.1 --port 8080
```

已实现接口：

```text
GET  /healthz
GET  /readyz
GET  /metrics                 # 需要 metrics:read
GET  /v1/capabilities
GET  /v1/capabilities/{capability_id}
GET  /v1/openapi.json
POST /v1/runs
GET  /v1/runs/{run_id}
GET  /v1/runs/{run_id}/event-history
GET  /v1/runs/{run_id}/events
POST /v1/runs/{run_id}/cancellations
GET  /v1/actions/{action_id}
POST /v1/actions/{action_id}/reconciliations
GET  /v1/input-requests/{input_request_id}
POST /v1/input-requests/{input_request_id}/responses
POST /v1/execution-sessions
GET  /v1/execution-sessions/{session_id}
POST /v1/execution-sessions/{session_id}/executions
GET  /v1/workspaces/{workspace_id}/files/{file_path}
PUT  /v1/workspaces/{workspace_id}/files/{file_path}
POST /v1/artifacts
GET  /v1/artifacts/{artifact_id}
GET  /v1/artifacts/{artifact_id}/content
POST /v1/integrations/{integration_id}/operations/{operation_name}/invocations
```

真实 PostgreSQL 回归可运行：

```bash
scripts/test_with_postgres.sh --cov --cov-report=term-missing
```

最小 API 示例（仅提交，不会自动运行模型）：

```bash
curl -sS http://127.0.0.1:8080/readyz
curl -sS -H "Authorization: Bearer $HNH_DEV_TOKEN" \
  http://127.0.0.1:8080/v1/capabilities
curl -i -X POST http://127.0.0.1:8080/v1/runs \
  -H "Authorization: Bearer $HNH_DEV_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: example-run-001' \
  -d '{"agent_id":"demo","input":"检查授权能力"}'
```

最后一个请求在数据库提交后返回 `202` 和 Run 地址；`202` 不代表任务完成。
如需在本机推进已提交 Run，可按[本地运维手册](docs/P08_local_runbook.md)
另启 `hnh-dev-worker`，并提供真实模型配置；也可使用无需模型配置的
`hnh-dev-worker --cancel-only` 仅处理取消 job，不推进普通任务。该开发入口的模型动作
支持原生读取及 `artifact.create`、条件文件写入这两种本地账本保护的操作；可自动处理
无未决效果的取消任务，并只读核对已提交但 Action 回执丢失的本地写入；收据缺失或不匹配时继续等待，未知外部效果仍须对账。MCP、Python 会话和远端写入在租约执行路径上
失败关闭。未启动 worker 时 Run 保持排队。

本项目是独立于 MiniCodex 的新 Harness。目的不是包装一个聊天接口，也不是把所有 Python 函数变成 HTTP 微服务，而是让 Agent 通过可寻址、可发现、可校验的 HTTP 操作访问世界，由一个有持久化状态的内核控制执行。

一句话定位：**HTTP 描述操作；模型提出行动；内核决定是否执行；执行器产生结果；持久化记录支持检查与恢复。**

## 本包包含什么

`docs/` 是完整的架构、协议、状态、存储、安全、模型、MCP、测试和运行方案。`contracts/` 是拟定的 OpenAPI、JSON Schema、状态迁移与示例。`prompts/` 是逐阶段 Codex 任务。`eval/` 包含验收规格、实验定义和部分已实现的可选评估入口。`AGENTS.md` 与 `CODEX_MASTER_PROMPT.md` 用于启动开发。`docs/12_ports_and_execution.md` 给出内部接口、适配器返回约定与执行时序。

初始资料包只附带设计静态校验器；当前仓库已经新增 HTTP 服务、八段数据库迁移和
P00–P08 的分阶段测试。模型 Runner 已实现，当前默认真实 provider 为 DeepSeek
Responses API；AT-030 已用真实 DeepSeek 与 PostgreSQL 完成读取、转换、Artifact 和
证据门禁闭环。默认模型为 `deepseek-flash`，工具循环默认 reasoning effort 为
`none`，均可通过受信任环境配置覆盖。TypeSafe System One 是默认关闭的可选策略
分类器，已完成真实 API 契约验证，但尚无收益/校准结论。隔离执行已在本地 Docker Engine
中通过 P04 验收，但这不等于对任意敌对多租户代码的生产安全证明；MCP/OAuth 的独立远端验证仍未完成。静态设计校验通过不代表
这些后续能力、故障恢复或生产安全已经通过。

P05 的恢复验收使用确定性的进程内故障 effect driver，并非真实远端服务；DNS/跳转
测试也使用受控 resolver，并非生产网络抓包或完整出站代理证明。因此 P05 阶段完成，
但 AT-065 的独立 MCP/OAuth 四组实验完成前，不把整个发布出口称为完整通过。AT-068
已有真实 DeepSeek 三任务 raw JSONL 和 skills off/on 数据；失败仍计入分母，结果不代表
生产质量或统计显著性。

CI 区分“验收证据映射一致”和“可以发布”：当前 68 项映射检查通过，但
AT-065 未完成，严格发布门禁会返回非零状态；见
`scripts/check_ci_test_report.py` 与 `reports/acceptance_status.json`。

## 阅读与运行入口

先看 [架构边界](docs/01_architecture.md)、[恢复语义](docs/03_runtime_recovery.md)、
[安全边界](docs/04_security.md) 和 [本地运维手册](docs/P08_local_runbook.md)。
当前可作为受控本地开发服务运行，但 worker 必须显式启动，且尚无生产身份、完整
OAuth 或独立远端 MCP 验证；不要据此直接部署公网服务。
另有固定镜像 digest 的开发用 Compose 拓扑和自清理烟测；它验证迁移、API 与
worker 进程可启动，但烟测仅使用占位模型凭据，不是实时模型或生产部署证明。

## 权威关系

项目规范的含义由安全不变量、领域语义文档、机器契约共同确定；发现冲突时先修复规范与测试，禁止悄悄选择更宽松的行为。

外部协议事实以日期固定的 MCP 2026-07-28 详细规范为主要依据；旧服务器以 2025-11-25 的独立兼容 profile 处理。MCP 说明页、博客和 SDK 示例不一致时，记录差异，遵循详细规范并通过实际 SDK 互操作测试确认。

本包的 `/v1/*`、Capability、Run、Action 与错误 code 是**本项目设计**，不是 MCP 标准，也不声称注册了新的互联网标准。

## 交付等级

- **v0.1 可运行内核**：P00–P05；模型—工具闭环、HTTP 契约、Postgres 恢复、基本隔离与审批通过测试。
- **v0.2 互操作与扩展**：P06–P07；MCP、可选分类器与受限子任务通过各自测试。
- **v1.0 候选**：P08；全套故障注入、端到端评估、运维演练和限制说明完成。没有实测结果不得称为生产就绪。

## 许可证与公开状态

当前 `pyproject.toml` 明确标记为 `Proprietary`。即使仓库被公开查看，也不代表
授予复制、修改或再分发权利。若要开放协作或允许复用，需要由项目所有者明确
选择许可证并新增对应 `LICENSE`；本项目不会在没有该决定时自动套用开源许可。

## 静态校验

已有 Python、PyYAML、jsonschema 时运行：

```bash
python scripts/validate_design.py
```

校验对象是文档链接、JSON Schema、示例、OpenAPI 的本地引用和基本结构、阶段与验收编号。**不是完整 OpenAPI 官方一致性验证，也不是 Harness 测试。** 实现阶段还必须加入 OpenAPI validator 与真实请求契约测试。

## 来源与归属

研究来源见 `docs/SOURCES.md`。Elastik PPT 的资源寻址、原始产物与派生表达分离是用户提供的设计启发，不是本项目自行发明，也不是可靠性证明。pythond、curldb、TypeSafe 不作为强制生产依赖；先隔离在适配器后验证。复制上游代码前保留并检查实际许可证，不把他人的设计或实现说成原创。

## 导航

[总体架构](docs/01_architecture.md) · [HTTP契约](docs/02_http_contract.md) · [状态与恢复](docs/03_runtime_recovery.md) · [安全](docs/04_security.md) · [测试与评估](docs/08_testing_evaluation.md) · [阶段路线](docs/10_roadmap.md) · [内部接口](docs/12_ports_and_execution.md) · [Codex总指令](CODEX_MASTER_PROMPT.md)
