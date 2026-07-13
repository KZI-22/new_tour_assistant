# 远行 · 多 Agent 旅游规划助手

当前里程碑是一个可运行的网页版 LLM 聊天机器人。它提供：

- 通过 YAML 配置新增、禁用或删除模型。
- OpenAI、Anthropic、Google GenAI 及 OpenAI-compatible 接口接入方式。
- FastAPI + LangChain 的 SSE 流式聊天接口。
- Python FlyAI CLI 客户端，以及航班、火车、酒店、POI 四个结构化 LangChain Tool。
- 高德 Web Service 异步客户端，以及 IP 城市推测、POI、路线、距离矩阵、天气五个结构化 Tool。
- 单 Agent 工具调用闭环，支持多轮决策、同轮并发、统一错误和最大轮数限制。
- SSE 工具调用状态与前端进度展示，以及脱敏的 PostgreSQL 工具调用审计日志。
- 请求级可信代理 IP、时区时钟上下文和基础旅行日期标准化能力。
- Next.js + TypeScript + Tailwind CSS 的响应式聊天界面。
- PostgreSQL 会话与消息持久化，支持刷新后加载和继续历史对话。
- 模型选择、Markdown 回复、停止生成、删除会话和错误提示。

当前仍未接入 LangGraph、多 Agent 编排和自动行程生成。模型可以根据上下文串联或并发调用
FlyAI 与高德工具，但尚未形成完整的多 Agent 行程规划图。

## 项目结构

```text
apps/
├── api/                    # FastAPI 后端
│   └── app/
│       ├── clients/        # FlyAI、高德等外部客户端
│       ├── schemas/        # API 与 Tool 的结构化输入输出
│       ├── services/       # 聊天、工具执行、审计日志等应用服务
│       └── tools/          # 已绑定到聊天模型的旅游工具
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

阿里百炼模型共用 `DASHSCOPE_API_KEY`。

高德工具使用 Web Service 类型的 Key。未配置 `AMAP_API_KEY` 时，应用仍可启动，但只注册
原有四个 FlyAI Tool；配置后会额外注册五个高德 Tool：

```dotenv
AMAP_API_KEY=your-web-service-key
AMAP_BASE_URL=https://restapi.amap.com
AMAP_TIMEOUT_SECONDS=15
AMAP_MAX_RETRIES=1
```

高德返回的坐标统一标记为 GCJ-02。外部 WGS84、BD-09 或 Mapbar 坐标可通过客户端内部
转换接口显式转换；项目不会猜测 FlyAI 数据的坐标系。Key 不会写入工具结果、异常或应用日志。

请求时间和代理配置如下：

```dotenv
APP_TIMEZONE=Asia/Shanghai
TRUSTED_PROXY_CIDRS=
```

默认不信任任何 `X-Forwarded-For` 或 `X-Real-IP`。只有直接连接来自
`TRUSTED_PROXY_CIDRS` 中的 Nginx/CDN/反向代理时，后端才会沿可信代理链提取客户端 IP。
IP 只保存在请求级上下文中，不作为 Tool 参数暴露给模型；回环、局域网、IPv6 或其他不可定位
地址会得到明确的“不可定位”结果。每次 HTTP 请求还会动态生成带时区的当前日期、时间和星期上下文。

FlyAI 认证继续使用 FlyAI CLI 自己已经保存的配置，项目不会读取或记录密钥。默认会在
应用启动时从 `PATH` 自动查找 Windows 的 `flyai.cmd`（其次为 `flyai`），也可以配置：

```dotenv
FLYAI_CLI_PATH=C:\path\to\flyai.cmd
FLYAI_TIMEOUT_SECONDS=60
FLYAI_MAX_CONCURRENCY=3
MAX_TOOL_ROUNDS=5
TOOL_EXECUTION_TIMEOUT_SECONDS=130
```

`MAX_TOOL_ROUNDS` 限制一次助手回复最多执行多少轮工具，避免模型重复调用失控。
`TOOL_EXECUTION_TIMEOUT_SECONDS` 是单个工具调用的外层安全超时；它应略大于供应商客户端自身的
超时与重试总时长。

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

高德单元测试同样全部使用 mock。若要执行真实高德冒烟测试（POI、天气、驾车路线和小规模
距离矩阵），先填入 `AMAP_API_KEY`，再显式启用：

```powershell
$env:RUN_AMAP_TESTS="1"
python -m pytest -m amap
```

该测试会访问真实高德接口并消耗调用配额；默认测试流程会跳过它。内部
`normalize_travel_dates` / `validate_travel_dates` 当前支持今天、明天、后天、明确中英文日期格式、
“住 N 晚”和基础日期关系校验，不处理节假日、调休或复杂多段行程语义。

若 FlyAI、高德、PostgreSQL 和至少一个工具调用模型均已配置，可以显式运行完整聊天闭环场景：

```powershell
$env:RUN_TOOL_CALL_E2E="1"
$env:TOOL_CALL_E2E_MODEL="qwen3.7-plus"
python -m pytest -m e2e
```

该组测试会覆盖航班、火车、酒店、天气、路线、交通对比、缺少日期追问和普通聊天，
会消耗模型与供应商调用额度，因此默认跳过。

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

会话、消息和脱敏后的工具调用摘要由 PostgreSQL 保存，前端刷新后可以加载并继续历史对话。
工具原始结果和 API Key 不入库；前端工具进度当前不随历史会话恢复。助手消息会记录
`streaming`、`completed`、`failed` 或 `interrupted` 状态。当前尚未实现用户账号和数据隔离，
因此本地实例中的所有会话对访问该后端的客户端可见；在对外部署前必须加入身份认证与用户归属。
