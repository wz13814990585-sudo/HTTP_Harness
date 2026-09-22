# 技术路线与阶段验收

这不是“做完九个框架”，而是逐步实现一个共享状态、统一操作入口的内核。阶段顺序由依赖与风险确定，不承诺一个固定天数或一次模型会话就能完成。

## 1. 阶段表

| 阶段 | 名称 | 目标 | 出口验收 |
|---|---|---|---|
| P00 | 仓库与依赖可行性 | 建立可执行开发基线，不实现一个假服务 | 文档静态校验与新增工程检查真实可运行；缺凭据/环境明确记录；锁版本可复现 |
| P01 | 持久化内核与授权基础 | 让接受请求与状态提交可靠 | 真实Postgres并发/回滚测试通过；202持久化边界和重复准入被证明 |
| P02 | HTTP资源契约与统一操作网关 | 让模型动作与直接HTTP共享规则 | 实际HTTP契约、二次参数校验、路径/权限/条件写竞争测试通过 |
| P03 | 模型循环、上下文与事件续传 | 真实运行一个非沙箱多步任务 | fake和真实provider结果分开；验收证据可解释；没有key不得声称live通过 |
| P04 | 执行隔离与持续会话 | 加入受控代码执行而不是宿主exec | 真实sandbox限制/会话/故障测试通过；缺broker失败关闭，不退化成宿主执行 |
| P05 | 审批、未知结果与恢复加固 | 先做可靠单Agent候选，再扩展 | 关键故障与安全AT全部实测；v0.1候选只在满足本阶段门槛时建立 |
| P06 | MCP兼容适配器 | 吸收新规范但不让MCP污染内核 | C1真实现代互操作通过；C2扩展/legacy逐项列证据，不以fake宣称完整兼容 |
| P07 | 按需智能与有界子任务 | 把分类器/skills/多Agent做成可关闭插件 | 扩展不绕过执行/恢复/权限；收益以独立ablation评价 |
| P08 | 评估、部署与发布候选 | 把宣称替换为可重复的测试证据 | 全部必需AT真实运行并报告；没过live/broker/compatibility门槛只能保留候选/实验标签 |

## 2. 版本界线

P00–P05构成独立HTTP-native v0.1候选。此时不需要MCP/TypeSafe/多Agent就能证明主要架构。P06–P07为v0.2，P08完成后才讨论v1.0候选。MCP属于用户需要的完整技术路线，但不是拖延最小内核验证的前置条件。

权限、路径校验、secret边界必须从P01/P02开始；P05是加固与故障验证，不是前四阶段允许裸奔。每阶段新增测试继续跑已有回归。未实现功能在supported capabilities中不出现，不能返回假成功。

## 3. 切片式开发

每阶段先实现一条vertical slice：入口→绑定→持久化→执行/结果→查询→测试，再推广到更多能力。不要一口气生成数百个空类、TODO和抽象工厂。暂时不需要的模块可以不创建；公开未支持操作应明确拒绝。

每个阶段文档是一个Codex工作包：读取上下文、检查当前代码、实现、运行、修复、记录证据。在上下文或环境受限时留下准确checkpoint，以同一PLANS继续。不要把失败测试删掉、降低安全限制或编造报告来完成阶段。

## 4. 暂缓项与引入条件

通用图执行：简单策略无法描述实际需要的依赖且有工作负载证据时再加。

Redis/Kafka/工作流引擎：Postgres队列实际测得瓶颈或长流程需求超出当前内核时再评估；只指定一个状态/重试权威，不并行运行两套调度器争夺Action。

生产多租户：先完成真实跨主体隔离、凭据隔离、网络/资源隔离、quota和运维审计后单独审查。容器demo不自动获得该标签。

浏览器自动化/包安装/任意出站：在单独的能力契约、授权与沙箱策略成熟后加入，默认关闭。

## 5. 代码组织建议

```text
src/hnh/
  domain/          # 状态、身份、事件、BoundOperation；不导入FastAPI/SDK
  application/     # RunController、Runner、调度、Policy、Context、Completion
  ports/           # ModelProvider、ToolAdapter、RunStore、Broker、BlobStore
  adapters/
    postgres/      # repo/UoW/job领取
    models/        # 真模型与测试provider清晰区分
    tools/         # native/http/pythond/mcp，插件可选
    sandbox/       # 可信执行broker
    blobs/
  transport/http/  # ASGI路由、schema编解码、SSE
  worker/          # entrypoint/leases/heartbeats
  cli/             # run/watch/cancel/approve/inspect
  observability/
tests/
  unit/ contract/ integration/ security/ chaos/ e2e/
  support/         # controlled effect_server，不用于生产
migrations/
```

`domain`不允许反向依赖adapters；transport不能直接绕过application写DB。不要把所有剩余函数塞进utils；只有确实跨模块的纯函数放小型明确命名模块。

## 6. 发布需要的实证

真实模型演示、真实Postgres恢复、真实隔离执行、HTTP契约、关键安全门槛、必要MCP互操作、完整失败记录、可复现运行命令。星标数、架构图和完整README不是这些证据的替代品。
