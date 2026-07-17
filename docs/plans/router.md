# Trip 请求路由 MVP 重构计划

## 一、背景

当前项目在 `apps/api/app/services/trip_request_router.py` 中，通过正则匹配用户最新一条消息，将请求分类为：

```text
general_chat
single_travel_query
new_trip_plan
modify_trip_plan
```

调用入口位于 `apps/api/app/services/chat_service.py`。

现有实现存在以下问题：

1. 只分析最新一条用户消息，无法稳定理解“4 天”“3 个人”“预算 5000”等多轮补充信息。
2. 正则无法可靠覆盖口语、省略表达、上下文相关修改和多意图请求。
3. `general_chat` 与 `single_travel_query` 最终都进入普通 Tool Calling，顶层没有必要区分。
4. 创作、查询和规划关键词可能同时出现，基于关键词优先级容易误路由。
5. 继续扩充正则会增加规则冲突，不能从根本上改善可用性。

本轮采用最小可用方案：使用固定 LLM 进行结构化语义路由，结合最近对话和现有行程状态选择下一条执行链路，直接替换当前正则主分类器。

---

## 二、本轮目标

本轮只重构顶层 Router，不重构 TripPlanner 的状态机和持久化流程。

需要完成：

1. 使用 LLM 结构化输出替换正则分类。
2. Router 输入包含最近对话、当前行程状态和现有草稿状态，而不是只有最新一句话。
3. 顶层路由合并为 `general_agent`、`trip_planner` 和 `clarify`。
4. Router 使用 `config/models.yaml` 中指定的固定模型，不跟随用户选择的聊天模型变化。
5. 通过兼容适配继续向现有 TripPlanner 传递 `new_trip_plan` 或 `modify_trip_plan`。
6. Router 调用或解析失败时安全回退到普通 Agent，不中断 SSE。
7. 增加单轮、多轮、上下文相关和失败降级测试。

---

## 三、本轮非目标

以下内容不在本轮实施范围内：

- 新增 `TripPlanningSession` 状态表。
- 新增独立 `TripDraft` 数据表。
- 引入 LangGraph checkpointer。
- 修改 Planner 当前的自动保存行为。
- 增加用户确认后再提交版本的流程。
- 修改 `TravelPlan` 与 `TravelPlanVersion` 数据模型。
- 支持一个会话管理多份独立行程。
- 持久化普通 Agent 的完整工具结果用于跨轮 handoff。
- 新增路由日志表、评估平台、shadow 或灰度发布。
- 重构 General Agent 或 TripPlanner 内部图结构。

这些内容作为后续演进方向记录，不阻塞本轮替换正则分类器。

---

## 四、路由职责

Router 只负责选择下一条执行链路：

```text
general_agent
trip_planner
clarify
```

### 1. `general_agent`

处理不需要进入完整行程规划流程的请求：

- 普通聊天。
- 旅游知识问答。
- 单项航班、火车、酒店、天气、POI 或路线查询。
- 不要求写入行程的推荐。
- 旅游攻略、朋友圈文案等创作请求。

是否调用工具由现有 Tool Calling Agent 自己决定。

### 2. `trip_planner`

处理需要创建或修改结构化行程的请求：

- 创建完整行程。
- 修改已有行程。
- 调整日期、预算、节奏、酒店、景点或交通安排。
- 查询旅行数据并明确要求加入行程。
- 同时包含规划和单项查询的多意图请求。
- 对现有草稿继续补充缺失字段。

### 3. `clarify`

仅在无法可靠判断用户是想普通查询还是创建、修改行程时使用。

`clarify` 不调用普通 Agent 或 TripPlanner。`ChatService` 根据结构化的 `clarification_kind` 输出简短模板问题，例如：

```text
你是只想查询相关信息，还是希望把结果加入行程？
```

不要因为一般性的语义模糊频繁追问。

---

## 五、结构化路由结果

新增严格的 Pydantic 路由模型，建议放在：

```text
apps/api/app/schemas/routing.py
```

最小字段如下：

```python
class TripRouteDecision(BaseModel):
    route: Literal["general_agent", "trip_planner", "clarify"]
    trip_action_hint: Literal["none", "create", "modify"] = "none"
    clarification_kind: Literal[
        "none",
        "query_or_plan",
        "create_or_modify",
    ] = "none"
    reason_code: Literal[
        "general_conversation",
        "single_travel_query",
        "create_trip",
        "modify_trip",
        "resume_draft",
        "mixed_with_planning",
        "ambiguous_persistence",
    ]
```

