请独立审计当前HTTP-native Harness实现，不要相信README的完成宣称。读取AGENTS、contracts、状态机、eval验收清单、实际源代码与测试输出。

逐层追踪一个任务：HTTP身份→Run准入→模型响应持久化→HttpOperation绑定→Policy/Approval→Action准备→实际effect→结果事务→SSE→CompletionGate。核查有无旁路、重复Action、权限来源不可信、错误重试、未知结果丢失、旧worker迟到写入、取消被当作停止、session恢复被夸大。

重点注入远端效果成功但响应丢失、等待审批时重启、文件条件写竞态、SSE断连、pythond同名重建和MCP结果类型差异。区分HTTP-native设计收益与模型界面/下游协议/按需目录的实验混淆。

输出按严重性排序的有证据问题：文件/行号、触发路径、真实可复现命令、影响、不变量、最小修复与回归测试。可以先修复高优先级问题并运行测试；未测试能力如实说明。不要只给“代码整洁、架构合理”的泛泛好评。
