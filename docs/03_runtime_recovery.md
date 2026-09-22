# 执行内核、状态机与故障恢复

以下为设计规范。检查点与中断的参考机制见 R13、R14；HTTP 重试约束见 R23。具体状态在 `contracts/state_machines.json`。

## 1. Run 生命周期

Run 可为 queued、running、waiting_input、blocked、cancelling、succeeded、failed、cancelled、expired。succeeded/failed/cancelled/expired 为终态，终态不能重新变 running；重新尝试整个任务创建新的 Run 并引用父记录。

`waiting_input` 表示存在可回答的补充问题或审批；`blocked` 表示结果未知、依赖丢失或需要人工对账；两者不能混在普通失败里。存在 unresolved external effect 的 Run 不得静默进入“干净”的终态。预算/截止时间到达时停止派发，清理已运行操作；清理无法确认则 blocked 并标明原终止意图，而非谎称已取消。

API 的取消请求只表达取消意图。Run 已完成则返回 409 already_terminal；已取消请求的幂等重放返回原受理结果。完成与取消通过同一聚合版本串行裁决：完成先提交则取消不得推翻；取消先提交则禁止新的业务 dispatch，等待已有操作的真实结果。

## 2. Action 生命周期

Action 可为 proposed、waiting_input、ready、running、waiting_external、retry_wait、outcome_unknown、succeeded、failed、cancelled。

Action 的 retry 指同一逻辑动作的 transport attempt 增加；参数语义改变则产生新的 Action，旧动作不能原地改成另一个请求。MRTR 的补充输入属于有协议定义的 continuation，保留原业务参数、同一逻辑 Action、单独 continuation 记录。

Action 终态 immutable。迟到响应可记为新证据，不由过期 worker 直接改写终态；允许对未知状态进行明确的 reconcile，再由当前有效内核提交状态。

## 3. 单轮执行算法

1. worker 获取 Run 的租约与 fencing token，加载已提交检查点。
2. 检查取消、截止时间、未解决的 Action 与预算。先完成恢复，不能直接重新问模型。
3. ContextBuilder 生成有版本/来源的上下文快照；写 ModelCall intent，调用模型并保存完整可见响应和 usage，不记录或索取隐藏推理。
4. 对一个完整且通过结构校验的模型响应，先保存输出，再按稳定 `(run_id,turn_id,call_index)` 建立动作槽位。重复提交同一响应不能重复建 Action。
5. 将建议经过 capability binding、参数验证、权限/预算/审批检查。最多对非法格式做有上限的修复，不无限循环。
6. 执行准入 Action。同步读取可立即返回；异步动作保存句柄，scheduler 查询/唤醒，不能让 LLM 无休止调用 poll。
7. 将结果、证据摘要、事件与下一步 job 事务提交。只有提交成功的结果才进入下一轮上下文。
8. 模型建议结束时执行 CompletionGate，成功才提交 Run.succeeded；否则返回缺失证据，预算不足则明确失败/阻塞。

模型调用响应丢失可能需要重发模型请求并重复计费，不承诺这部分 exactly-once。禁止在此模式下启用模型提供商不受本地网关控制的 hosted side-effect tools。

## 4. 提交边界

准入事务写 Action、冻结请求指纹、批准依据、预算预留、action.ready 事件和 durable job。执行前短事务确认 Run 未取消、租约有效、授权仍有效、资源前提未过期，然后开始 attempt 并提交 running。外部调用在事务之外执行，避免长时间占数据库锁。

结果事务通过版本/租约条件写 attempt 结果、Action 状态、event、budget settlement 和唤醒 Run 的 job。事务失败时不重复对外执行，而是进入 recovery。

所有 HTTP 202 必须发生在准入持久化之后。通知可通过 Postgres NOTIFY 优化，不能依赖通知不丢；周期扫描持久 jobs 兜底。

## 5. 租约与并发

队列表短事务使用 FOR UPDATE SKIP LOCKED 领取任务（R27）；运行期依靠 `lease_owner / lease_until / lease_epoch`，以数据库时间判断有效期。建议起始配置 30 秒租约、10 秒续租；它们是待测试的配置值，不是性能保证。

