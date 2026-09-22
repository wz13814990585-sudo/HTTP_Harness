# 技术选型、部署与运行手册

## 1. 建议基线

Python 3.12、uv 锁定依赖；FastAPI/ASGI 提供 HTTP；Pydantic v2 + JSON Schema 提供结构校验；httpx 提供 HTTP 适配；SQLAlchemy 2 + Alembic + psycopg 提供 Postgres 数据访问与迁移；asyncio/AnyIO 做有界并发；pytest + property/stateful tests 做验收；OpenTelemetry 统一 trace。

这些是本项目选型，不是“必须最新版本”。Postgres 17 是可选已知基线，不称其最新；实现时验证依赖兼容后提交精确 lockfile、镜像 digest 与版本报告。不要在设计文档里编造具体 SDK import API。

初版无 Redis/Kafka/Celery/Kubernetes 的强依赖；分类器、MCP、云对象存储和高级执行 backend 为可选 extras。所有额外服务有明确故障行为，禁用后核心仍可工作。

## 2. 仓库布局

```text
src/hnh/
  domain/          # 纯状态、命令、事件、BoundOperation
  application/     # RunController、Runner、Policy、调度/恢复、Context、Completion
  ports/           # ModelProvider、ToolAdapter、RunStore、Clock、Broker、BlobStore
  adapters/
    postgres/      # repo/UoW/job领取
    models/        # 真实provider与明确标识的测试provider
    tools/         # native/http/pythond/mcp/typesafe
    sandbox/       # 可信执行broker
    blobs/
  transport/http/  # ASGI路由、schema编解码、认证、SSE
  worker/          # entrypoint/lease/heartbeat
  cli/             # run/watch/cancel/approve/inspect
  observability/
migrations/
tests/{unit,contract,integration,e2e,chaos,security}/
eval/
```


`domain` 不导入 FastAPI、具体 SDK 或数据库对象；`application` 不直接操作宿主 shell；`transport/http` 不复制内核状态机。不要建立无边界的 utils.py 或把每个 if 都拆成空模块。

## 3. 本地与受控部署

本地开发可在 Mac 编辑；真实进程隔离/限额用 Linux 容器环境测试。Apple Silicon 环境应验证镜像架构，不能仅在 x86 CI 上通过就假设 ARM 正常。

Compose 起点：api、worker、postgres、受信任 sandbox broker，加上按需 runtime 容器；模型服务是外部配置。API/worker 与模型生成代码不共享秘密和宿主工作目录。TLS 在可信反向代理或服务端终止；公网部署前启用真实身份与资源范围校验。

Broker 配置只接受受审阅镜像 digest，管理通道不能暴露给模型。容器运行需要的高权限不能误导为“普通 Agent 工具”。

## 4. 可观察性

持久化领域事件不能采样丢失；高体积 token/log 可以采样和截断。Trace 使用 W3C traceparent（R29）；metric labels 使用 route template、operation class、error category，避免把 run_id/path/user_id 当高基数 labels。

至少记录：任务终态分布、队列等待、模型/工具耗时、p50/p95/p99 内核开销、重试、outcome_unknown、审批等待、token/cost、context 大小、duplicate-effect 检测、lease 失效与恢复时间。实验先测真实值，不预填“95%”。

## 5. 服务故障行为

数据库不可用：停止新准入和新 dispatch，不退回内存成功模式。模型不可用：按模型错误策略有限重试，deadline 到达诚实结束。分类器不可用：默认策略继续。MCP integration 不可用：标记能力 unavailable，不影响无关能力。

Sandbox broker 不可用：运行等待/失败但不转而宿主 exec。SSE 客户端离线：Run 继续。Artifact 写入失败：不得提交 success 引用。事件发布失败：已持久化事件可后续读取。

## 6. 运维演练

发布候选必须完成备份恢复、数据库重启、worker kill、网络响应丢失、secret rotation、权限撤销、schema 升级与旧 Run 恢复测试。记录重建服务步骤、数据位置、所有 active handles、失效执行会话的处理方式。

升级分为 drain 新任务、完成或暂停在可恢复边界、备份、迁移、部署、重启扫描、验证。任务长时间等待用户时不要求服务保持同一进程存活。

## 7. 依赖与供应链

依赖/镜像锁版本；读取上游许可并保留必要 attribution；对低成熟度实验包单独限制权限。SDK 版本更新必须经过 contract 和 integration matrix，不把每次 `pip install -U` 当升级策略。CI 必需检查失败不得用 xfail/skip 掩盖。
