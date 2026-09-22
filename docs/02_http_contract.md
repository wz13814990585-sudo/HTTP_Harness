# HTTP 契约、能力目录与模型操作表示

本章与 `contracts/openapi.yaml`、`contracts/schemas/` 共同定义本项目 profile。标准依据 R23、R24、R26、R28、R29；额外字段和默认策略属于本项目。

## 1. 路由原则

`GET/HEAD` 读取已授权资源，不触发模型生成、外部发布或后台代码执行。访问日志、计量等附带记录不等于用户要求的业务副作用。需要计算生成的请求使用 POST；新产物与原始产物分开保存。

重要路径：

```text
GET  /v1/capabilities
GET  /v1/capabilities/{capability_id}
GET  /v1/openapi.json
POST /v1/conversations
POST /v1/conversations/{conversation_id}/messages
POST /v1/runs
GET  /v1/runs/{run_id}
GET  /v1/runs/{run_id}/events
GET  /v1/runs/{run_id}/event-history
POST /v1/runs/{run_id}/cancellations
GET  /v1/actions/{action_id}
GET  /v1/input-requests/{input_request_id}
POST /v1/input-requests/{input_request_id}/responses
POST /v1/execution-sessions
POST /v1/execution-sessions/{session_id}/executions
GET  /v1/execution-sessions/{session_id}
GET  /v1/workspaces/{workspace_id}/files/{file_path}
PUT  /v1/workspaces/{workspace_id}/files/{file_path}
POST /v1/artifacts
GET  /v1/artifacts/{artifact_id}
GET  /v1/artifacts/{artifact_id}/content
POST /v1/integrations/{integration_id}/operations/{operation_name}/invocations
```

这里故意不占用未注册的 `/.well-known/agent` 名字。无需发明新消息总线协议也能做资源发现。复杂插件的 native 资源可以扩展路由；未知外部 RPC 工具默认用 POST invocation，不凭名字假定是安全 GET。

`file_path` 是逻辑相对路径，不是宿主机绝对路径。OpenAPI 中它是普通模板参数；真实实现要显式使用 catch-all routing，并以契约测试核实斜杠编码，不能靠 OpenAPI 自动保证正确。

## 2. 接受任务与完成任务分离

创建 Run：先提交 Run、请求幂等记录、初始事件与 job，提交成功后返回 `202` 和 `Location: /v1/runs/{id}`。断网不会撤销已经接受的 Run。

获取失败 Run 时仍可返回 HTTP 200，因为查询成功；`status=failed` 才是业务结果。HTTP 请求失败用 RFC 9457 的 `application/problem+json`，并附本项目的稳定 `code`。工具进程正常运行但测试失败，用工具结果中的 exit_code / outcome 表达，不能一概变成 HTTP 500。

## 3. 请求幂等契约

所有公开 POST 和 PUT 命令要求 `Idempotency-Key`；读取无需该键。键为本 profile 自定义的 1–128 个 ASCII 安全字符，不宣称完整符合已过期 IETF 草案。服务端 scope 为 `(tenant_id, subject_id, method, canonical_route, key)`。

在同一 scope 下，相同指纹返回既有接受结果及同一资源地址；不同指纹返回 `409 idempotency_conflict`。必须先重新认证/授权才可读取历史接受结果，避免过期权限通过 replay 命中获取数据。并发请求由数据库唯一约束裁决，不能只靠内存字典。

指纹以 method、已规范化目标、query 的确定性表示、content type、受支持的条件字段和正文摘要构建。JSON 正文先拒绝重复键和非有限数，再以明确的项目序列化规则排序 keys、保留 array 顺序、UTF-8 编码；不是自行宣称 RFC 8785 完全规范化。文本/二进制按字节摘要。不能包含 trace id、认证密钥或每次不同的请求 ID。

活动 Run 的键不得过期；终态后默认保留 30 天，具体值是可调整项目配置，不是 HTTP 保证。模型不同轮次重复提出同一业务操作仍会有不同准入键，因此幂等键不能替代语义重复检测或发布审批。

## 4. 文件条件写

读取文件返回强 ETag 对应已提交版本。覆盖必须带 `If-Match`；新建必须带 `If-None-Match: *`，两者不得同时出现。缺少前提返回 `428 precondition_required`；前提不满足返回 `412 precondition_failed`。实际比较必须发生在提交版本指针的同一事务内，不能先检查后随意写磁盘。

