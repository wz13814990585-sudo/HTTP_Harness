# 架构决策记录（初始建议，实施时记录验证结果）

## ADR-001 · 统一 HTTP 语义，不强制所有内部调用经过网络

选择同一 ResourceDispatcher/ActionGateway 支持 in-process 和真实 HTTP。原因是语义一致性与传输拓扑是两个问题。拒绝把日志写入等内部函数全部改 HTTP。验收看不同 binding 是否保留授权、错误和恢复语义。

## ADR-002 · 一个耐久内核，不叠加多个工作流引擎

借鉴框架机制，但核心不同时依赖 LangGraph Runner、OpenAI Runner 和另一个自研 Runner。当前选择有限状态机 + Postgres 的实现范围；若以后采用耐久引擎，必须明确状态和调度所有权迁移。

## ADR-003 · OpenAPI / JSON Schema，而不是无 schema 的纯文本 curl

HTTP-native 不排斥 JSON。采用标准描述格式并提供紧凑模型投影。原始 HTTP 文本是实验，不作为唯一生产接口；不宣称模型对 HTTP 一定优于原生 tools。

## ADR-004 · Command、Event、Exchange 分离

请求是意图；事件是已提交事实；报文是通信证据。无法以 HTTP 报文重放替代事务记录。为了简单可在同一数据库，但不能合并语义。

## ADR-005 · 外部副作用不宣称通用 exactly-once

租约/唯一约束保证局部一致性；外部系统须支持幂等或句柄查询。未知效果保留 outcome_unknown 并阻塞，不为了“任务完成率”冒险重发。

## ADR-006 · 持久会话不是持久内存快照

pythond 的活进程与 checkpoint 是不同边界。默认保存安全产物，不承诺恢复任意 socket、线程、连接和 Python 对象。禁用不可信 pickle 路径。

## ADR-007 · 分类器是辅助，不是权限执行者

TypeSafe 可替换、默认关闭。是否启用由本地误路由与成本实验决定。模型置信度不能签发权限、替代审批或证明成功。

## ADR-008 · MCP 为边界兼容层

native resource API 独立；MCP 自己仍用官方线协议。现代和旧版 profile 分开测试，MRTR/Tasks 不支持时明确不 advertise，不用 stub 假装支持。

## ADR-009 · 先单 Agent，再受限 child run

子任务复用 RunStore、身份交集和预算台账。无必要不引入 manager/reviewer/critic 多循环；确认收益后才启用。

## ADR-010 · 成功由任务相关证据决定

工具 exit 0、HTTP 200、模型自述和计划勾选都不是同一层成功。CompletionGate 明确自己能检查什么，语义评价不伪装为机械证明。
