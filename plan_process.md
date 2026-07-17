# 工具调用闭环实施记录

> 实施日期：2026-07-14
> 对应计划：`docs/plans/tool_call.md`

## 实施范围

本次改动依据工具调用计划，并结合仓库现有 FastAPI、LangChain、SSE、数据库和前端结构，实现了单 Agent 的完整工具调用闭环。当前阶段未引入 LangGraph 或多 Agent 编排，优先保证最小闭环可运行、可观测、可审计和可恢复。

完整链路如下：

```text
用户消息
  -> 模型判断是否调用工具
  -> 后端校验并并发执行同一轮工具
  -> 工具结果回填模型上下文
  -> 模型继续调用工具或生成最终回答
  -> SSE 推送调用状态、结果和回答增量
  -> 数据库记录工具调用审计信息
```

## 主要改动

### 1. Agent 执行循环

- 新增 `AgentExecutor`，基于 LangChain 标准 `AIMessage.tool_calls` 实现多轮 `ainvoke()` 调用循环。
- 支持设置最大工具调用轮数，避免模型无限调用。
- 同一轮多个工具通过 `asyncio.gather()` 并发执行，并按模型原始调用顺序回填结果。
- 模型返回空内容时允许一次受控重试，仍为空则返回明确错误。
- 请求取消会继续向下传播，不会被普通异常处理吞掉。

### 2. 工具执行与错误治理

- 建立统一工具注册表和执行入口，覆盖航班、火车、酒店及高德地图相关工具。
- 调用前检查工具是否存在，并使用工具参数 Schema 校验输入。
- 统一工具结果、元数据和错误结构，区分参数错误、未知工具、供应商失败和内部执行错误。
- 工具返回内容经过安全序列化与敏感字段脱敏后写入 `ToolMessage`。
- 保留工具调用 ID、工具名、参数、耗时、结果状态等关联信息，确保模型上下文和审计日志可以互相追踪。

### 3. 模型与 Prompt 装配

- Chat 模型通过 `bind_tools()` 绑定现有工具集合。
- 系统 Prompt 明确实时事实必须来自工具结果，不允许虚构价格、库存、班次、天气或路线信息。
- 每次请求注入当前日期、时间和时区，减少相对日期理解偏差。

### 4. SSE 事件协议

在保留原有会话事件的基础上，新增并接入以下事件：

- `message_start`
- `tool_call`
- `tool_result`
- `message_delta`
- `message_end`
- `done`
- `error`

后端会在工具开始、工具完成、回答增量和回答结束时发送对应事件。客户端断开或任务取消时会按现有会话持久化边界处理，不会伪造完成状态。

### 5. 工具调用审计

- 新增 `tool_call_logs` 数据表、ORM 模型和 Alembic 迁移。
- 记录会话、消息、工具调用 ID、工具名称、输入参数、执行状态、耗时、错误和脱敏后的结果摘要。
- 新增日志服务，将工具执行过程写入数据库，便于故障排查和后续可观测性建设。

### 6. 前端展示

- 扩展 SSE 客户端事件类型和解析逻辑。
- 聊天界面可实时显示工具调用中、成功和失败状态。
- 工具过程状态与最终自然语言回答分开展示，避免把中间执行信息混入正文。

### 7. 配置与文档

- 增加工具循环最大轮数、结果长度限制和日志相关配置。
- 更新 `.env.example` 与 `README.md`，说明配置项、事件协议、数据库迁移和运行方式。
- 在 `pyproject.toml` 中补充真实端到端测试标记。

## 数据库迁移

新增迁移：

```text
apps/api/alembic/versions/20260713_0002_create_tool_call_logs.py
```

迁移已在本地开发数据库执行，并确认 Alembic 当前版本位于最新 head。

## 验证结果

### 静态检查与构建

- Ruff 检查通过。
- Ruff 格式检查通过。
- 前端 TypeScript 类型检查通过。
- 前端 ESLint 检查通过。
- Next.js 生产构建通过。
- `git diff --check` 通过。

