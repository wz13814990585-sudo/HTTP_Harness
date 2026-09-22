# 安全、权限与隔离

这是设计要求，不是经过审计的安全证明。HTTP/协议安全参考 R03、R05、R10、R30、R31。

## 1. 信任边界

可信控制面：认证服务、管理员配置的 capability binding、内核、数据库、经过审阅的执行 broker。非可信输入：用户文档、网页、MCP 描述/annotations、工具返回、模型生成的操作/headers、上传脚本。

模型不拥有审批端点、系统事件写入、运行终态修改、密钥读取或 capability 配置权限。客户端传来的 `Agent-Actor` / tenant / risk 等字段一律不能用于确认主体身份；主体来自受信任认证上下文。

本地模式也使用随机开发 token、最小 scope 和资源归属检查，监听 loopback。多用户模式加入真实 OIDC/JWT 的 issuer、audience、expiry 校验以及适当的跨租户隔离测试；未完成前不得开放公网多租户服务。

## 2. 审批必须绑定具体动作

InputRequest(kind=approval) 保存 action_id、请求摘要、正文/产物内容 hash、capability 版本、policy revision、主体、资源范围、费用上限、过期时间。UI 展示即将执行的目标和正文/产物版本，不能仅显示“批准工具使用”。

审批响应由受信任用户通道提交，使用 If-Match 和 Idempotency-Key；在事务内检查 pending、未过期、版本匹配。一个审批只能消费一次。dispatch 前重新校验权限、policy 和请求 hash；参数改变、目标文件版本改变、范围扩大时原审批失效，重新审批。

明确区分拒绝与可审批：policy deny 不能被“请用户批准”自动升级。模型新生成一个高风险 header 也不能绕过 deny。

## 3. HTTP 出站

模型默认只能使用 `/v1/...` 相对资源地址。外部目的地由 capability binding 关联已配置服务；禁止任意目标 URL 的通用代理成为默认工具。

确有网页 fetch 工具时，需要独立出站策略：允许 scheme/domain，阻止 loopback、链路本地、云 metadata、私网和非预期端口；DNS 解析与连接路径一致检查，不能只对最初字符串做正则。重定向逐跳校验且上限；跨 origin 不转发认证；代理环境变量不能意外绕过策略。使用网络层 egress broker 实施约束，而非只在 Python 函数里 if 一次。来源：R30。

外部 URL 形式的 output destination / callback 同样需要授权。初版不支持任意 webhook URL；先用本地 InputRequest 与 SSE，后续只允许注册的 callback，签名、重试与重复投递另行定义。

## 4. 执行环境

pythond 运行在受控隔离环境内部，不能直接暴露宿主用户 daemon。使用非 root、只读根文件系统、明确写目录、CPU/RAM/PID/输出大小/时长限制、drop capabilities、no-new-privileges、默认禁网和受控镜像。容器不能被描述为对敌对多租户代码的绝对安全边界。来源：R19、R31。

不要把 Docker socket、宿主 HOME、SSH agent 或 API 凭据挂给 Agent 容器。执行 broker 属于高权限控制服务，要与模型工具边界分离。初版 broker 不允许传入任意 image、mount、privileged 或 network 参数。

支持超时、中止进程树、确认结束和环境 generation；停止请求不等于执行已停止。跨会话不可共享命名空间。禁用不可信 pickle 导入；Python 对象任意反序列化与运行代码属于相同高风险边界。

依赖安装必须使用预构建镜像或受审阅的依赖清单；不因模型报 ImportError 自动把公网任意包安装到宿主机。

## 5. 文件与产物

拒绝绝对路径、..、NUL、路径分隔符混淆、符号链接逃逸和编码后穿越。规范化与访问检查必须一致；不能字符串 startswith 判断 workspace 边界。沙箱内文件操作和产物导出分别检查权限。

不可变 Artifact 按租户/主体引用授权；同 hash 的存储去重不能泄露其他租户是否存在该对象。HTML/Markdown 渲染隔离并处理脚本、链接与嵌入内容。原始报文、正文、Trace 和模型输入都可能含秘密；默认日志脱敏且 body 截断/按引用存储。

## 6. Secrets 与鉴权适配

秘密由 server-side secret reference 解析，绑定服务/主体/audience；不加入 LLM context，也不在输入 header 或 artifact 中回显。MCP OAuth 的 token 与 issuer/resource 绑定必须交给经过验证的实现；禁止把 Harness 的 bearer token 当作上游 MCP token 透传。来源：R10。

目录与输入 schema 也可能含指令注入。把它们当能力描述数据，不提升为 system policy；描述改变需 catalog revision 更新，安全敏感 binding 仍由管理员控制。

## 7. 发布前安全门禁

必须覆盖：跨租户读写、模型自审批、审批后参数替换、撤销权限后执行、SSE/日志密钥泄漏、SSRF 与 redirect、路径穿越、沙箱网络绕过、输出洪泛、并发审批和过期租约结果提交。

这些是具体测试目标，不等于形式化证明“永不越权”。发布报告必须列出未覆盖的攻击面和部署限制。
