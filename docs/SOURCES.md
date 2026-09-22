# 研究来源与证据边界

访问日期：2026-09-22。以下为规范、官方文档、公开仓库或维护者发布页。

这些来源支持本包的外部事实；具体架构、默认值、状态机、验收清单与阶段拆分属于本方案的工程设计，不是这些来源承诺的能力。

## R01 · MCP 2026-07-28 正式发布

正式发布说明：无状态核心、HTTP 路由、MRTR、缓存与 Tasks 扩展。

```text
https://blog.modelcontextprotocol.io/posts/2026-07-28/
```

## R02 · MCP 2026-07-28 基础规范

协议仍使用 JSON-RPC；不能把 HTTP-native 自定义接口称为原生 MCP。

```text
https://modelcontextprotocol.io/specification/2026-07-28
```

## R03 · MCP Streamable HTTP

header 来源、适用范围、编码、校验与安全要求。

```text
https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
```

## R04 · MCP 版本兼容

modern / legacy / dual-era 与兼容探测。

```text
https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
```

## R05 · MCP Tools

输入输出结构、工具结果、x-mcp-header 与不可信 annotations。

```text
https://modelcontextprotocol.io/specification/2026-07-28/server/tools
```

## R06 · MCP MRTR

input_required、inputResponses、requestState 的多轮请求模型。

```text
https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr
```

## R07 · MCP Tasks

持久化任务句柄、轮询、补充输入和协作取消。

```text
https://modelcontextprotocol.io/extensions/tasks/overview
```

## R08 · MCP Caching

ttlMs、cacheScope 与客户端缓存约束。

```text
https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching
```

## R09 · MCP Changelog

明确移除旧传输层会话以及 SSE 事件重放；列出新旧差异。

```text
https://modelcontextprotocol.io/specification/2026-07-28/changelog
```

## R10 · MCP Authorization

OAuth 资源和发行者绑定、客户端身份与授权要求。

```text
https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
```

## R11 · OpenAI Agents SDK Runner

小型模型—工具循环、会话与耐久执行集成是不同职责。

```text
https://openai.github.io/openai-agents-python/running_agents/
```

## R12 · OpenAI Agents SDK MCP

SDK 为 Agent 接入 MCP 工具及结果；线协议与模型工具界面不是同一层。

```text
https://openai.github.io/openai-agents-python/mcp/
```

## R13 · LangGraph Checkpointers

检查点、线程状态与执行历史。

```text
https://docs.langchain.com/oss/python/langgraph/checkpointers
```

## R14 · LangGraph Interrupts

中断恢复时的重执行与副作用约束。

```text
https://docs.langchain.com/oss/python/langgraph/interrupts
```

## R15 · LangChain Deep Agents

上下文、文件系统、skills、执行环境等 Harness 机制。

```text
https://docs.langchain.com/oss/python/deepagents/overview
```

## R16 · ByteDance DeerFlow

公开项目以 long-horizon harness 定位，包含 sandbox、skills、memory、subagents。

```text
https://github.com/bytedance/deer-flow
```

## R17 · Google ADK Event Loop

Runner 接收事件并提交状态变化。

```text
https://adk.dev/runtime/event-loop/
```

## R18 · Microsoft Agent Pipeline

区分 Agent、模型与工具调用层的扩展管线。

```text
https://learn.microsoft.com/en-us/agent-framework/concepts/agents/agent-pipeline
```

## R19 · pythond 维护者项目说明

HTTP 访问存活 Python 进程；代码按 daemon 的 OS 权限运行。

```text
https://pypi.org/project/pythond/
```

## R20 · curldb 维护者项目说明

记录 HTTP 交换；replay 是再次发送请求，可能再次产生副作用。

```text
https://pypi.org/project/curldb/
```

## R21 · TypeSafe API 概览

结构化选择与评分等决策接口。

```text
https://docs.typesafe.ai/introduction
```

## R22 · TypeSafe Confidence

confidence 来源于返回的概率分布；阈值需要本地验证。

```text
https://docs.typesafe.ai/confidence
```

## R23 · HTTP Semantics — RFC 9110

方法、状态、条件请求等标准语义。

```text
https://www.rfc-editor.org/rfc/rfc9110.html
```

## R24 · Problem Details — RFC 9457

HTTP 错误表示格式。

```text
https://www.rfc-editor.org/rfc/rfc9457.html
```

## R25 · Idempotency-Key 草案状态

查询时是过期 Internet-Draft -07，不是已发布 RFC。

```text
https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/
```

## R26 · OpenAPI 3.1.1

选用的 HTTP 接口描述格式；不声称它是最新 OpenAPI 版本。

```text
https://spec.openapis.org/oas/v3.1.1.html
```

## R27 · PostgreSQL 17 SELECT

SKIP LOCKED 可用于 queue-like 表；不用于一般一致性读取。

```text
https://www.postgresql.org/docs/17/sql-select.html
```

## R28 · WHATWG Server-sent events

SSE 格式和 Last-Event-ID；持久化重放由应用实现。

```text
https://html.spec.whatwg.org/multipage/server-sent-events.html
```

## R29 · W3C Trace Context

traceparent / tracestate。

```text
https://www.w3.org/TR/trace-context/
```

OpenTelemetry Python SDK 与 OTLP/HTTP 导出器的实现参考（用于 P08 可选 trace
出口；具体包版本以 `uv.lock` 为准）：

```text
https://opentelemetry.io/docs/languages/python/exporters/
https://opentelemetry.io/docs/languages/python/propagation/
```

## R30 · OWASP SSRF Prevention

出站地址、重定向和允许列表风险。

```text
https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
```

## R31 · Docker Engine Security

隔离机制与 daemon 权限边界；容器不是无条件安全保证。

```text
https://docs.docker.com/engine/security/
```

## R32 · Codex AGENTS.md

项目指令的发现、作用域与大小限制。

```text
https://developers.openai.com/codex/guides/agents-md
```

## R33 · Codex ExecPlans

长任务使用持续更新的实施计划和验收证据。

```text
https://developers.openai.com/cookbook/articles/codex_exec_plans
```

## R34 · DeepSeek Responses API、思考模式与模型目录

用于当前 DeepSeek provider 的 `/responses`、`text.format`、function tools、
`reasoning.effort`、模型 ID 与 base URL。具体价格会变化，不写入运行契约。

```text
https://api-docs.deepseek.com/guides/responses_api/
https://api-docs.deepseek.com/api/create-response/
https://api-docs.deepseek.com/guides/thinking_mode/
https://api-docs.deepseek.com/quick_start/pricing/
```

## U01 · 用户提供的 Elastik PPT

文件：347c27d5-0064-4778-9053-315fade73b24.pptx。第 12–14 页提供资源地址、header/body 分工与原始/派生内容的设计启发；第 28 页提出用 URL 承载上下文、输出位置和通知的设想。此处不把 PPT 的营销式绝对表述作为技术事实。例如 HMAC 能支持完整性/认证校验，不能证明存储内容在现实中为真；HTTP 形式本身也不能证明模型更擅长操作。

## 本次没有做的事情

没有全面审计上游源码，没有实际部署这些上游项目，没有调用 TypeSafe 或模型付费 API，没有测量真实 Harness 性能。pythond.sh 与 curldb.ai 首页此次无法通过浏览工具直接打开，改用其维护者发布到 PyPI 的项目说明核实对应功能。没有把项目 star 数、营销性能数字或未执行的测试当成结论。
