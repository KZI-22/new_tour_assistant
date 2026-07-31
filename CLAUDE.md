# AGENTS.md

本文件是本仓库中所有开发者与 AI 编码 Agent 的项目级协作约定。除非用户在当前任务中给出更高优先级的明确要求，否则在仓库任意目录工作时都应遵守本文件；子目录可通过更具体的 `AGENTS.md` 补充或覆盖局部规则。

## 1. 项目主题

本项目从 0 到 1 构建一个**智能旅游规划助手系统**。

## 2. 暂定技术栈

### 后端

- Python 3.12
- FastAPI
- LangChain / LangGraph
- Fli 航班工具
- 高德API
- PostgreSQL
- Redis

### 前端

- Next.js
- TypeScript
- Tailwind CSS
- shadcn/ui
- CopilotKit / AG-UI
- React Flow

以上技术栈目前为项目默认选择。若要替换核心框架、数据库或交互协议，应先说明动机、迁移成本和影响范围；必要时新增 ADR（Architecture Decision Record），不要在普通功能改动中静默引入同类替代品。

## 3. 本地开发环境

项目主要使用 Conda 环境 `py312`，其中已包含基础依赖。

```powershell
conda activate py312
python --version
```

约定：

- 执行 Python、测试、格式化、迁移或后端开发命令前，默认先激活 `py312`。
- Python 版本以 3.12 为准，不主动降级兼容旧版本。
- 安装新依赖前先确认现有环境和依赖清单中是否已提供。
- 新增直接依赖时必须同步更新项目依赖文件和相关说明，禁止只修改个人 Conda 环境。
- 不提交虚拟环境、缓存、构建产物、数据库文件或本地 IDE 配置。

前端运行时与包管理器尚未最终确定。初始化前端时，应在 README 中固定 Node.js 版本和包管理器；一旦仓库出现 lockfile，后续必须沿用对应包管理器，不能混用 lockfile。

## 4. 建议的仓库结构

项目初始化时优先采用前后端分离的单仓库结构：

```text
new_tour_assistant/
├── apps/
│   ├── api/                 # FastAPI 应用
│   └── web/                 # Next.js 应用
├── packages/                # 可选：共享协议、生成客户端或 UI 包
├── tests/                   # 跨模块、集成与端到端测试
├── docs/
│   ├── adr/                 # 架构决策记录
│   └── architecture/        # 架构、状态图与数据流说明
├── scripts/                 # 可重复执行的开发与运维脚本
├── infra/                   # 容器、部署与基础设施配置
├── .env.example
├── AGENTS.md
└── README.md
```

后端内部建议按领域和职责拆分，而不是把所有代码放在 `main.py`：

```text
apps/api/app/
├── api/                     # HTTP、SSE、WebSocket/AG-UI 边界
├── graphs/                  # LangGraph 编排、状态与路由
├── tools/                   # 外部工具接口与供应商适配器
├── domain/                  # 领域模型与纯业务规则
├── services/                # 应用服务与用例编排
├── repositories/            # PostgreSQL 数据访问
├── schemas/                 # API 输入输出模型
├── core/                    # 配置、日志、安全与通用基础设施
└── main.py                  # 应用装配入口，保持轻量
```

目录结构可以随实现调整，但必须保持领域逻辑、行程编排、外部工具、持久化和传输层之间的边界。

## 5. Git 提交规则

- 每完成一个独立的代码修复并完成必要验证后，必须在同一任务中立即创建一次 Git 提交，不能把多个无关修复积攒到同一个提交中。
- 提交前必须检查 `git status` 和相关 diff，只暂存并提交本次修复涉及的文件；不得夹带用户已有改动、其他未完成工作、缓存或构建产物。
- 提交信息必须简洁说明修复对象和结果，使该提交能够独立审查和安全回滚。
- 如果测试未通过、修复尚未完成，或因权限、冲突等原因无法安全提交，不得把任务描述为已完成；应明确报告阻塞原因，处理完成后再提交。

## 6.开发前流程
- 当我每次要开发新的功能或者改变现有功能的时候，我们需要先进行讨论，分析改动是否合理，知道达成共识。

## 7.小红书MCP

- 当前项目所用的小红书MCP的项目路径在 "D:\A_Project\xhs_mcp\xhs-read-mcp" ，实现中有需要确认的功能或者字段对齐必须去该项目的源码确认。
