# 作品集录屏演示

这个演示证明一条真实的本地链路：HTTP 资源写入 → 持久化 Run 准入 → 独立
worker 调用 DeepSeek → 模型提出不可信操作 → ActionGateway 准入并执行 →
CompletionGate 验证 → 读取不可变 Artifact。它不是生产部署，也不关闭 AT-065。

## 前置条件

- 本机安装 Python 3.12、uv 和 Docker，Docker Engine 已启动；
- 根目录被 `.gitignore` 排除的 `.env` 中已有有效的
  `HNH_DEEPSEEK_API_KEY`；
- 模型保持 `deepseek-flash`、reasoning effort 保持 `none`；
- TypeSafe 在这个演示中强制关闭，避免把独立实验变量混入主链路。

不用购买云服务器或域名。脚本调用真实 DeepSeek，会消耗少量 API 配额。

## 一条命令运行

```bash
uv sync --locked --all-extras --dev
set -a
source .env
set +a
uv run python scripts/run_portfolio_demo.py
```

脚本执行以下步骤：

1. 为本次运行生成随机数据库密码和开发 bearer token；
2. 以独立 Compose project 启动临时 PostgreSQL、迁移、API 和 worker；
3. 调用 `/readyz`；
4. 通过 `PUT /v1/workspaces/.../files/input.txt` 写入 `paper boat\n`；
5. 通过 `POST /v1/runs` 获得 `202 Accepted` 和 Run 地址；
6. 查询持久化状态，直到成功、失败、阻塞或等待输入；
7. 读取 Event 历史与 Artifact 内容；
8. 独立检查 Artifact 是否恰好等于 `PAPER BOAT\n`，并检查至少两个已提交
   Action ID；
9. 输出不含凭据的 JSON 摘要；
10. 删除只属于本次演示的容器、网络和临时数据卷。

只有摘要里的 `verified` 为 `true` 才是一次成功演示。Run 自述成功但 Artifact
字节或 Action 证据不匹配时，脚本退出非零，不会把模型回答当成验证结果。

## 推荐录屏顺序

1. 用 15 秒展示 README 的架构与 67/68 验收快照；
2. 展示 `.env` 只有变量名，务必遮住所有值；
3. 运行上述命令，解释 `202` 只代表持久化准入；
4. 指出状态从 `queued`/`running` 进入终态；
5. 展示最终 JSON 中的 `run_id`、`artifact_id`、`action_ids`、
   `artifact_sha256` 和 `verified`；
6. 最后说明 AT-065 仍因独立 HTTPS/OAuth MCP 环境阻塞，项目不宣称生产就绪。

建议录制 3–5 分钟，不展示 API key、数据库 DSN、完整环境变量或 Docker inspect
输出。

## 对已有服务运行

如果已经按本地运维手册启动 API 和 worker，可以只运行客户端：

```bash
export HNH_DEV_TOKEN='<与服务一致的本地 token>'
export HNH_DEMO_BASE_URL='http://127.0.0.1:8080'
uv run hnh-demo
```

客户端只调用公开 HTTP API，不导入内核或直接写数据库。

## 失败如何解释

- `configuration missing`：没有把 `.env` 加载到当前 shell；
- Docker/Compose 失败：先确认 Docker Engine 正在运行；
- `run_status=failed`：保留输出，这是模型或格式失败的真实结果；
- `run_status=blocked`：查看事件历史，不要盲目重发可能有副作用的操作；
- `verified=false`：Run 或 Artifact 未满足独立验证条件，不应剪辑成成功演示。

重复演示会使用新的 workspace、幂等键和临时数据库，不覆盖前一次证据。
