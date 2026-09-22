# 模型、上下文、策略与完成检查

参考机制：R11、R12、R15–R18、R21、R22。以下是本项目的模块方案。

## 1. ModelProvider

定义 `generate(context, tools, limits) -> ModelTurn`。支持完整响应、结构化工具调用、usage、取消/超时和服务错误；流式 token 只是展示。`ModelTurn` 只允许三种动作建议：operations、request_input、final_candidate；最终运行状态由内核提交。

初版提供 ScriptedProvider（确定性单元/集成测试）以及可替换的 Responses API
provider。当前开发 worker 与 live 评估默认使用 DeepSeek，并只接受官方当前列出的
`deepseek-flash`、`deepseek-v4-pro`；reasoning effort 作为受信任配置写入
`reasoning.effort`。HTTP-semantic 与 function-tool 两种表面分别经过本地契约测试，
reasoning item 不写入模型上下文或公开审计响应。OpenAI adapter 仍保留为独立实现，
但兼容 Responses 格式不等于所有参数行为相同；每个 provider 必须分别测试（R34）。

生成完整响应落库后才准入动作。部分流式 JSON 不可触发执行。Provider 报拒绝或不可解析输出时不能伪造成功；记录明确错误。记录公开输出、结构化行动和证据，不要求或存储隐藏思维链。

## 2. HTTP 语义输入与替代视图

默认工具 `harness_request` 使用 HttpOperation schema，规范化后经过能力专属 schema 第二次验证。泛化方法/路径提供一致行动空间，但模型仍然必须知道 path 参数、body 类型与权限边界；不能认为它见过 HTTP 就自然知道你的接口。

同一 Capability 可编译成 native function schema 作为另一种视图，交给同一个 ActionGateway。两种视图的安全与恢复不能不同，否则评估不公平。MCP Adapter 决定传输，不能决定模型必须手写 JSON-RPC。

## 3. ContextBuilder

每轮输入包括：固定系统约束、用户目标、当前 Run 状态与剩余预算、未完成验收条件、必要最近消息、已授权能力目录、与当前任务相关的工具结果摘要和 Artifact 引用。

正文过大时保存原始 Artifact，上下文仅保留摘要、范围、版本、来源和重新读取地址。对 binary 结果使用媒体/Artifact 引用，不能将二进制错误地 UTF-8 解码成文本。秘密、跨主体数据及未授权资源不因“提高上下文质量”被加入。

压缩保留：目标、审批约束、资源版本、已有结果与未解决问题。压缩不能改变 Action 状态；被压缩的旧文件如版本变化必须重新读。保留 source refs 后可检查摘要是否过时。长期记忆是经过授权和来源检查的派生数据，不能作为事件权威库。

## 4. Strategy

SimpleStrategy 是默认模型—工具循环；PlannedStrategy 仅多一个版本化计划，计划步骤有验收引用而非空勾。计划是建议，不能绕过 ActionGateway 或扩大预算。不要给每个 step 再套独立无限 reflection 循环。

并行由策略提出依赖关系，调度器依据 capability 和资源锁决定是否允许。并行读取可用；共享 Python session 的 cell 默认串行；相关写动作不能因为模型说“可并行”而无锁执行。

## 5. TypeSafe 决策插件

提供 `DecisionProvider.classify(request, allowed_labels)`；返回 label、confidence、distribution 与 provider/version。可选标签先限制为任务类别和执行策略建议，不让分类器决定授权、最终成功或 unsafe retry。

代码默认关闭，避免把额外网络模型变成启动必需项。当前开发 worker 可通过
`HNH_TYPESAFE_ENABLED=1` 显式启用 System One Choice adapter；它使用服务端
`TYPESAFE_API_KEY`、固定 HTTPS endpoint、短超时、禁止重定向/环境代理，并在任何
失败或低置信度时退回 `simple`。不得在 README 宣称更快/更准，除非有本项目同任务
数据验证。

TypeSafe 的 confidence 是从整个概率分布导出的统计量，不等于所选标签自身的概率，
也不等于真实正确率；以本地测试估计误路由、拒识覆盖与可靠性，需要时计算校准指标。
置信度高也不能替代用户审批（R22）。

## 6. Skills

Skill 是版本化指令和必要资源描述，不是可自动扩权的代码插件。只在需要时加载；每个 skill 声明依赖的 capability ID 和兼容版本。管理员允许的 skill 文件可信级别高于检索内容，但仍不能改变内核安全规则。

CLI 从配置读取批准的 skills 目录，不让模型任意读宿主 ~/.config。skill 更新后记录 revision，新旧 Run 的恢复使用各自固定版本。

## 7. child run

可选、晚于单 Agent 稳定阶段。父任务创建子 Run 时说明目标、结果 schema、允许能力、deadline 与预算分配。子任务只能读取显式共享产物；文件系统工作区独立。父 Run 通过 durable handle 等待，不用长连接串接；取消传播为意图，不假定外部副作用已经撤销。

初始 max_depth=2、max_children=4 只是保守配置，需要评测调整。多 Agent 的调用成本全部计入父级预留/结算，不能每个 child 都继承父的完整额度。

## 8. CompletionGate

FinalCandidate 附 answer、artifact_refs、evidence_refs 和 acceptance_claims；验收器检查证据是否实际存在、属于该 Run/允许共享范围、资源版本是否匹配、必要动作是否完成。不能接受用户文本或模型伪造的“测试通过”当执行证据。

验收器按任务类型扩展：文件结构、计算结果 schema、外部 receipt、测试退出码、必需字段、引用可追溯性。工具测试和自然语言质量评价分开记录；测试全绿不能证明回答满足所有语义目标。
