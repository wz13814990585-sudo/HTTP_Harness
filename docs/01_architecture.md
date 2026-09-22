# 总体架构与模块边界

状态：本项目拟定规范。外部机制来源见 R11–R18、U01；以下组件划分与取舍是设计建议。

## 1. 目标与不做什么

目标是通用的单租户试用起步、可扩展到有隔离校验的多用户 Harness：可发现的 HTTP 能力、自然语言驱动的执行、明确权限、异步运行、故障恢复、执行证据和可引用产物。新项目不导入 MiniCodex 的历史业务模块。

初版不做：通用 DAG 编辑器、跨区域调度、无限多 Agent、任意网站自主登录、默认公网代码执行、任意 host shell、自动安装任意依赖、全量长期记忆、生产级多租户沙箱保证。

## 2. 四种不同的“语言”

模型界面负责理解和提出动作；HTTP 资源契约负责表达操作；领域状态负责持久化执行事实；Adapter 负责和具体世界通信。不要试图用一条 HTTP 报文取代这四层的全部结构。

`HttpOperation` 是模型提出的方法、相对目标、查询参数、允许的内容协商/前提字段与 payload。它没有可信身份、租户、批准状态或系统事件权限。内核解析后得到 `BoundOperation`：带已核实的 capability 版本、主体、授权范围、动作 ID 与下游绑定。这两个类型不互相等价。

## 3. 部署拓扑

```text
CLI / SDK / future UI
        |
    HTTP API + SSE                模型服务（外部，可替换）
        |                                  ^
Auth + Resource routing                     |
        |                             ModelProvider
        v                                  |
+-------------------------------------------------------+
| Harness Kernel                                        |
| RunController   Runner   ContextBuilder   Policy       |
| ActionGateway   Scheduler   Recovery   CompletionGate |
+---------------------+-------------------------+-------+
                      |                         |
             PostgreSQL RunStore          Executor Ports
             + durable jobs               /      |      \
             + append-only events      Native   HTTP     MCP(optional)
             + request identity          |        |        |
                      |                  +---- Execution Broker
             Artifact Blob Store                    |
                                          isolated runtime / pythond
```

先使用模块化单体：API 和 worker 可是同一代码库的独立进程；Postgres 作为唯一必需状态服务；sandbox broker 放在独立信任边界。不为每个 Agent 角色部署微服务。

## 4. 内核组件职责

| 模块 | 负责 | 不负责 |
|---|---|---|
| RunController | 事务内校验并提交 Run/Action 状态变化 | 让 LLM 直接 set status |
| Runner | 组织本轮上下文、模型建议和结果交付 | 解析 socket、写 SQL 细节、实现沙箱 |
| ContextBuilder | 组装目标、必要历史、证据引用、能力目录 | 从日志恢复执行事实 |
| Policy | 权限、审批、资源范围和预算约束 | 使用分类置信度当作授权 |
| ActionGateway | 绑定能力、校验、准入、创建动作记录 | 不受控地按模型 URL 发请求 |
| Scheduler | 租约、并发配额、到期唤醒、worker 分工 | 决定用户目标的语义 |
| Recovery | 检查动作状态、句柄、未知结果并恢复 | 盲目重放历史 HTTP |
| CompletionGate | 校验当前目标的必要完成证据 | 宣称任意开放式答案均可机械证明正确 |

## 5. 一个入口，不能出现“双重 Action”

人通过真正 HTTP 调用本地资源；Runner 可通过 in-process 方式调用同一 ResourceDispatcher。二者必须复用身份范围检查、操作绑定、参数校验与 ActionGateway。

ActionGateway 负责一次性准入，产生唯一 Action。worker 执行这个已准入 Action 时使用内部 `execute_bound()`，不再次调用公开准入路径。远端 ExecutorAdapter 可以真正发 HTTP，但不会因此在本地再生成一套嵌套 Action。此规则防止“POST 执行 → 又创建 Action → 再 POST 执行”的递归。

真实通信日志标记 `transport=http`，进程内记录标记 `transport=inproc`；不得伪造并不存在的网络通信。核心业务约束对两者相同。

## 6. 资源身份

AgentDefinition 是版本化配置；Conversation 是用户消息历史；Run 是一次目标执行；Action 是一次已经准入的工具或资源操作；ModelCall 是模型请求的持久化记录；ExecutionSession 是执行进程状态；InputRequest 是等待补充或审批；Artifact 是不可变产物；Event 是内核已确认的事实。

Run 引用一个 conversation、一个 agent 配置版本，可以关联多个 Action。Action 可拥有多个 transport attempts，但所有同一业务操作的重试共享逻辑 Action ID 和业务幂等键。ExecutionSession 的消失不能删除 Run 的审计信息。

## 7. 强不变量

每个模型产生的外部副作用必须先有持久化 Action；每个终态必须有已提交的依据；模型不能审批自身动作；未完成的危险副作用不得因网络超时自动重发；权限必须在真正 dispatch 前再次校验；恢复不能凭聊天摘要猜状态；Action 的最新状态、对应 Event 和唤醒记录必须原子提交。

数据库租约只防止旧 worker 提交本地状态，不能单独保证外部系统 exactly-once。外部副作用必须依赖下游幂等、可查询操作句柄或人工对账；否则诚实保留未知状态。

## 8. 扩展而不破坏内核

插件使用明确生命周期钩子；每个钩子有超时与失败策略。强制安全检查属于内核，不能被插件短路绕过。Strategy 可替换简单执行与计划式执行，但不能换掉状态持久化和执行网关。

以后实现 child run 时，用父子 Run 关系复用现有状态机、审批、事件和预算，而不是引入第二个多 Agent 总线。子任务的权限是父权限的交集，预算从父预算原子预留，不能自我扩权。

## 9. 直接 HTTP 调用的 Run 归属

Runner 的可信执行上下文绑定 run_id，模型不能通过自填 header 改归属。外部诊断客户端直接调用有副作用资源且没有已授权 Run 上下文时，Gateway 创建一个单操作 implicit Run，并在同一准入事务绑定 Action；纯GET诊断读取可只写访问审计。外部客户端需要向已有Run发命令时，由服务端验证并绑定该主体与Run的范围，不能相信模型提交的身份字段。模型驱动的只读操作仍保存Action/observation以支持恢复；普通状态查询不递归创建新的Run。