初版使用受管理的版本化文件：不可变 blob + 数据库版本指针；需要在沙箱使用时 materialize。若直接编辑 OS 文件，原子文件替换和数据库记录之间存在额外崩溃窗口，必须另加日志与对账，不能沿用数据库事务的保证。

## 5. Capability 不是只有 URL

能力定义包含 ID、版本、方法与 path template、输入输出 schema、说明、权限、副作用类型、执行超时、并发键、是否支持下游幂等/对账。安全相关 binding 由管理员配置，不能信任服务端工具自报的 `readOnly` 或模型生成的 risk。

目录按 capability ID 稳定排序并分页；只暴露当前主体有权发现的能力。模型先得到紧凑目录，需要时再加载单个完整 schema。OpenAPI 是 HTTP 描述源之一，不表示要把完整 OpenAPI 文档每轮塞给模型。首版只导入经过审阅的有限 OpenAPI 子集；复杂鉴权、callbacks、递归 $ref、非 JSON 编码需要明确拒绝或专门适配。

目录响应可带私有 ETag / 缓存提示。服务端缓存键包含 tenant、主体授权指纹、policy revision 和 catalog revision，不能仅按 URL 缓存敏感目录。目录缓存命中也不能跳过 dispatch 时的授权。

## 6. 模型界面

默认只提供结构化 `harness_request` 工具，参数遵循 `http_operation.schema.json`。模型输出相对路径和受限字段，不写 Host、Authorization、Cookie、Content-Length、Transfer-Encoding、tenant 或 actor。模型也不能调用输入审批、管理配置、创建系统事件等管理路径。

JSON/文本/产物引用是 body 的可选形式，只有对应 capability 支持时才允许。实际 HTTP 序列化和秘密注入由 Adapter 完成。模型使用 JSON 表达操作不违背 HTTP-native；JSON 是描述结构，非第二套可绕过 HTTP 契约的业务路由。

原生函数工具视图可从相同 Capability 生成，用于兼容和公平实验。原始 HTTP 报文输出只是可选研究模式，不是生产默认值。

## 7. SSE 与持久化历史

`/events` 是 SSE 视图，`/event-history` 是分页 JSON 视图，读同一份已提交事件。SSE id 使用 Run 内单调 sequence；Last-Event-ID 从最后已消费的位置恢复。客户端按 `(run_id,seq)` 去重，只承诺同一 Run 的顺序，不承诺全局顺序。

无游标从保留历史开始；游标过期在发流前返回 `410 event_cursor_expired`，带 snapshot/history 链接；未来游标返回 409。已开始的流落后过多则发送 reset 提示并断开。断开订阅绝不能取消 Run。原生浏览器 EventSource 无法任意设置认证 header 时用受控 fetch streaming；不要把长期密钥写 query string。

进度 token 流可丢弃，不是执行事实；动作完成事件必须先写库再发送。服务重启后依靠数据库历史恢复，不依赖 worker 内存列表。

## 8. 错误与重试

错误类别至少区分 schema_invalid、policy_denied、approval_required（通过 InputRequest 表达，不用 403 自动升级）、precondition_failed、rate_limited、capacity_unavailable、dependency_failed、outcome_unknown、budget_exhausted。

429/503 只是参考信号。Retry-After 是等待提示，不代表重试一定安全。是否可重试还要检查 Action 类型、是否已 dispatch、下游保证、剩余预算和截止时间。412 需要重新读取/重算或再次审批，不盲目用旧补丁重试；403 是拒绝，不能当作获得更高权限的入口。

## 9. 目录与协议导出的最小范围

Capability 的参数契约描述逻辑输入与序列化绑定，GET 的 query/header/path 参数不表示必须附 JSON body。公开 OpenAPI 只描述当前实现且当前主体可发现的资源；认证/运维接口不下发给模型。已有完整契约文件可以包含未来阶段路径，但服务宣告 supported capabilities 时必须过滤未实现项。

Reconciliation 的 path action_id 与 body中已有action_id必须相等，否则400；更改该schema时需同步机器契约和示例。操作者不能通过重复身份字段覆盖path资源授权。
