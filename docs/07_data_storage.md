# 数据模型、事务与存储

## 1. 起点：Postgres 是唯一权威运行状态库

不并行部署 curldb、Redis、Kafka、LangGraph checkpoints 等多份状态真相。借鉴 curldb 的 Exchange Journal 作为 Postgres 表/归档视图；实际 curldb 可选用于导出/调试，不作为生产状态双写目标。

初版采用 Postgres 持久 jobs + worker。SQLite 可用于纯结构单测，不能用 SQLite 通过测试来替代多 worker / 行锁 / 崩溃恢复验收。队列领取参考 R27。

## 2. 表与必要约束

| 表 | 关键字段与约束 |
|---|---|
| agent_definitions | id, revision, config, policy_ref；版本 immutable |
| conversations / messages | tenant/subject, message seq, role, body refs；role 由入口赋值 |
| runs | id, conversation_id, goal, status, version, agent_revision, policy_revision, deadline, parent_run_id |
| actions | id, run_id, turn_id, call_index, status, request_hash, capability_revision, effect_semantics, lease_epoch；slot unique |
| model_calls | run_id, turn_id, provider/model revision, context_ref, request/result refs, usage；unique(run_id,turn_id) |
| model_call_attempts | model_call_id, attempt_no, status, provider_request_id, usage/estimated_cost, response_ref；避免重发模型请求后只计一次费用 |
| action_attempts | action_id, attempt_no, dispatch_started, result_ref, upstream_handle；unique(action_id,attempt_no) |
| events | run_id, seq, event_id, type, schema_version, actor_ref, data refs；unique(run_id,seq) |
| checkpoints | run_id, checkpoint_seq, runner_cursor, context_ref, schema_version；不能只存自然语言摘要 |
| jobs | kind, run/action_id, due_at, lease_owner, lease_until, lease_epoch, attempts；可重复唤醒但不可重复副作用 |
| idempotency_records | scope hash, key, request_hash, accepted response/resource ref, expiry；唯一约束 |
| input_requests / responses | kind, action_id, request_hash, version, expires_at, decided_by；一次消费 |
| execution_sessions | owner/run, provider, generation, status, sandbox_ref, expiry |
| artifacts / artifact_refs | tenant, blob hash, MIME, size, storage key, source action, ACL refs |
| workspace_entries | workspace, logical_path, revision, blob_ref；unique(workspace,path) |
| http_exchanges | action/attempt, request/response metadata, redacted blob refs, transport kind |
| budget_ledger | run, reservation, settled usage, parent reservation；不能使用 float 计费 |

每个敏感外键和查询均保持租户/主体边界。多租户模式除应用检查外可以增加 Postgres RLS，但需要以非 owner 角色做真实隔离测试；不能用超级用户测试 RLS 后宣称隔离有效。

## 3. 事件与状态如何保持一致

RunController 的 transaction 读取聚合 expected_version，做合法迁移，再更新 snapshot、增加版本、插入 event 和下一步 job。Run 内 seq 在同一行锁事务分配；不要用应用 `SELECT MAX(seq)+1` 造成并发重复。

事件是 append-only 的审计事实；修正错误事件应追加 correction event。Snapshot 是可操作的当前状态，事件和 snapshot 都只从同一事务入口写；不是两套允许独立修改的数据库。定期 audit 检查终态证据引用、event seq 和 snapshot version。

## 4. 幂等准入事务

```text
BEGIN
  authenticate / authorize current subject (trusted context)
  insert or lock idempotency record by scoped key
  reject if fingerprint differs
  return prior accepted envelope if it exists and remains authorized
  insert run/action with immutable identity
  insert accepted event + durable job
  store response status + location + accepted body
COMMIT
return accepted response
```

返回的旧接受 body 不一定是任务当前状态；Location 可查询最新状态。不得将当前状态查询和幂等回执混成不稳定响应。

## 5. Durable job 不是外部执行投递保证

Job 的内容是“检查这个 Run/Action”，不是“无条件再次发这条 HTTP”。worker 领取后必须读取领域状态、effect 类型和 lease 再做决定。过期 running 的 unsafe action 进入 outcome_unknown，不回 ready。

可使用 outbox 命名记录唤醒/通知，但只有一个 durable job/outbox 机制即可；不为了架构漂亮增加第二张重复队列表。Postgres NOTIFY 只做低延迟唤醒，扫描 jobs 做可靠性兜底。

## 6. Artifact 原子性

文件内容先写临时 blob，计算 hash 和大小，通过受控 rename/对象写完成后，事务登记 metadata 与引用。失败留下 orphan blob 可由 GC 回收；不能先向用户发布可读取引用，再发现 blob 尚未写成功。

Workspace 文件是版本指针，写入在同一事务比较 ETag 并改指针。沙箱挂载/复制工作区是派生视图；需要把新文件导回权威存储时显式运行 import/commit 操作。Blob hash 支持内容完整性，不能证明语义正确或作者诚实。

## 7. 保留与迁移

活动任务的 checkpoint、关键结果和幂等映射不能过早 GC。终态后的 event、内容、trace 与 secret retention 分开配置；删除数据时保留必要的最小审计元数据，具体合规期限由部署方决定。

每条事件、checkpoint、capability、配置都带 schema_version。恢复固定模型/工具/策略引用；版本不兼容时 blocked: migration_required，不随机使用最新版继续旧任务。数据库迁移使用版本化迁移工具并测试空库升级、上个版本升级与备份恢复。不能依赖生产 downgrade 一定无损。
