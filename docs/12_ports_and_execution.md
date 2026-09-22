# 实现接口与执行时序

以下是建议的内部接口契约，不是已经运行的源码。Codex应先以Protocol/typed DTO落实，再通过真实适配器测试细节。类型定义与机器schema共同演进，不能复制出三套不同语义。

## 1. 核心 ports

| Port | 输入 | 输出 / 保证 |
|---|---|---|
| ModelProvider.generate | ContextSnapshot、允许的工具视图、预算、取消信号 | 完整ModelTurn；public output、operations、usage、provider request id |
| CapabilityRegistry.resolve | 已授权主体、method、target、query、content type | 唯一能力版本和受信任binding；未知/歧义/未授权拒绝 |
| Policy.evaluate | TrustedContext、BoundOperation、资源版本、预算 | deny / allow / require_input；模型不能设置该结果 |
| ActionGateway.admit | 可信Run上下文、HttpOperation、稳定slot | 已持久化Action；返回action handle或同步已提交结果 |
| ToolAdapter.execute_bound | 不可变BoundOperation、AttemptContext、取消信号 | complete / input_required / accepted_external；异常需说明dispatch证据 |
| ToolAdapter.reconcile | 已保存handle/key、Action版本 | applied / not_applied / still_running / unknown；不得靠LLM猜测 |
| ToolAdapter.cancel | 真实可取消handle | requested / stopped / unsupported / unknown；requested不是stopped |
| RunStore.transaction | expected_version、lease_epoch、所需写集合 | 全部提交或回滚，Run/Event/Job/Budget一致 |
| BlobStore.put/get | bytes/stream、声明MIME、主体scope | immutable content reference；内容完成后才发布可读取引用 |
| CompletionGate.verify | FinalCandidate、验收约束、已提交证据 | verified / missing_evidence / semantic_review_required |

`HttpOperation`不携带可信tenant/actor；`BoundOperation`不交给模型修改。`AttemptContext`包含logical action ID、attempt no、下游幂等键、deadline、secret refs和lease，不把secret value写入持久日志。

## 2. 结果不要只留字符串

完整结果应保留：应用结果是否成功、原HTTP/RPC状态、exit_code、有限文本摘要、结构化内容、Artifact引用、已知副作用状态、下游handle、是否需要补充输入、实际usage。对于不同适配器，允许不适用字段为空；禁止统一抹成“200 OK / error string”。

ExecutionAdapter已经收到完整结果但DB暂时失败时，应优先重试提交该结果或使用可信receipt对账，而不是重新执行工具。任何可重试建议都由内核与effect contract复核。

## 3. 一次执行的消息顺序

```text
Client         API/Kernel             PostgreSQL            Worker           Executor
  | POST Run       |                      |                    |                 |
  |--------------->| BEGIN admission     |                    |                 |
  |                | Run/Event/Job/Key-->| COMMIT             |                 |
  |<---202 + URI---|                      |                    |                 |
  |                |                      |<---claim job-------|                 |
  |                |                      |  save ModelTurn <--|<-- model reply   |
  |                |                      |  admit Action  <--|                 |
  |                |                      |  running attempt<-|                 |
  |                |                      |                    |---execute------>|
  |                |                      |                    |<--receipt-------|
  |                |                      | result/event/job<--|                 |
  | GET events---->|<--committed events---|                    |                 |
  |<---SSE---------|                      |                    |                 |
```

模型调用服务与Executor是不同port，图中model reply仅表示完整模型响应，不代表Executor负责模型调用。实践中请用独立span标注模型、队列、准入、执行、结果提交。

## 4. 两种时序必须阻断

错误一：`model stream chunk -> parse some JSON -> execute immediately -> later persist`。必须等完整可校验响应落库后才准入Action。

错误二：`public POST execution -> create Action -> worker POST same public endpoint -> create another Action`。内部执行使用execute_bound；真正远端HTTP使用已经冻结的binding，不再次走本地“创建任务”入口。

## 5. 补充输入与多Agent的接口边界

模型可提出澄清问题，但InputRequest的ID、版本、主体与状态由内核生成。对approval，问题正文来自冻结动作的可信摘要，不能由模型随意删去风险内容。

ChildRunProvider可以晚于v0.1实现，其返回值是受父任务约束的Run handle。wait/join由调度器管理；父模型不通过重复GET占满tokens。没有child能力时明确unsupported，不生成“模拟子Agent”成功结果。

## 6. 编码时优先完成的最小切片

第一切片：POST Run→真实Postgres→GET Run，不调用模型。第二切片：ScriptedProvider→读取受管文件→FinalCandidate→证据检查。第三切片：真实模型→同一能力→保存产物。第四切片：隔离Python→人工审批→可对账外部写。

每个切片都具有实际HTTP入口、持久化事实和测试，不等待几十个抽象类全部创建后才尝试运行。

## 7. OpenAPI、能力描述与后端绑定必须同源

公开Capability提供逻辑input_schema；可信BackendBinding负责把同名逻辑参数放入path/query/header/body，并保存执行器、目的地、secret reference与effect约束。绑定是管理员注册的数据，不是模型输出。第一次实现不要支持“猜测哪些字段是query”。

一个受管文件的绑定可以是：

```yaml
capability_id: workspace.file.read
revision: '1'
openapi_operation_id: readWorkspaceFile
method: GET
path_template: /v1/workspaces/{workspace_id}/files/{file_path}
logical_arguments:
  workspace_id: {location: path, name: workspace_id}
  file_path: {location: path, name: file_path}
body: {mode: none}
executor: native_workspace
```

text执行的body使用显式text模式；JSON请求使用已验证JSON对象。数组query的重复键/顺序与percent encoding由声明的序列化方式决定，header只允许能力声明且不属于身份/秘密字段。未知参数不得静默丢弃。复杂schema/ref/编码未实现时拒绝导入并说明限制，不能猜测调用。

生成OpenAPI、紧凑HTTP模型视图和native function schema时都使用这份注册数据，并做contract snapshot tests。三个视图可以结构不同，但语义和允许参数应一致。完整schema的验证与未知字段处理规则由能力版本固定。
