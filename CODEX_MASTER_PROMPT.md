请在当前新仓库中实现一个独立的 HTTP-native Agent Harness。它不是 MiniCodex 的重构，不允许把项目退化成 FastAPI 包装的聊天接口，也不要把所有内部方法做成微服务。

这个仓库最初只有设计包。请阅读 AGENTS.md、PLANS.md、README.md、docs/、contracts/，然后按 prompts/taskboard.json 和 P00→P08 依次实际实施。我要的是运行代码、迁移、测试、可用接口与真实验收报告，而不是再解释一次架构。

## 总体目标
模型看到可发现、可校验的 HTTP semantic operations；资源使用 /v1 地址；HTTP 网关与进程内调用复用同一个准入入口。内核管理任务状态、动作、权限、预算、审批、调度、恢复、上下文和完成验证。Postgres是唯一权威运行状态库。MCP只在边缘适配，TypeSafe只提供可选决策提示。

## 首先完成
检查真实仓库内容、Python/uv、Docker或可用隔离broker、Postgres、网络和凭据是否存在。不要输出秘密。核实并锁定实际依赖版本，尤其是官方MCP SDK；不要依据协议日期编造包版本或导入API。先建立自包含的实现计划，随后立即执行P00/P01可做的部分，而不是停在计划。

## 实施方式
1. 依照每个阶段提示，做最小纵向闭环，然后测试与修复。保留既有回归。将阶段状态、真实命令、输出摘要、未解决问题写入PLANS和reports/implementation。
2. 以真实Postgres验证幂等、事务、并发和恢复。Fake model用于确定性故障测试；真实模型和真实MCP/sandbox测试单列，不混淆证据。
3. 内核状态变化只能走RunController；副作用只能走ActionGateway；worker执行已准入动作不能重新进入本地准入API创建套娃Action。完整模型响应先持久化，再绑定动作slot。
4. 202在提交后返回；网络断开不取消Run；查询失败Run可以HTTP200；工具失败、请求失败和任务失败分别建模。不要只用HTTP status决定重试。
5. 保存logical Action、attempt、下游handle、请求hash与effect类型。未知unsafe结果必须blocked/outcome_unknown并对账。不能靠重播HTTP日志恢复外部操作，不声称通用exactly-once。
6. 审批绑定精确请求/资源/版本/主体/期限，一次性消费，dispatch前重审。模型风险/置信度不能授权；模型不得伪造身份/秘密headers或审批自己。
7. 执行代码必须通过隔离broker。缺broker时拒绝，禁止host exec fallback、任意容器flags/挂载/socket、不可信pickle和无控制的出站访问。文件、工具结果、skill都属于不可信内容。
8. MCP的2026-07-28与legacy2025-11-25采用独立profile。真实验证discover/list/call、header镜像、MRTR/Tasks/取消/OAuth，再逐项宣告支持。核心缺MCP仍能运行。
9. TypeSafe、skills、子任务后加且可关闭。子任务复用Run/Action、继承交集权限和父预算。不要另造一套状态机。
10. 以eval/acceptance_cases.yaml为验收清单，逐项实现真实测试。在reports/acceptance_status.json映射AT编号、测试位置、命令、结果与环境限制。失败/跳过不能写成通过；静态schema测试不算Harness通过。

## 交付
实际源码、pyproject与uv.lock、迁移、CLI/API、部署配置、操作手册、测试、故障注入、真实演示与评估脚本。README区分已实现/未实现/实验能力；配置完整时能按步骤运行，配置缺失时准确失败。

## 执行边界
在当前会话与环境允许的范围内连续推进已就绪阶段；不要承诺后台继续工作。若上下文或环境阻塞，先保存准确可恢复的计划、代码状态、证据和下一条命令，不虚报整个项目完成。不为了通过测试减少安全限制、删除失败用例、默认批准操作或用mock替代生产实现。

开始执行：读取仓库，更新PLANS，运行静态校验，落实P00，然后继续第一个可用纵向闭环。