### 自动化测试

- 后端完整测试：`80 passed, 11 skipped`。
- 数据库集成测试：`1 passed`。
- FlyAI 真实集成测试：`1 passed`。
- 高德地图真实集成测试：`1 passed`，覆盖 IP 定位、POI、天气、路线和距离矩阵。
- 使用 `qwen3.7-plus` 的真实工具调用端到端测试：`8 passed`。

真实端到端场景覆盖：

| 场景 | 实际行为 |
| --- | --- |
| 航班查询 | 调用 `search_flight` 并成功返回 |
| 火车查询 | 调用 `search_train` 并成功返回 |
| 酒店查询 | 调用 `search_hotel` 并成功返回 |
| 天气查询 | 先定位当前城市，再调用天气工具 |
| 路线规划 | 搜索起终点 POI 后调用路线规划工具 |
| 交通对比 | 同轮调用航班和火车工具 |
| 缺少必要日期 | 不盲目调用工具，先向用户澄清 |
| 普通写作请求 | 不调用外部工具，直接回答 |

## 当前边界

- 当前是单 Agent 工具调用闭环，后续多 Agent 协作和 LangGraph 编排另行演进。
- 前端工具状态目前用于实时展示，尚未作为独立历史消息持久化。
- 后端仓库当前未配置 mypy 或 pyright，因此本次以后端测试与 Ruff 检查作为主要验证手段。
- 外部供应商结果仍受账号权限、网络和供应商可用性影响；系统会返回受控错误，不把失败结果包装成事实。

---

# LangGraph 结构化行程规划工作流 V1 完成摘要

> 实施日期：2026-07-14  
> 对应计划：`docs/plans/plan_executor.md`

本次实施以仓库实际代码、依赖、数据库模型和既有工具协议为准，完成了 LangGraph 结构化行程规划与修改工作流 V1。原有 `AgentExecutor` 工具调用闭环继续保留，完整规划和已有方案修改进入新增的 LangGraph 链路；本阶段没有拆分 Supervisor 或多个领域子 Agent。

## 架构与请求分流

`ChatService` 通过规则优先分类器将请求分为：

```text
普通聊天 / 单项旅游查询
  -> 现有 AgentExecutor

新建完整行程 / 修改已有方案
  -> LangGraph Trip Planner
```

分类不确定时继续使用原有 `AgentExecutor`，避免误触发完整规划。当前会话存在未完成草稿时，下一轮消息会继续进入规划图，以便合并此前已经提取的结构化需求。

LangGraph 包含九个节点：

1. `understand_request`
2. `check_required_fields`
3. `ask_clarification`
4. `collect_travel_data`
5. `generate_itinerary`
6. `validate_itinerary`
7. `revise_itinerary`
8. `persist_itinerary`
9. `finalize_response`

其中需求提取、候选行程生成、体验辅助校验和复杂修订可以使用 LLM 结构化输出；必要字段检查、工具任务构造、确定性校验、版本持久化和最终渲染采用确定性逻辑。

结构化 LLM 调用受独立超时保护。模型不支持结构化输出或调用超时时，系统会切换到保守的确定性降级逻辑，只使用已验证的工具候选，不虚构交通班次、价格、酒店、POI、坐标或天气。体验辅助校验失败只产生 warning，不会丢失确定性校验结果。

## 结构化数据模型

新增的核心模型包括：

- `TripRequest`：保存目的地、日期、人数、预算、偏好、节奏和特殊约束。
- `TransportOption`、`HotelOption`：保存标准化工具候选以及来源、查询时间和可验证字段。
- `Activity`、`DayPlan`、`BudgetSummary`：表示按日活动、交通耗时和预算组成。
- `ItineraryPlan`：完整结构化行程方案。
- `ValidationIssue`：统一表示 info、warning 和 error 级别的校验问题。
- `TripPlanningState`：分别保存需求、工具事实、当前方案、上一版本、校验结果、修订次数和持久化版本，不依赖自然语言消息作为唯一状态来源。

