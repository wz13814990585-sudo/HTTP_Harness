# 销售报告、审批与安全恢复演示

这是一条可交互、可录屏的业务链路：生成销售 CSV → 通过受控资源读取 → 在隔离
Python 容器中计算 → 导出不可变 Markdown Artifact → 等待操作者批准 → 调用独立
本地发布服务 → 在发布已经提交后故意断开响应 → 保留 `outcome_unknown` → 查询发布
账本并对账 → 重放同一 Action，验证没有第二次发布 → CompletionGate 验收。

演示不需要 DeepSeek 或 TypeSafe API Key。它专门展示 Harness 的执行、审批和恢复
语义，而不是模型质量。发布服务是本机独立 HTTP 线程和内存账本，不是生产远端服务；
因此它是可重复故障演练，不是生产部署证明，也不关闭 AT-065。

## 前置条件

- Python 3.12 与 `uv`；
- 已启动 Docker Engine；
- 本机能够拉取仓库固定 digest 的 PostgreSQL 和 Python 镜像。脚本在镜像缺失时会
  自动拉取；也可以按下方命令预先拉取 Python 沙箱镜像。

```bash
docker pull python@sha256:1dd3dca85e22886e44fcad1bb7ccab6691dfa83db52214cf9e20696e095f3e36
```

## 交互运行

```bash
cd "/Users/april/Desktop/AI-agent/HTTP_Harness"
uv sync --locked --all-extras --dev
uv run python scripts/run_sales_demo.py
```

脚本显示 Artifact ID、SHA-256、目标和副作用类型后暂停。只有准确输入：

```text
approve
```

才会发送发布请求。其他输入和 EOF 都会拒绝，Run 进入失败终态，并且不会产生发布。

用于自动化验证或录屏时可以显式使用：

```bash
uv run python scripts/run_sales_demo.py --auto-approve
```

## 成功标准

最终 JSON 必须同时包含：

```json
{
  "verified": true,
  "run_status": "succeeded",
  "total_revenue": "6010.00",
  "status_after_response_loss": "blocked",
  "action_status_after_response_loss": "outcome_unknown",
  "reconciliation": "confirmed_applied",
  "publish_requests": 1,
  "publication_count": 1,
  "replay_without_redispatch": true
}
```

`publish_requests=1` 和 `publication_count=1` 是关键：演示中的外部服务不提供幂等
保护，如果 Harness 在响应丢失后盲目重发，这两个数字会变成 2。当前流程只查询
账本并提交对账证据；对同一操作的再次调用读取已提交 Action，不再触发 HTTP POST。

## 数据与产物

可读样例位于 [`examples/sales_demo/sales.csv`](../examples/sales_demo/sales.csv)，
独立预期产物位于
[`examples/sales_demo/expected_report.md`](../examples/sales_demo/expected_report.md)。
运行时 CSV 通过版本化 workspace 资源写入；报告由禁网、非 root、只读根文件系统、
固定镜像 digest 的 Python 沙箱生成，并由 Harness 导入 Blob/Artifact 存储。

## 清理和安全边界

脚本只删除它自己随机命名的临时 PostgreSQL 容器；Python session 由 ExecutionService
停止；Blob 临时目录自动清理。不要把这个本地发布模拟器理解成生产认证、OAuth、
公网出站策略或多租户安全证明。
