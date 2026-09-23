# 测试、评估与证据交付

本章给出待实现测试，不包含任何 Harness 实测成绩。机器清单见 `eval/acceptance_cases.yaml`，实验变量见 `eval/experiment_design.json`。静态 schema 检查、fake provider 仿真、真实服务互操作、真实模型任务是四种不同证据，报告不能混淆。

## 1. 分层测试

Unit/property：状态机不变量、审批指纹、路径规范化、重试决策、预算保全、结果映射、终态不可逆。

Contract：OpenAPI 3.1 validator、请求/响应 schema、实际路由一致性、错误类型、header/precondition/idempotency、SSE 游标、capability 二次参数校验。

Integration：真实 Postgres 与迁移、API+worker、并发事务、blob commit、受控HTTP测试服务。SQLite不替代这些证明。

Security：租户/主体越权、SSRF、秘密脱敏、审批重放、技能/工具内容prompt injection、sandbox资源/网络限制。

Chaos：明确注入点kill进程、断连接、延迟响应、过期租约、丢失执行会话、等待审批时重启、提交后失去结果。每个故障必须能从DB、远端计数和事件序号核实。

Live provider：使用真实模型完成至少一条跨多个能力的任务，记录模型/配置/usage；没有key时标 blocked_environment，不使用fake冒充完成。

## 2. 一个最小但有价值的测试工具服务

Codex 应实现 `tests/support/effect_server`，不用于生产。它提供读取、带幂等键写计数、无幂等写计数、查操作结果、慢任务、丢弃响应、返回500但已经写成功、MRTR/Tasks协议测试 fixture等行为。服务保留独立effect ledger，用它验证Harness是否重复操作，而不只检查Harness自己说了什么。

一个可控故障点例如 `after_effect_before_response` 必须真正让远端效果已经存在但客户端拿不到响应。用简单 `raise TimeoutError` 替代全部真实网络窗口不足以验收。

## 3. 用例组织

每个AT编号至少对应一个测试函数/参数化case；`reports/acceptance_status.json` 记录编号、文件/函数、运行命令、状态、真实输出证据。状态只能为not_implemented/passed/failed/blocked_environment/not_applicable，必须带原因。`skipped`不能被汇总成通过。不能仅根据测试函数存在更新passed。

关键安全门槛是零次已观察到的越权和危险重复副作用；它不是数学安全证明。发现一个失败就阻止发布，不能用“总体95%通过”掩盖。

## 4. 公平比较HTTP与MCP

两条轴必须拆开：

| | 下游原生HTTP | 下游MCP适配器 |
|---|---|---|
| 模型看原生函数schema | A | B |
| 模型看HTTP semantic operation | C | D |

模型通常通过SDK看到工具说明和schema，而不是JSON-RPC线报文（R12）。不得把“要求模型手写完整MCP JSON-RPC”当作常规MCP baseline，或把没有进上下文的网络字段计入token节省。

首先验证各组工具行为、权限、输入信息和结果等价。按相同任务/随机种子/模型配置执行，保留失败样本。目录按需加载、分类器、子任务是独立ablation，不同时打开所有改进后把效果归给HTTP。

下游协议轴不能顺便改变模型看见的 capability ID、函数名、路径模板、版本、说明或输入输出 Schema。当前 echo 评估在两种下游都暴露同一 `native.echo` 契约；MCP 组只在可信执行绑定处把已准入 Action 映射到预检锁定的远端工具。原始 MCP RPC 账本和 Action 收据仍保留真实 integration/tool 与协议结果，不能为了界面等价伪装成一次不存在的原生调用。不同 Run/Action 的持久化 ID 仍会出现在上下文中，因此这里证明的是能力目录等价，不宣称整个请求字节完全相同。

当前只读 echo 四组预检要求远端 MCP 输入覆盖原生 `message` 字符串域，输出 Schema 明确声明必需的字符串 `message`；额外输出字段可以存在，但模型观察和验收仅投影共同的 `message` 字段。通用 `dict[str, str]` 的 Schema 不能保证该键存在，因此预检会在创建 Run、调用模型、打开原始数据文件前拒绝它。即使 Schema 合格，真实响应仍需由已提交 Action 和独立 oracle 核对；预检不是远端行为正确性的证明。