## 旅行数据采集

交通、酒店、第一轮 POI 和天气查询互不依赖，复用现有 `ToolExecutor` 并发执行。POI 获得坐标后，再分阶段调用距离矩阵和代表性路线规划。

复用能力包括：

- 工具 Schema 校验。
- 超时与错误转换。
- 敏感信息脱敏。
- SSE `tool_call`、`tool_result` 事件。
- PostgreSQL 工具调用审计日志。
- 同轮多工具并发执行。

工具结果通过保守适配器转换为结构化交通和酒店候选。无法核验的字段保持为空；部分工具失败时允许降级生成，但缺失信息会进入方案 warnings，未知实时费用不会被计入为已确认预算。

## 行程生成、校验和修订

行程生成会考虑：

- 第一天的抵达时间。
- 最后一天的返程时间。
- 景点之间的交通耗时。
- 轻松、适中和紧凑节奏的每日活动上限。
- 天气与室内、室外活动。
- 用户兴趣、必去地点和避开地点。
- 工具事实与经验估算之间的边界。

确定性校验覆盖日期连续性、抵达前和返程后活动、时间重叠、日程时长、活动数量、交通耗时、住宿日期、重复 POI、恶劣天气、预算以及虚构的班次、酒店价格和 POI 等问题。LLM 仅作为体验校验补充。

自动修订最多执行配置的次数，默认两轮。达到上限后停止循环，将仍未解决的问题写入 warnings，并保存当前最合理方案。

## 已有方案局部修改

修改请求会读取当前结构化方案、提取 `affected_sections`，只重新查询受影响的数据范围：

- 减少景点时复用已有 POI，不重新查询交通、酒店和 POI，但使用现有坐标重新计算受影响路线。
- 更换交通方式时只刷新交通数据，并重新编排首尾日。
- 更换酒店位置时刷新酒店及相关路线和预算。
- 修改预算时重新评估交通、住宿和预算摘要。
- 修改日期时刷新交通、酒店、天气和路线等时效数据。

每次有效修改会生成新的 `change_summary` 和历史版本，不覆盖旧版本。

## 数据库持久化

新增迁移：

```text
apps/api/alembic/versions/20260714_0003_create_travel_plans.py
```

新增表：

- `travel_plans`：保存当前方案、当前版本和 version 0 草稿。
- `travel_plan_versions`：保存每次有效生成或修改的不可变快照。

`TripRequest` 和 `ItineraryPlan` 使用 PostgreSQL `JSONB` 保存。新建方案生成版本 1，后续有效修改递增版本；数据库失败会返回受控错误，不会声称保存成功。

迁移验证结果：

- `alembic current`：`20260714_0003 (head)`。
- `alembic check`：`No new upgrade operations detected.`

## SSE 与前端

新增 `planning_stage` SSE 事件，支持：

```text
running
success
failed
skipped
```

前端将规划阶段、工具执行状态和最终 Markdown 正文分开展示。最终回答继续通过原有 `message_delta` 流式返回，不向客户端发送 LangGraph 完整 State、Prompt、模型思维过程、供应商原始响应或内部异常堆栈。

## 主要文件

### 新增文件

- `apps/api/app/graphs/__init__.py`
- `apps/api/app/graphs/trip_state.py`
- `apps/api/app/graphs/trip_planner.py`
- `apps/api/app/schemas/itinerary.py`
- `apps/api/app/services/itinerary_renderer.py`
- `apps/api/app/services/travel_data_collector.py`
- `apps/api/app/services/trip_plan_service.py`
- `apps/api/app/services/trip_request_router.py`
- `apps/api/app/services/trip_validation.py`
- `apps/api/alembic/versions/20260714_0003_create_travel_plans.py`
- `docs/architecture/trip_planner_v1.md`

### 主要修改文件