每次状态提交携带 expected_version 和 lease_epoch。旧 worker 不能提交新事实。失去租约时停止新 dispatch；已经在外部执行的操作仍需对账。不能声称 fencing token 能阻止不识别该 token 的外部 API 产生副作用。

Run 的模型决策串行；独立只读 Action 可有界并行；同一执行会话一次只执行一个 cell；同一资源的写操作用明确 concurrency key 串行化并保留 ETag；child run 使用独立命名空间。

## 6. 四类副作用

| effect_semantics | 故障后的原则 |
|---|---|
| read_only | 可重试，但返回值可能随时间变化；回放分析优先使用已保存 observation |
| local_transactional | 用数据库内 effect ledger / 版本指针确认执行结果 |
| remote_idempotent | 同一业务幂等键重试，受下游去重作用域和有效期限制 |
| unsafe | 发出后结果不明即 outcome_unknown；只查询或人工对账，不自动重发 |

在取消窗口内，`artifact.create` 和条件 `workspace.file.write` 若已提交本地资源事务、但 Action 回执丢失，`cancel_run` worker 仅查询已持久化的幂等记录、请求指纹及对应 Artifact 的归属/来源，不再次派发写入。匹配后由内核把 Action 确认为 succeeded，再确认 Run.cancelled；没有记录、记录不一致或仍有其他未决效果则继续 `cancelling`。这只证明本地账本中的效果，不证明外部 API 停止，也不保证独立 Blob 文件永不丢失。

人工对账和 MCP Task 取消也不能只凭某一个 Action 的收据终结整个 Run：必须确认同一 Run 的所有 Action 均已解决、子 Run 已终结。取消期间确认某次操作「未生效」应把该 Action 置为 cancelled，而不是重新变成 ready 等待派发。

任意 Python 代码不能仅因当前示例“看起来只计算”就标为 read_only。隔离环境阻止出站与宿主写入，但它仍可能修改自身会话状态，因此默认不可任意重放。可重算步骤需要显式声明且约束输入/输出边界。

## 7. 崩溃点与恢复

| 崩溃位置 | 恢复动作 |
|---|---|
| 准入事务提交前 | 无已接受动作；客户端按原 key 重试 |
| 已准入，尚未开始 attempt | 新 worker 可领取 ready 动作 |
| running 已提交，但不知道字节是否送达 | 保守视为可能 dispatch；按副作用类型判断 |
| 外部成功，响应丢失 | 查询下游句柄/业务 key；不支持则 outcome_unknown |
| 收到结果，结果事务提交前 | 用可恢复 receipt/下游对账，不重复写操作 |
| 结果与事件已提交，SSE 未送达 | 从数据库事件游标续传 |
| 等待审批时进程退出 | 重新加载 InputRequest，继续等，不重新执行或默认批准 |
| pythond 进程退出 | 标记 session generation 失效；只恢复显式可重算步骤/持久产物 |

## 8. 恢复不是重放

inspection replay：只用历史记录重建时间线/上下文，不调用外部世界。

simulation replay：替换为录制结果或受控 fake provider，用于确定性测试。

re-execution：重新执行外部操作，是新的有权限和副作用检查的行为，默认禁用批量危险重发。

curldb 的报文 replay 属于第三类，不应自动接到 worker 恢复路径（R20）。

## 9. CompletionGate 与无进展

Run 的成功条件包括必要 Action 无未决状态、必要 Artifact 可读取且版本吻合、声明的验收项有证据、没有 pending approval/未知副作用。开放式内容仅能机械核实结构和引用，语义质量要使用用户评审或模型辅助，不能把概率评分当作证明。

区分 acceptance（当前目标）与 regression（系统既有测试）；计划勾选和模型自述不是证据。无进展检测看动作指纹、资源版本与新增 observation，不因单次错误立即回滚。重复读取且无新版本可提出恢复建议；仍由预算和明确 stop rule 终止。