字段约束：

- `general_agent` 必须使用 `trip_action_hint = none`。
- `trip_planner` 可以提供 `create` 或 `modify` 提示。
- `clarify` 必须提供非 `none` 的 `clarification_kind`。
- `reason_code` 只用于调试和普通结构化日志，不向用户展示。
- 不使用模型自报的 `confidence` 参与业务决策。
- Router 不输出自由文本回答，不调用工具，也不执行数据库写入。

`trip_action_hint` 仅用于兼容现有 Planner 的 `PlanningIntent`，不在本轮建设完整动作分类体系。

---

## 六、Router 输入上下文

Router 至少接收以下上下文：

```text
最近 8 条有效对话消息
用户最新消息
has_current_plan
has_draft
stored_plan_status
当前行程的简要摘要
```

当前行程摘要只包含路由所需信息：

```text
目的地
开始日期
结束日期
是否已经生成正式方案
```

不要把完整行程 JSON、工具结果或全部历史消息传给 Router。

基于当前数据模型，状态按以下方式构造：

```text
stored is None
  → has_current_plan = false
  → has_draft = false

stored.plan is None
  → has_current_plan = false
  → has_draft = true

stored.plan is not None
  → has_current_plan = true
  → has_draft = false
```

这是本轮兼容现有代码的简化状态，不等同于完整的持久化工作流状态机。

---

## 七、Router 提示词边界

Router 系统提示词需要明确：

```text
你只负责选择下一条执行链路，不回答用户问题，不调用工具。

仅查询航班、火车、酒店、天气、POI、路线或旅游知识，
且没有要求写入行程时，选择 general_agent。

用户要求创建、安排、修改或重新规划完整行程时，
选择 trip_planner。

用户要求查询数据并将结果加入行程时，选择 trip_planner。

用户同时要求规划行程和执行单项查询时，选择 trip_planner，
由 TripPlanner 内部调用所需工具。

必须结合最近对话和当前行程状态理解省略表达，
不能只根据用户最新一句话判断。

当现有草稿正在等待补充信息，用户回复日期、天数、人数、
预算、出发地或旅行偏好时，选择 trip_planner。

只有普通查询与持久化行程操作之间确实无法判断时，
才选择 clarify。

只输出符合 TripRouteDecision 的结构化结果。
```

Router 使用低温度和严格结构化输出。优先使用模型原生 structured output；无法产生合法结构时视为路由失败并执行安全回退。

---

## 八、固定 Router 模型配置

当前 `config/models.yaml` 由 `ModelCatalog` 严格校验，只支持 `default_model` 和 `models`。本轮新增可选的固定 Router 模型字段：

```yaml
default_model: mimo-v2.5-pro
router_model: qwen3.7-plus

models:
  # 现有模型列表
```

同步修改 `apps/api/app/core/model_registry.py`：

```python
class ModelCatalog(BaseModel):
    default_model: str | None = None
    router_model: str | None = None
    models: list[ModelEntry]
```

校验要求：

- `router_model` 必须引用 `models` 中已存在且启用的模型。
- Router 始终使用 `router_model`，不使用前端请求中的 `model_id`。
- Router 模型复用对应 `ModelEntry` 的 API Key、Base URL、超时和重试配置。
- 缺少或无法加载 `router_model` 时，不影响普通聊天请求，Router 进入安全回退。
- 不在 `.env` 或源码中硬编码模型名称。

---

## 九、兼容现有 TripPlanner

现有 TripPlanner 入口仍要求：

```text
new_trip_plan
modify_trip_plan
```

本轮不修改 Planner 内部状态结构，而是在 Router 与 Planner 之间增加轻量适配：

```text
route != trip_planner
  → 不调用 TripPlanner

has_draft
  → new_trip_plan

trip_action_hint == create
  → new_trip_plan

trip_action_hint == modify 且 has_current_plan
  → modify_trip_plan

has_current_plan
  → modify_trip_plan

其他 trip_planner 请求
  → new_trip_plan
```

如果模型输出 `modify`，但当前不存在正式行程，则兼容层转换为 `new_trip_plan`，避免进入现有的“没有可修改方案”分支。

