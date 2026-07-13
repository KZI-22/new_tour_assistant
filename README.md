# 远行 · 多 Agent 旅游规划助手

当前里程碑是一个可运行的网页版 LLM 聊天机器人。它提供：

- 通过 YAML 配置新增、禁用或删除模型。
- OpenAI、Anthropic、Google GenAI 及 OpenAI-compatible 接口接入方式。
- FastAPI + LangChain 的 SSE 流式聊天接口。
- Python FlyAI CLI 客户端，以及航班、火车、酒店、POI 四个结构化 LangChain Tool。
- Next.js + TypeScript + Tailwind CSS 的响应式聊天界面。
- PostgreSQL 会话与消息持久化，支持刷新后加载和继续历史对话。
- 模型选择、Markdown 回复、停止生成、删除会话和错误提示。

航班、酒店、天气、地图和多 Agent 编排等能力会在后续里程碑接入。

## 项目结构

```text
apps/
├── api/                    # FastAPI 后端
│   └── app/
│       ├── clients/        # FlyAI 等外部客户端
│       ├── schemas/        # API 与 Tool 的结构化输入输出
│       └── tools/          # 可供后续 LangGraph 绑定的旅游工具
└── web/                    # Next.js 前端
config/
└── models.yaml             # 模型注册表（不存密钥）
compose.yaml                # PostgreSQL 本地开发服务
alembic.ini                 # 数据库迁移配置
```

## 1. 配置后端

激活现有 Conda 环境：

```powershell
conda activate py312
```

如果环境中缺少项目依赖：

```powershell
python -m pip install -e ".[dev]"
```

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

在 `.env` 中填入至少一个已启用模型所需的 API Key。例如默认 MiMo 模型需要：

```dotenv
MIMO_API_KEY=your-mimo-api-key
```

阿里百炼模型共用 `DASHSCOPE_API_KEY`。`AMAP_API_KEY` 预留给后续高德地图工具。

FlyAI 认证继续使用 FlyAI CLI 自己已经保存的配置，项目不会读取或记录密钥。默认会在
应用启动时从 `PATH` 自动查找 Windows 的 `flyai.cmd`（其次为 `flyai`），也可以配置：

```dotenv
FLYAI_CLI_PATH=C:\path\to\flyai.cmd
FLYAI_TIMEOUT_SECONDS=60
FLYAI_MAX_CONCURRENCY=3
```

`.env` 已被 Git 忽略，不要把真实密钥写入 `config/models.yaml` 或提交到仓库。

## 2. 添加或删除模型

编辑 `config/models.yaml`。新增一个阿里百炼模型示例：

```yaml
models:
  - id: qwen-example
    display_name: Qwen Example
    description: My configured chat model
    provider: openai
    model: qwen3.7-plus
    api_key_env: DASHSCOPE_API_KEY
    base_url_env: DASHSCOPE_BASE_URL
    enabled: true
```

规则：

- `id` 是前后端使用的稳定标识，必须唯一。
- `provider` 使用 LangChain `init_chat_model` 支持的供应商名。
- `api_key_env` 填环境变量名称，不填密钥本身。
- 将 `enabled` 改为 `false` 可暂时隐藏模型；删除整个条目即可删除模型。
- `base_url` 或 `base_url_env` 可用于兼容 OpenAI Chat Completions 的自定义服务。
- `temperature`、`max_tokens` 等生成参数默认不传，由模型供应商使用官方默认值；只有明确需要覆盖时才配置。
- 后端会自动检测文件修改并重载模型列表，不需要重启。

更多供应商可以在安装对应 LangChain integration package 后用相同方式加入。供应商特有参数放在 `parameters` 中，例如：

```yaml
parameters:
  reasoning_effort: low
```

## 3. 启动 PostgreSQL

Docker Desktop 启动后，在仓库根目录运行：

```powershell
docker compose up -d postgres
docker compose ps
```

首次启动或数据库结构发生变化后执行迁移：

```powershell
conda activate py312
alembic upgrade head
```

数据库使用 Docker named volume `postgres_data` 持久保存。普通的 `docker compose down`
不会删除数据；除非确定要清空所有本地会话，否则不要使用 `docker compose down -v`。

## 4. 启动后端

在仓库根目录运行：

```powershell
conda activate py312
uvicorn app.main:app --app-dir apps/api --reload --port 8000
```

可用地址：

- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/v1/health
- 模型列表：http://localhost:8000/api/v1/models
- 会话列表：http://localhost:8000/api/v1/conversations

## 5. 启动前端

项目固定使用 npm。建议使用 Node.js 22 LTS（至少 22.13）或 Node.js 24，避免使用非 LTS 的奇数版本。首次运行：

```powershell
Set-Location apps/web
npm install
Copy-Item .env.local.example .env.local
npm run dev
```

访问 http://localhost:3000 。前端默认连接 `http://localhost:8000`，也可以在 `.env.local` 中修改 `NEXT_PUBLIC_API_BASE_URL`。

## 6. 运行检查

后端：

```powershell
conda activate py312
python -m pytest
ruff check .
```

FlyAI 单元测试全部使用 mock，不消耗请求额度。若要手工执行一次真实航班集成测试，先确认
`flyai --help` 可用，再在 PowerShell 中设置测试条件：

```powershell
$env:RUN_FLYAI_TESTS="1"
$env:FLYAI_TEST_ORIGIN="上海"
$env:FLYAI_TEST_DESTINATION="北京"
$env:FLYAI_TEST_DEPARTURE_DATE=(Get-Date).AddDays(14).ToString("yyyy-MM-dd")
python -m pytest -m flyai
```

出发日期必须是执行测试当天或之后。真实测试默认跳过；它会使用 FlyAI CLI 已配置的认证，
最长等待 90 秒，并消耗一次 FlyAI 查询额度。

前端：

```powershell
Set-Location apps/web
npm run typecheck
npm run lint
npm run build
```

## 终端代理

如果终端配置了 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`，可以通过 `.env` 中的
`TOUR_ASSISTANT_NO_PROXY_HOSTS` 指定必须直连的供应商域名。项目会在启动时把这些域名
合并到已有的 `NO_PROXY` 和 `no_proxy`，不会覆盖终端原有的绕过规则。

```dotenv
TOUR_ASSISTANT_NO_PROXY_HOSTS=dashscope.aliyuncs.com
```

## 当前数据边界

会话和消息由 PostgreSQL 保存，前端刷新后可以加载并继续历史对话。助手消息会记录
`streaming`、`completed`、`failed` 或 `interrupted` 状态。当前尚未实现用户账号和数据隔离，
因此本地实例中的所有会话对访问该后端的客户端可见；在对外部署前必须加入身份认证与用户归属。