- `.env.example`
- `README.md`
- `apps/api/app/core/settings.py`
- `apps/api/app/db/models.py`
- `apps/api/app/main.py`
- `apps/api/app/schemas/tool_execution.py`
- `apps/api/app/services/chat_service.py`
- `apps/api/app/services/tool_execution.py`
- `apps/web/lib/api.ts`
- `apps/web/components/chat-shell.tsx`

### 新增和扩展的测试

- `apps/api/tests/test_itinerary_models.py`
- `apps/api/tests/test_trip_request_router.py`
- `apps/api/tests/test_trip_validation.py`
- `apps/api/tests/test_trip_planner_graph.py`
- `apps/api/tests/test_travel_data_collector.py`
- `apps/api/tests/test_chat_routing.py`
- `apps/api/tests/test_trip_planner_e2e.py`
- `apps/api/tests/test_api.py`
- `apps/api/tests/test_conversation_database.py`
- `apps/api/tests/test_settings.py`

## 验证结果

- 后端完整基线测试：`101 passed, 11 skipped`；地点提取修复后再次执行全量回归并通过。
- 最终 LangGraph、局部修改和数据采集定向测试：通过。
- PostgreSQL 方案版本化集成测试：`1 passed`。
- 真实模型和真实旅游工具端到端测试：`1 passed`，约 15 秒完成。
- 所有新增和修改 Python 文件的 Ruff 检查：通过。
- 前端 TypeScript 类型检查：通过。
- 前端 ESLint：通过。
- `git diff --check`：通过。
- Next.js 生产代码编译成功，但沙箱在后续派生进程阶段返回 `spawn EPERM`；沙箱外复跑审批通道连接中断，因此未取得完整生产构建成功退出码。

## 场景验证

| 场景 | 验证结果 |
| --- | --- |
| 完整行程规划 | 真实模型和真实工具端到端测试通过，生成并保存结构化方案 |
| 缺失日期追问 | 不调用旅行工具，保存草稿并只追问必要日期信息 |
| 多轮补全需求 | 下一轮日期信息与已保存的出发地、目的地合并后继续规划 |
| 修改第二天 | 只修改受影响活动，保留无关交通和酒店，并重新校验 |
| 更换交通方式 | 定向数据采集测试确认只调用交通工具组 |
| 更换酒店 | 定向数据采集测试确认只调用酒店及相关路线工具组 |
| 普通单项查询 | 继续进入原有 `AgentExecutor`，不进入 LangGraph |

## 后续修复：地点语义提取

针对测试中“规划一份去杭州”被错误解析为“从规划一份出发”的问题，地点提取已调整为：

- LLM 优先提取地点，并同时返回规范值、原文证据和是否明确表达。
- 后端验证证据确实存在于用户原文，并拒绝“规划一份、安排、攻略、行程”等明显非地点内容。
- 需求提取的原生 structured output 失败后，继续尝试严格 JSON 输出并使用 Pydantic 校验。
- 需求提取、行程生成和体验校验分别记录模型能力，一个节点失败不会直接禁用全部节点。
- 全部模型提取方式失败时，确定性规则只提取明确目的地、日期等安全字段，不再猜测出发地。
- 缺少可信出发地时保存草稿并追问，在用户确认前不调用航班或火车工具。
- 已保存草稿中的历史非法出发地会在下一轮合并前清除。
- 新增 30 秒需求提取总预算，原生 structured output 探测和 JSON 回退共享该预算。
- 修复后的真实模型、真实工具端到端测试通过。

## 当前边界与工作区说明

- V1 支持单个主要目的地和 2 至 5 天的国内行程。
- 本阶段没有继续拆分 Supervisor、航班 Agent、酒店 Agent 或其他领域子 Agent。
- 仍不支持自动预订、支付、多城市复杂行程、地图拖拽和长期记忆。
- 原工作区中已有的 `docs/plans/tool_call_implementation.md` 删除状态及顶层 `tool_call_implementation.md` 未跟踪文件未被本次实施修改。