旧的 `PlanningIntent` 暂时保留，仅作为 TripPlanner 内部兼容协议。旧正则分类结果不再控制主业务链路。

---

## 十、安全回退

以下情况视为 Router 失败：

- 固定 Router 模型未配置或不可用。
- Router 调用超时。
- 模型不支持结构化输出。
- 输出无法通过 Pydantic 校验。
- 输出枚举或字段组合非法。

MVP 统一回退为：

```text
general_agent
```

回退要求：

- 记录不包含完整用户原文的 warning 日志。
- 不向用户展示内部异常、模型名称、Prompt 或解析错误。
- 不因路由失败中断 SSE。
- 不继续调用旧正则分类器。
- 不在回退过程中执行行程持久化操作。

这种回退可能让少数规划请求暂时进入普通 Agent，但比因 Router 故障阻断所有聊天更适合本轮 MVP。

---

## 十一、`ChatService` 主链路调整

调整 `apps/api/app/services/chat_service.py`：

```text
1. 根据用户 model_id 创建后续聊天模型
2. 加载当前 StoredTripPlan
3. 构造精简 RouteContext
4. 使用固定 router_model 执行结构化路由
5. 校验 TripRouteDecision
6. 失败时回退 general_agent
7. general_agent → 进入现有 Tool Calling Agent
8. trip_planner → 转换为旧 PlanningIntent 后进入现有 TripPlanner
9. clarify → 输出服务端模板问题
10. 保持现有 SSE 和消息持久化流程
```

Router 调用发生在绑定普通工具之前。Router 模型不绑定航班、酒店、天气、地图等工具。

建议将 Router 封装为独立服务：

```python
class TripRequestRouter:
    async def route(
        self,
        messages: list[ChatMessage],
        *,
        stored: StoredTripPlan | None,
    ) -> ResolvedTripRoute:
        ...
```

不要把 Router 的 Prompt、解析、回退和兼容映射全部堆入 `ChatService.stream()`。

---

## 十二、实施任务

### Task 1：新增路由 Schema

- 新增 `TripRouteDecision`。
- 新增内部 `ResolvedTripRoute`，记录最终来源 `llm_router` 或 `fallback`。
- 为字段组合增加 Pydantic 校验。
- 为 `clarification_kind` 提供服务端模板映射。

### Task 2：扩展模型目录

- 为 `ModelCatalog` 新增 `router_model`。
- 校验引用的模型存在且启用。
- 在 `config/models.yaml` 配置 Router 模型。
- 为目录解析与热加载增加测试。

### Task 3：重写 `trip_request_router.py`

- 删除正则作为主分类逻辑。
- 构造最近 8 条消息和精简行程摘要。
- 调用固定 Router 模型。
- 使用严格结构化输出。
- 实现安全回退。
- 不执行工具调用或数据库写入。

### Task 4：接入 `ChatService`

- 用新 Router 替换 `classify_trip_request()`。
- 实现 `trip_planner` 到旧 `PlanningIntent` 的兼容映射。
- 实现 `clarify` 的模板化 SSE 文本输出。
- 保持普通 Tool Calling 与 TripPlanner 现有执行方式不变。

### Task 5：移除旧正则主链路

- 删除或弃用 `_PLAN_WORDS`、`_REVISION_WORDS`、`_SINGLE_QUERY_WORDS`、`_CREATIVE_WORDS`。
- 不保留“LLM 失败后重新使用旧正则”的分支。
- 如暂时保留 `classify_trip_request` 名称，只允许作为新 Router 的兼容包装，不得继续执行旧逻辑。

### Task 6：增加测试

- 新增 Router 单元测试。
- 更新 `test_chat_routing.py`。
- 保留现有 TripPlanner 图测试。
- 使用 Fake Router Model，测试不依赖真实外部模型。

---

## 十三、测试场景

### 1. 普通查询

```text
北京明天天气怎么样？
帮我查一下上海到成都的机票。
成都有哪些值得去的景点？
```

期望：`general_agent`。

### 2. 新建行程

```text
帮我规划成都四日游。
我周五到南京，周日下午走，帮我安排一下。
第一次去云南，五天，不想太赶。
```

期望：

```text
route = trip_planner
trip_action_hint = create
```

