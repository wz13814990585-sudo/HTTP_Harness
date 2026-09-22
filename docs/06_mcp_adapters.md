# MCP 与其他 Adapter 的实现方案

本项目核心不是 MCP；没有 MCP 依赖时仍应跑通全部内核测试。这里的兼容层用于复用外部能力，不将自定义 `/v1` 服务冒充 MCP。

## 1. MCP 基线与版本

主 profile 选 MCP 2026-07-28；可选 legacy profile 选 2025-11-25。二者生命周期不同，不能把 initialize、Mcp-Session-Id、新 per-request `_meta` 和新订阅模式混在一个模糊模式中。来源：R02–R04、R09。

使用官方 SDK 并在 P00/P06 记录实际安装版本、导入 API、最小互操作测试；包版本不能根据协议日期推断。SDK 当前 API 与官方示例不同则先验证，再修改 adapter，不在核心抛出一堆 SDK 类型。

## 2. Capability 导入

`server/discover` / legacy initialize 只在正确 profile 使用。先用没有副作用的 discovery 探测，不用“创建记录”这样的写操作测试新旧协议；否则失败后回退可能重复执行。

将工具命名空间映射为 `(integration_id, tool_name, schema_revision)`；服务端 name 不足以保证全局唯一。保存 inputSchema、outputSchema、描述和原始 annotations；annotations 仅作为提示，副作用与权限由受信任 binding 覆盖。

默认导入为 POST `/v1/integrations/{integration_id}/operations/{operation_name}/invocations`。经过审查的只读资源才可另外映射成 GET；不要自动把所有名为 search/read 的工具当作安全方法。

## 3. Header 镜像

现代 MCP 中协议 metadata 仍以 body 为源；SDK/adapter 按规范生成必要 header 并确保一致。`Mcp-Name` 只在适用调用中必需，不为所有请求胡乱设置。x-mcp-header 需要检查字段类型、编码和敏感信息限制（R03、R05）。

本项目不让模型手工生成 Mcp-*、OAuth 或 transport headers；原始请求 envelope 可脱敏记录，不进入模型 token 成本统计，除非它真的进入了模型输入。

## 4. 四种结果必须分别处理

- JSON-RPC error：协议/服务器错误，保留原 code 与 data。
- complete + isError：工具执行错误；可能发生在 HTTP 200 内。
- input_required：生成 durable InputRequest，暂停，不是失败。
- task：保存 taskId、TTL、轮询时间、integration/profile，并由 scheduler 跟踪。

structuredContent 保留任意合法 JSON 值，不擅自限制为对象。text/image/audio/resource_link 转换为对应内容或 Artifact；schema 校验失败要明确报告而非吞掉。远端 resource URI 不能自动当作可信本地路径。

## 5. MRTR 的恢复

保存同一逻辑 Action 的业务参数、inputRequests、opaque requestState、服务器标识和 profile；requestState 按敏感状态保护，不交给模型解释或修改。受信任用户补充后保存 inputResponses，再由适配器按协议 continuation 调用。JSON-RPC request ID 更新，逻辑 Action ID 保持。来源：R06。

审批本身不由模型完成。没有识别或实现的请求类型返回明确 unsupported，不能默默忽略后继续。输入内容变化只能发生在协议指定的输入槽中；业务目标/工具参数改动需新的 Action 和必要审批。

## 6. Tasks 与断线

使用 Tasks 扩展前确认 capability。保存 durable handle 后断线只恢复轮询，不能重新调用原工具创建新任务。input_required 由 tasks/update 提交；terminal result 原样映射；取消只是协作请求，等服务器实际结果。来源：R07。

新 Streamable HTTP 不提供旧 SSE 重投递保证。没有 Tasks handle 的调用断线，必须由副作用分类判断是否可安全 reissue；协议允许新 request ID 并不意味着业务上可盲目重试。己方 `/v1/runs/{id}/events` 的数据库重放完全独立于 MCP 传输机制。来源：R09。

## 7. 缓存和 OAuth

缓存工具目录时应用 ttlMs、cacheScope 和确定顺序，但仍按本地授权范围 partition，并受本地 policy revision 失效约束。不能把上游声称 public 的含租户数据直接放进共享缓存。来源：R08。

外部 OAuth 凭据按 issuer/resource/subject 绑定存储并校验，不能透传 Harness API token。不要自造 OAuth 流程；使用受维护实现并做 token audience、issuer mismatch 和撤销测试。来源：R10。

## 8. 兼容性承诺分级

C0：无 MCP，核心独立运行。

C1：一个真实现代 server 的 discover/list/call、结果错误及 header 契约通过测试。

C2：MRTR / Tasks / legacy profile 分别测试；只在验证后 advertise。

C3：可选入站 MCP facade，把 Harness 的已批准能力导出，复用同一准入和安全规则；不是 v0.1 必须功能。

报告必须说明测试过哪些 profile、transport、扩展和 server。不能只跑一个 fake tools/call 就写“全面兼容 MCP”。

## 9. pythond / HTTP / Native

PythondAdapter 持久化 session worker ID/generation、cell ID 和结果获取信息；不能只存 session 名称，因为重建后同名代表不同进程。超过上游结果保留期应标记 unavailable 并对账，不把空结果当成功（R19）。

HTTPAdapter 只调用受审阅的 endpoint binding，限制 body/response 大小、重定向、content type 和超时。NativeAdapter 不享受安全豁免，使用相同 Action 记录和权限检查；执行危险代码不能在 API 进程中 `exec`。