执行顺序也要可复现：在固定 `arm_order_seed` 下，逐任务轮换四组顺序；
四个任务组成一块时，每组应各占每个执行位置一次。原始 JSONL 保存
任务与组的位置以及 controls hash。汇总分别给出四组分母、完成、失败、
用量和延迟样本数；如果某任务缺少组或出现重复组，标记
`design_complete=false`，命令行非零退出。残缺实验仍保留原始记录，
但不得当作完整四组胜率。
即使四组数量齐全，混用 controls hash、任务 oracle、case index、固定种子或
执行位置，以及在同一文件混入无组别记录，也必须标记实验设计不完整；
汇总指标可供故障分析，但不能作为有效的四组比较。
显式记录的模型版本、模型设置哈希、能力版本、策略版本和 fixture 标识也
必须逐行一致；不能仅凭相同的 controls hash 掩盖一组实际使用了不同配置。
真实入口还必须提供精确的代码提交、源码归档摘要或镜像摘要，并把它与不含
秘密的适配器/后端配置一起写入 `environment_hash`。缺少或混用该哈希会使
设计不完整；OAuth secret、API key 和数据库口令绝不参与或写入原始记录。
每行还必须显式标记 `evidence_tier`：本地脚本、MockTransport、回环 MCP 和
受控执行器只能写 `controlled`；三个 opt-in 真实入口写 `live_provider`，且
必须同时具有非零的明确环境身份。同一原始文件混入两种层级会使设计不完整。
`controlled` 结果可以验证接线、失败计数和证据门禁，但不能升级为真实模型、
独立远端 MCP 或生产服务测量。
每次执行后重新核对共享 controls 的内容哈希；即使记录器字段相同，
执行器也不能在组与组之间悄悄修改可变的模型设置或预算。
独立 ablation 文件也按任务交替 off/on 顺序；每个任务必须各有一条
off 与 on 记录，且功能名、controls hash、oracle 与执行位置一致。
截断、重复或混合不同功能时 `design_complete=false`，汇总命令非零退出。
三任务套件适用相同的显式配置一致性检查。

## 5. 指标定义

Task success：当前任务的事先声明验收条件全部有证据；开放式报告的主观质量另评。False completion：Run声称成功但验收失败。Argument error与tool selection error分开。Recovery correctness：预设故障后结果和副作用计数符合oracle，不只是“任务最终停了”。

评估用时限另算：每次同步模型/执行轮次返回后都要重新检查 deadline。若 Run 最终有正确证据、甚至已经 `succeeded`，但返回发生在评估时限之后，该样本仍计入 `timeout` 分母，不能计为按时完成。原始行保留 `run_status`、已接受证据及失败原因；汇总用 `late_evidenced_claim_count` 单独呈现「证据足但超时」，不把它误算为内容错误的 `false_completion_count`。这只是评估计数规则，不表示同步调用在 deadline 时被强制中止；执行期超时仍需模型/工具客户端自身的约束。

Harness overhead：测量绑定、政策、排队、事务、序列化等时间，明确排除或单列模型/外部工具等待；总体任务延迟另给p50/p95。低样本p95不稳定，必须附样本数和分布。模型tokens来自provider计费/usage接口或明确标注估算；不要统计未发送给模型的HTTPheaders。金钱使用定点或decimal，并记录计价日期、来源；不凭空给成本收益。

初期可从30–50个内部任务模板开始，包含读取、计算、生成产物、审批发布、故障恢复。数字是建设目标不是已建立的数据集。扩展样本时划分开发/保留评测集，避免按失败样例逐一写特殊规则后在同集宣称泛化。

当前另有一条可显式运行的三任务内部样本入口 `eval/run_live_tasks.py`：
固定输入文件分别要求大写转换、非空行计数、整数求和；每个任务都要
通过受控 Gateway 读取文件、创建不可变 `text/plain` Artifact，
完成后由独立 oracle 核对实际 Artifact 字节、媒体类型及已提交的读/写
Action。正确文件的读取必须发生在创建所引用 Artifact 的模型决策轮次之前；
同一轮里先读后写仍不能证明写入参考了读取结果。预置文件也由可信操作者通过同一 Gateway 准入，模型看到的目录
仅有读取和创建产物两项能力。原始 JSONL 包含 suite hash、预期任务数、
Run ID、失败结果和用量；缺行/重复/混合配置时汇总命令非零退出。
汇总器也重新计算 outcome、oracle 与 accepted evidence 的完成关系；
原始行中的 `verified_completion` 不一致时直接拒绝报告。
评估路径的原始行还记录模型、能力、策略版本、`evidence_tier` 和设置/fixtures 哈希，
以及 ModelCall、Action、Artifact ID 与已提交事件起止序号；工具正文仍留在
受权访问的持久化记录，不复制可能含秘密的原始内容到 JSONL。
这仍是小型内部任务集。2026-09-23 的真实 DeepSeek 运行留下三条不可变
原始记录：一项有证据完成、两项 `model_output_invalid` 失败，失败仍在分母
中。该记录可作为 AT-068 的真实测量证据，但不是公开基准、泛化结论或
生产质量证明；模拟 Responses 测试仍只证明接线与证据门禁。

## 6. 每阶段交付报告

记录代码版本、依赖lock、环境、命令、测试数量/通过/失败/跳过、AT覆盖、已知限制、无法执行的集成、真实产物引用。不把“计划运行pytest”写成“pytest通过”。

评估原始记录至少包含run_id、case_id、model/config hash、capability revision、policy revision、event范围、工具结果、验收oracle、用量与失败原因。审计导出必须脱敏。