### 3. 修改已有行程

前置条件：存在正式行程。

```text
第二天不要去熊猫基地了。
酒店换个便宜一点的。
行程太赶了，放松一点。
```

期望：

```text
route = trip_planner
trip_action_hint = modify
```

### 4. 多轮草稿补充

前置条件：`stored.plan is None`，已有未完成的需求草稿。

```text
四天。
三个人。
预算五千左右，不要太赶。
```

期望：

```text
route = trip_planner
reason_code = resume_draft
```

### 5. 查询与写入边界

```text
查一下第二天附近的酒店。
```

期望 `general_agent`。

```text
查一下第二天附近的酒店，然后放进行程。
```

期望 `trip_planner`。

```text
只查机票，先不要规划行程。
```

期望 `general_agent`。

### 6. 多意图请求

```text
帮我规划成都四天，再查一下合适的往返航班。
帮我规划成都四日游，最后生成一段朋友圈文案。
```

期望均为 `trip_planner`，不能因为包含查询或文案而忽略规划请求。

### 7. 上下文省略

```text
用户：帮我规划成都旅行。
助手：计划几天？
用户：四天。
```

Router 必须结合历史理解“四天”，不能仅按最新一句路由到普通 Agent。

### 8. 需要澄清

```text
把刚才那个加进去。
```

当上下文中不存在明确候选项或目标行程时，期望：

```text
route = clarify
clarification_kind = query_or_plan
```

### 9. 失败降级

覆盖：

- Router 模型抛出异常。
- structured output 不可用。
- 返回非法枚举。
- 返回不符合字段组合的数据。
- Router 超时。

期望均为 `general_agent`，并且普通 Tool Calling 与 SSE 可以继续运行。

### 10. 固定模型

当用户选择不同聊天模型时，断言 Router 始终由 `config/models.yaml` 中的 `router_model` 创建。

---

## 十四、验收标准

本轮完成后应满足：

1. 主业务链路不再通过正则分类用户请求。
2. Router 使用最近对话和当前 StoredTripPlan 状态，而不是只分析最新消息。
3. “4 天”“3 个人”“预算 5000”等草稿补充能够继续进入 TripPlanner。
4. 普通航班、酒店、天气和 POI 查询进入 General Agent。
5. 创建、修改以及“查询并加入行程”请求进入 TripPlanner。
6. 规划与创作、查询混合的请求不会因为单个关键词误路由。
7. Router 使用配置的固定模型，不受用户聊天模型选择影响。
8. Router 输出解析失败时回退 General Agent，不中断 SSE。
9. 原有普通 Tool Calling、TripPlanner、消息持久化和 SSE 流程继续工作。
10. 不引入数据库迁移，不修改现有行程保存语义。

---

## 十五、已知限制与后续演进

本轮有意接受以下限制：

- `TravelPlan` 仍同时承担现有草稿与正式方案存储职责。
- Planner 仍会按当前逻辑自动保存生成结果。
- 没有持久化的 Planner 工作流状态与 checkpoint。
- 暂不支持可靠的跨轮工具结果引用和 General Agent handoff。
- 一个会话仍只能对应当前数据模型允许的单个行程记录。
- Router 错误进入 Planner 的风险只能通过 Prompt、结构校验和测试降低，不能完全消除。

后续如果需要增强可靠性，再单独设计：

```text
TripPlanningSession
独立 TripDraft
用户确认后提交 TravelPlanVersion
一个会话多行程
工具结果 artifact
并发与幂等控制
路由日志与评估数据集
```

这些内容不应重新混入本轮 Router MVP。

---

## 十六、最终链路

```text
前端 POST /api/v1/chat/stream
→ 创建或恢复会话并保存用户消息
→ 创建用户选择的后续聊天模型
→ 加载当前 StoredTripPlan
→ 构造最近对话与精简行程状态
→ 使用固定 router_model 获取 TripRouteDecision
→ 结构校验；失败则回退 general_agent
→ general_agent / trip_planner / clarify
→ 复用现有 Tool Calling 或 TripPlanner
→ SSE 输出
→ 保存助手消息
→ 结束请求
```

核心原则：

```text
本轮只替换实用性不足的正则分类器。

Router 根据最近对话、当前行程状态和用户语义选择执行链路，
同时保持现有 Planner 与持久化实现不变。
```
