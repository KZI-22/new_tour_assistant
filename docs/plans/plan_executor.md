# LangGraph 结构化行程规划工作流 V1 实施任务

## 一、项目当前状态

当前项目已经完成单 Agent 工具调用闭环，具备：

* Next.js 聊天界面
* FastAPI SSE 流式响应
* PostgreSQL 会话和消息持久化
* 多模型配置、切换和可用性检查
* LangChain `bind_tools()`
* 多轮工具调用循环
* 同轮多工具并发执行
* 统一工具结果和错误处理
* 工具调用审计日志
* 前端工具调用状态展示
* 请求取消和异常治理

当前已经注册并可真实调用 9 个旅游工具：

### FlyAI

* `search_flight`
* `search_train`
* `search_hotel`
* `search_poi`

### 高德地图

* `amap_get_current_city`
* `amap_search_places`
* `amap_plan_route`
* `amap_travel_time_matrix`
* `amap_get_weather`

当前工具调用链路已经稳定：

```text
用户消息
→ 模型判断是否调用工具
→ 工具执行
→ ToolMessage 回填
→ 模型继续调用工具或生成最终回答
→ SSE 返回结果
```

本阶段需要在此基础上，实现：

```text
LangGraph 结构化行程规划与修改工作流 V1
```

---

# 二、本次目标

将系统从“单项旅游查询助手”升级为“完整行程规划助手”。

目标场景示例：

```text
帮我规划 2026 年 7 月 20 日到 23 日，
从南京去杭州，两个人，预算 4000 元，
喜欢自然风景和人文景点，行程不要太赶。
```

系统应完成：

1. 提取用户旅行需求。
2. 判断必要参数是否缺失。
3. 缺失必要参数时向用户追问。
4. 查询跨城交通。
5. 查询酒店。
6. 查询景点和 POI。
7. 查询天气。
8. 计算地点之间的距离和耗时。
9. 生成按天排列的结构化行程。
10. 检查时间、路线、预算和节奏是否合理。
11. 根据校验结果自动修订。
12. 保存行程方案。
13. 支持用户基于已有方案继续修改。

例如用户继续说：

```text
第二天太满了，安排轻松一点。
```

系统应基于已有行程修改第二天，而不是重新丢失上下文、从零开始规划。

---

# 三、本阶段设计原则

## 1. 保留现有 AgentExecutor

现有单 Agent 工具调用执行器继续保留，用于：

* 航班查询
* 火车查询
* 酒店查询
* 天气查询
* 路线查询
* POI 推荐
* 普通聊天
* 单项实时信息查询

不要推翻或重写现有工具调用闭环。

---

## 2. 新增 LangGraph 行程规划链路

请求需要分为三类：

```text
普通聊天
单项旅游查询
完整行程规划或已有方案修改
```

建议路由结构：

```text
用户消息
  ↓
请求分类
  ├── 普通聊天或单项查询
  │      ↓
  │  现有 AgentExecutor
  │
  └── 完整规划或方案修改
         ↓
     LangGraph Trip Planner
```

---

## 3. 暂时不要拆大量子 Agent

本阶段不要实现：

* 航班 Agent
* 火车 Agent
* 酒店 Agent
* 天气 Agent
* 路线 Agent
* 景点 Agent
* 预算 Agent
* 审查 Agent
* Supervisor Agent

当前采用：

```text
一个 LangGraph 编排工作流
+ 少量 LLM 节点
+ 确定性业务节点
+ 现有工具执行基础设施
```

工具调用本身不等于 Agent。

只有未来某个领域需要独立多步推理、搜索、筛选和重试时，才考虑拆分为子 Agent 或子图。

---

# 四、第一版业务范围

V1 先限定为：

```text
单个主要目的地
2～5 天
包含跨城交通、住宿、景点、天气和市内路线
支持生成和修改行程
```

暂时不做：

* 多城市复杂旅行
* 国际行程
* 自动预订或支付
* 多用户协同
* 地图拖拽编辑
* 长期记忆
* 旅游 RAG
* 手机端 App
* 高复杂度预算优化算法

---

# 五、核心数据模型

引入 LangGraph 前，优先定义稳定的结构化模型。

不要只依赖自然语言和 `messages` 保存行程状态。

## 1. TripRequest

用于描述用户旅行需求。

建议字段：

```python
class TripRequest(BaseModel):
    origin: str | None = None
    destinations: list[str] = []

    start_date: date | None = None
    end_date: date | None = None
    duration_days: int | None = None

    traveler_count: int | None = None
    adults: int | None = None
    children: int | None = None

    total_budget: float | None = None
    hotel_budget_per_night: float | None = None

    transport_preferences: list[str] = []
    hotel_preferences: dict[str, Any] = {}
    interests: list[str] = []
    pace: str | None = None

    must_visit: list[str] = []
    avoid_places: list[str] = []
    special_requirements: list[str] = []
```

需要兼容：

* 今天
* 明天
* 后天
* 下周末
* 三天两晚
* 玩三天
* 7 月 20 日到 23 日

所有相对日期必须基于当前日期和时区解析。

---

## 2. TransportOption

```python
class TransportOption(BaseModel):
    transport_type: str
    provider: str | None = None

    departure_city: str
    arrival_city: str
    departure_time: datetime | None = None
    arrival_time: datetime | None = None

    flight_number: str | None = None
    train_number: str | None = None

    origin_station: str | None = None
    destination_station: str | None = None

    price: float | None = None
    seat_or_cabin: str | None = None
    duration_minutes: int | None = None

    source_tool: str
    source_reference: str | None = None
```

工具没有返回的字段不能由模型虚构。

---

## 3. HotelOption

```python
class HotelOption(BaseModel):
    name: str
    address: str | None = None
    poi_id: str | None = None
    coordinates: str | None = None

    star_level: str | None = None
    room_type: str | None = None
    bed_type: str | None = None

    nightly_price: float | None = None
    total_price: float | None = None

    check_in_date: date
    check_out_date: date

    source_tool: str
```

---

## 4. Activity

```python
class Activity(BaseModel):
    start_time: time | None = None
    end_time: time | None = None

    place_name: str
    poi_id: str | None = None
    coordinates: str | None = None

    activity_type: str
    estimated_duration_minutes: int | None = None
    estimated_cost: float | None = None

    indoor: bool | None = None
    notes: str | None = None
```

---

## 5. DayPlan

```python
class DayPlan(BaseModel):
    date: date
    day_index: int
    theme: str | None = None

    activities: list[Activity] = []

    estimated_transport_time_minutes: int = 0
    estimated_activity_cost: float | None = None

    weather_summary: str | None = None
    warnings: list[str] = []
```

---

## 6. BudgetSummary

```python
class BudgetSummary(BaseModel):
    transport_cost: float | None = None
    hotel_cost: float | None = None
    activity_cost: float | None = None
    local_transport_cost: float | None = None
    food_estimate: float | None = None

    total_estimated_cost: float | None = None
    user_budget: float | None = None
    over_budget: bool | None = None

    assumptions: list[str] = []
```

---

## 7. ItineraryPlan

```python
class ItineraryPlan(BaseModel):
    title: str

    origin: str | None = None
    destination: str
    start_date: date
    end_date: date

    outbound_transport: TransportOption | None = None
    return_transport: TransportOption | None = None

    hotel: HotelOption | None = None

    days: list[DayPlan] = []

    budget: BudgetSummary | None = None

    assumptions: list[str] = []
    warnings: list[str] = []
```

---

## 8. ValidationIssue

```python
class ValidationIssue(BaseModel):
    code: str
    severity: str
    message: str

    day_index: int | None = None
    activity_index: int | None = None

    suggested_action: str | None = None
```

严重程度建议：

```text
info
warning
error
```

---

## 9. TripPlanningState

LangGraph 状态建议包含：

```python
class TripPlanningState(TypedDict):
    messages: list[BaseMessage]

    intent: str
    is_plan_revision: bool

    request: TripRequest | None
    missing_fields: list[str]

    transport_results: list[TransportOption]
    hotel_results: list[HotelOption]
    poi_results: list[dict]
    weather_results: list[dict]
    route_results: list[dict]

    current_plan: ItineraryPlan | None
    previous_plan: ItineraryPlan | None

    validation_issues: list[ValidationIssue]
    revision_count: int

    plan_id: str | None
    plan_version: int | None

    current_stage: str
    clarification_question: str | None
    final_answer: str | None
```

根据当前 LangGraph 版本选择 `TypedDict`、Pydantic 或合适的状态定义方式。

---

# 六、LangGraph 节点设计

第一版建议实现以下节点：

```text
START
  ↓
understand_request
  ↓
check_required_fields
  ├── 缺失信息
  │      ↓
  │ ask_clarification
  │      ↓
  │     END
  │
  └── 信息完整
         ↓
   collect_travel_data
         ↓
   generate_itinerary
         ↓
   validate_itinerary
       ├── 存在严重问题且未超过修改次数
       │          ↓
       │   revise_itinerary
       │          ↓
       └── validate_itinerary
       
       校验通过或达到上限
                 ↓
          persist_itinerary
                 ↓
          finalize_response
                 ↓
                END
```

---

## 1. understand_request

职责：

* 识别普通查询、完整规划和方案修改。
* 从当前消息和历史消息中提取结构化旅行需求。
* 判断是新建行程还是修改已有方案。
* 解析相对日期。
* 合并用户上一轮已确认的条件。
* 输出 `TripRequest`。
* 对修改请求提取变化字段。

该节点可以使用 LLM 结构化输出。

禁止在此节点直接调用 FlyAI 或高德工具。

---

## 2. check_required_fields

确定性节点，不使用 LLM。

至少检查：

* 是否存在目的地。
* 是否存在开始日期。
* 是否存在结束日期或旅行天数。
* 结束日期是否早于开始日期。
* 日期是否已过期。
* 需要查询跨城交通时是否有出发地。
* 出发地和目的地是否相同。
* 住宿日期是否有效。
* 行程天数是否在 V1 支持范围内。

必要字段缺失时，只追问当前必须补充的信息。

例如：

```text
你计划哪一天出发，大约玩几天？
```

不要一次询问大量非必要偏好。

预算、人数、兴趣、酒店等级等不是所有场景下的硬性必填字段。

可以使用默认值时，必须在最终方案的 `assumptions` 中明确记录。

---

## 3. ask_clarification

职责：

* 根据 `missing_fields` 生成简洁、明确的追问。
* 不调用外部工具。
* 不生成虚假行程。
* 将当前已提取的 `TripRequest` 保存到会话或规划草稿状态，供下一轮继续补充。

追问后结束当前图执行。

下一轮用户回答时，应继续使用之前的结构化需求，而不是重新从零提取。

---

## 4. collect_travel_data

根据 `TripRequest` 确定性创建工具查询任务。

需要根据需求调用现有工具：

### 跨城交通

按用户偏好调用：

```text
search_flight
search_train
```

用户未指定交通方式时，可以并行查询飞机和火车，再交给后续节点比较。

需要考虑：

* 去程
* 返程
* 出发日期
* 返回日期
* 价格
* 时长
* 出发和到达时间

---

### 酒店

调用：

```text
search_hotel
```

参数应基于：

* 目的城市
* 入住日期
* 退房日期
* 用户预算
* 酒店等级
* 床型和房型偏好

---

### 景点

调用：

```text
search_poi
amap_search_places
```

先获取候选景点，再补全：

* POI ID
* 地址
* 坐标
* 类型
* 是否适合用户兴趣
* 是否属于室内或室外

禁止凭模型记忆补充实时营业状态、价格或坐标。

---

### 天气

调用：

```text
amap_get_weather
```

查询旅行日期范围内的天气。

天气超出供应商预报范围时：

* 不得虚构天气。
* 应明确说明暂时无法获得准确预报。
* 仅提供季节性一般建议，并标记为一般建议。

---

### 距离和耗时

调用：

```text
amap_travel_time_matrix
amap_plan_route
```

建议先使用距离矩阵计算候选景点之间的批量耗时，再对最终选中的具体路线调用路线规划。

不要对所有候选地点两两调用路线规划，避免调用量失控。

---

### 并发执行

互不依赖的数据查询可以并发执行，例如：

```text
交通查询
酒店查询
天气查询
第一轮景点查询
```

存在依赖的查询必须分阶段执行，例如：

```text
景点搜索
→ 获取坐标
→ 距离矩阵
→ 路线规划
```

可以复用现有工具执行器的：

* Schema 校验
* 超时
* 重试
* 错误转换
* 敏感信息脱敏
* 审计日志
* 并发执行

不要在 LangGraph 中重新实现一套不同的工具调用协议。

---

## 5. generate_itinerary

使用结构化工具结果生成候选 `ItineraryPlan`。

必须优先输出结构化数据，不要直接只生成 Markdown。

生成规则：

* 第一天考虑到达时间。
* 最后一天考虑返程时间。
* 同一区域景点尽量安排在同一天。
* 计算景点之间的交通耗时。
* 根据用户节奏偏好控制每日活动数量。
* 根据天气调整室内和室外活动。
* 根据兴趣选择景点。
* 不重复安排相同景点。
* 不安排工具结果中不存在的具体航班、车次和酒店价格。
* 查询不到价格时使用 `None`，并在警告中说明。
* 预算估算必须区分实际工具价格和经验估算。

建议节奏规则：

```text
轻松：每天 2～3 个主要活动
适中：每天 3～4 个主要活动
紧凑：每天 4～5 个主要活动
```

这只是默认规则，应允许用户显式偏好覆盖。

---

## 6. validate_itinerary

采用：

```text
确定性校验为主
LLM 体验校验为辅
```

### 确定性校验

至少检查：

* 日期是否连续。
* 每天日期是否在行程范围内。
* 第一天是否在到达前安排活动。
* 最后一天是否在返程后安排活动。
* 活动时间是否重叠。
* 景点之间交通耗时是否被计入。
* 一天活动数量是否超出节奏限制。
* 活动时间加交通时间是否超过合理日程。
* 酒店日期是否覆盖全部住宿晚数。
* 是否有重复景点。
* 是否安排了同名或同 POI 活动。
* 室外活动是否与明显恶劣天气冲突。
* 总预算是否明显超过用户预算。
* 是否使用工具没有返回的具体价格、班次或余票。
* 起点、终点坐标是否缺失。
* 路线是否出现明显跨区域来回折返。

---

### LLM 辅助校验

可以检查：

* 行程是否符合轻松、适中或紧凑节奏。
* 景点组合是否符合用户兴趣。
* 每日主题是否连贯。
* 旅行体验是否存在明显不合理。
* 是否遗漏用户必须访问的地点。
* 是否包含用户明确要求避开的地点。

LLM 校验结果也必须转换为 `ValidationIssue`。

---

## 7. revise_itinerary

根据 `validation_issues` 修改候选方案。

设置：

```python
MAX_PLAN_REVISIONS = 2
```

修改原则：

* 仅修改有问题的日期和活动。
* 尽量保留用户已确认的交通和酒店。
* 不得无理由替换全部方案。
* 修改景点后重新计算受影响的距离和路线。
* 修改预算后重新选择受影响的交通或酒店。
* 修改节奏后主要调整每日活动数量和时间安排。

达到最大修改次数后：

* 停止循环。
* 保留当前最合理方案。
* 将剩余问题写入 `warnings`。
* 不得无限循环。

---

## 8. persist_itinerary

负责保存结构化旅行方案。

建议新增：

```text
travel_plans
travel_plan_versions
```

### travel_plans

建议字段：

```text
id
session_id
user_id（如果当前项目有）
title
status
current_version
request_json
plan_json
created_at
updated_at
```

`status` 可以包括：

```text
draft
active
archived
```

---

### travel_plan_versions

建议字段：

```text
id
plan_id
version
request_json
plan_json
change_summary
created_at
```

第一版使用 PostgreSQL `JSONB` 保存 `TripRequest` 和 `ItineraryPlan`。

暂时不要将所有活动、酒店和交通拆成大量关系表。

要求：

* 新建行程生成版本 1。
* 每次有效修改生成新版本。
* 保留旧版本。
* 数据库写入失败不能伪造保存成功。
* 版本保存失败时应返回受控错误。

---

## 9. finalize_response

将 `ItineraryPlan` 渲染成用户可读内容。

建议输出结构：

```text
行程概览
交通建议
住宿建议
每日行程
预算估算
天气提醒
方案假设
注意事项
```

最终回复中必须区分：

* 真实工具查询结果
* 系统计算结果
* 经验估算
* 默认假设

该节点只负责展示，不应重新修改结构化行程。

最终自然语言回复继续通过现有 SSE `message_delta` 流式返回。

---

# 七、已有行程修改流程

系统必须支持用户修改已有方案。

示例：

```text
第二天太累了，减少一个景点。
```

```text
不要飞机，只看高铁。
```

```text
把酒店换成西湖附近的。
```

```text
预算提高到 5000。
```

```text
第三天上午我要开会。
```

修改流程建议：

```text
读取当前 travel_plan
→ 提取用户修改意图
→ 更新 TripRequest 或 ItineraryPlan
→ 判断受影响的数据范围
→ 只重新查询相关工具
→ 修改对应行程
→ 重新校验
→ 保存新版本
→ 返回修改摘要
```

局部修改原则：

* 修改第二天景点时，不重新查询全部往返交通。
* 修改酒店区域时，只重新查询酒店和受影响路线。
* 修改交通方式时，重新查询交通并调整首尾日程。
* 修改预算时，重新评估交通、酒店和预算摘要。
* 修改日期时，需要重新查询交通、酒店、天气和相关营业信息。
* 修改目的地时，可以视为新规划或大版本修改。

需要生成清晰的 `change_summary`，例如：

```text
第二天活动从 4 个减少为 3 个，
移除宋城，延长西湖游览时间，
预计当天步行和交通时间减少约 90 分钟。
```

---

# 八、请求分类和路由

新增规划请求分类器。

建议分类值：

```text
general_chat
single_travel_query
new_trip_plan
modify_trip_plan
```

分类器可以使用：

* 规则优先
* LLM 结构化分类补充

明显规则示例：

```text
“帮我查航班” → single_travel_query
“帮我规划三天行程” → new_trip_plan
“把第二天改轻松” → modify_trip_plan
“给我写一句旅行文案” → general_chat
```

分类失败时，优先进入现有 AgentExecutor，不要误触发复杂规划。

修改请求只有在当前会话存在有效 `travel_plan` 时才进入修改流程。

如果不存在计划，应提示用户当前没有可修改的行程。

---

# 九、SSE 规划阶段事件

保留当前事件：

```text
message_start
tool_call
tool_result
message_delta
message_end
done
error
```

新增：

```text
planning_stage
```

事件示例：

```json
{
  "type": "planning_stage",
  "stage": "collecting_transport",
  "display_name": "正在查询交通方案",
  "status": "running"
}
```

建议阶段：

```text
understanding_request
checking_requirements
collecting_transport
collecting_hotels
collecting_pois
collecting_weather
calculating_routes
generating_itinerary
validating_itinerary
revising_itinerary
saving_itinerary
finalizing
```

状态可以包括：

```text
running
success
failed
skipped
```

不要向前端发送：

* 模型思维过程
* LangGraph 完整 State
* Prompt
* 工具原始响应
* 内部异常堆栈

---

# 十、前端 V1 要求

第一版不要求复杂地图和卡片，但需要支持：

* 展示规划阶段。
* 展示当前正在查询交通、酒店、景点等状态。
* 展示最终 Markdown 行程。
* 正确处理追问。
* 正确处理中断。
* 正确处理规划失败。
* 不影响现有普通聊天和工具状态展示。

建议前端将规划状态和正文分开展示：

```text
✓ 已理解旅行需求
✓ 已查询交通
✓ 已筛选酒店
• 正在优化每日路线

最终行程正文……
```

后端应同时保存结构化行程数据，为未来前端卡片和时间轴预留能力。

---

# 十一、配置项

建议新增配置：

```text
TRIP_PLANNER_ENABLED=true
TRIP_PLANNER_MAX_DAYS=5
TRIP_PLANNER_MAX_REVISIONS=2
TRIP_PLANNER_MAX_POI_CANDIDATES=20
TRIP_PLANNER_MAX_DAILY_ACTIVITIES=5
TRIP_PLANNER_TOOL_TIMEOUT_SECONDS=
TRIP_PLANNER_RESULT_MAX_LENGTH=
```

配置需加入：

* `.env.example`
* 配置模型
* README

不要将限制值散落硬编码在多个节点中。

---

# 十二、错误处理

统一处理：

* 请求分类失败
* 结构化需求提取失败
* 日期解析失败
* 缺少必要字段
* 不支持的行程天数
* 交通查询失败
* 酒店查询失败
* POI 查询失败
* 天气查询失败
* 距离矩阵失败
* 路线规划失败
* 模型结构化输出失败
* 行程校验失败
* 最大修订次数超限
* 数据库保存失败
* 用户取消请求
* SSE 客户端断开

允许部分工具失败后降级生成方案，但必须明确说明缺失信息。

例如：

```text
酒店实时价格查询暂时失败，
以下行程先按区域安排，住宿价格未计入总预算。
```

不得将失败结果包装成真实数据。

---

# 十三、测试要求

## 1. 数据模型测试

覆盖：

* 日期序列化
* JSONB 序列化
* 可选字段
* 非法日期
* 空目的地
* 负预算
* 行程天数计算
* 版本序列化和反序列化

---

## 2. 请求理解测试

使用 Fake Model 或结构化 Stub，覆盖：

```text
南京去杭州玩三天
下周末去苏州
7 月 20 日到 23 日上海旅行
两个人预算 4000
喜欢自然和人文
行程不要太赶
```

验证正确生成 `TripRequest`。

---

## 3. 缺失字段测试

覆盖：

```text
帮我规划一次旅行
帮我规划杭州行程
从南京去杭州
```

验证：

* 只追问必要字段。
* 不调用外部工具。
* 已提取字段在下一轮保留。

---

## 4. 路由测试

覆盖：

```text
查航班 → AgentExecutor
查天气 → AgentExecutor
规划三天杭州行程 → LangGraph
修改第二天 → LangGraph 修改流程
旅行文案 → 普通聊天
```

---

## 5. 数据采集节点测试

使用 Fake Tools，验证：

* 交通和酒店可以并发。
* POI 查询后再进行距离矩阵。
* 工具失败不会导致未处理异常。
* 工具结果正确写入 State。
* 不重复调用相同任务。
* 调用数量受配置限制。

---

## 6. 行程生成测试

验证：

* 每天日期正确。
* 活动属于有效日期。
* 第一天考虑抵达时间。
* 最后一天考虑返程时间。
* 不生成重复景点。
* 不生成工具结果之外的具体班次和价格。
* 符合轻松、适中、紧凑节奏。

---

## 7. 行程校验测试

至少覆盖：

* 活动时间重叠。
* 抵达前安排活动。
* 返程后安排活动。
* 单日活动过多。
* 交通时间不足。
* 酒店日期不覆盖。
* 重复景点。
* 超预算。
* 恶劣天气安排过多户外活动。
* 虚构价格或班次。

---

## 8. 自动修订测试

覆盖：

* 第一次校验失败。
* 修订后通过。
* 连续失败达到最大次数。
* 剩余问题进入 warnings。
* 不发生无限循环。

---

## 9. 方案持久化测试

覆盖：

* 新建计划版本 1。
* 修改后版本加 1。
* 旧版本保留。
* `current_version` 更新。
* JSONB 内容可恢复。
* 当前会话只能读取自己的方案。
* 数据库失败时返回受控错误。

---

## 10. SSE 测试

验证事件顺序：

```text
message_start
planning_stage
tool_call
tool_result
planning_stage
message_delta
message_end
done
```

同时覆盖：

* 缺失字段追问。
* 正常规划。
* 工具部分失败。
* 自动修订。
* 用户取消。
* 数据库保存失败。

---

# 十四、真实端到端验证场景

## 场景一：信息完整

```text
帮我规划 2026 年 7 月 20 日到 23 日，
从南京去杭州，两个人，预算 4000 元，
喜欢自然和人文景点，行程轻松一点。
```

预期：

* 进入 LangGraph。
* 查询交通、酒店、景点、天气和路线。
* 生成 4 天结构化行程。
* 保存版本 1。

---

## 场景二：缺少日期

```text
帮我规划南京到杭州的旅行。
```

预期：

* 不调用交通和酒店工具。
* 追问出发日期和旅行天数。

---

## 场景三：继续补充

上一轮后用户回答：

```text
7 月 20 日出发，玩三天。
```

预期：

* 合并上一轮出发地和目的地。
* 不重复询问已知信息。
* 开始规划。

---

## 场景四：修改节奏

```text
第二天太满了，减少一个景点。
```

预期：

* 读取当前计划。
* 只修改第二天。
* 重新校验受影响内容。
* 保存新版本。

---

## 场景五：修改交通方式

```text
不要飞机，只坐高铁。
```

预期：

* 重新查询火车。
* 更新交通。
* 调整第一天和最后一天。
* 不重新搜索全部无关 POI。

---

## 场景六：修改酒店位置

```text
酒店换到西湖附近，每晚不超过 600 元。
```

预期：

* 重新查询酒店。
* 更新受影响的路线和预算。
* 保存新版本。

---

## 场景七：普通单项查询

```text
帮我查明天南京到杭州的高铁。
```

预期：

* 继续走现有 AgentExecutor。
* 不进入完整行程规划图。

---

# 十五、建议实施顺序

```text
Task 1：检查现有 ChatService、AgentExecutor 和 SSE 路由
Task 2：定义 TripRequest 和 ItineraryPlan 等结构化模型
Task 3：新增 travel_plans 和 travel_plan_versions 迁移
Task 4：实现规划请求分类
Task 5：实现 understand_request
Task 6：实现 check_required_fields 和 ask_clarification
Task 7：实现 collect_travel_data
Task 8：实现 generate_itinerary
Task 9：实现确定性 validate_itinerary
Task 10：实现 revise_itinerary 和最大修订次数
Task 11：实现方案保存和版本管理
Task 12：实现 finalize_response
Task 13：接入 ChatService 请求分流
Task 14：增加 planning_stage SSE 事件
Task 15：前端增加规划阶段展示
Task 16：增加 Fake Model、Fake Tool 和图节点测试
Task 17：增加数据库和 SSE 集成测试
Task 18：执行真实端到端验证
Task 19：更新 README、环境配置和架构文档
Task 20：执行全量测试、Lint 和生产构建
```

---

# 十六、完成标准

满足以下条件才算完成：

1. 普通查询继续使用现有 AgentExecutor。
2. 完整行程规划请求进入 LangGraph。
3. 能从自然语言提取结构化旅行需求。
4. 能在多轮对话中补全缺失字段。
5. 缺少必要信息时不会盲目调用工具。
6. 能真实查询交通、酒店、景点、天气和路线。
7. 能生成结构化 `ItineraryPlan`。
8. 每日行程考虑到达、返程和交通耗时。
9. 能检查明显时间、路线、天气和预算冲突。
10. 能自动修订不合理方案。
11. 修订次数存在上限。
12. 能持久化计划和历史版本。
13. 能基于已有方案进行局部修改。
14. 局部修改不会无理由重新查询全部数据。
15. SSE 可以展示规划阶段。
16. 最终回答继续流式输出。
17. 不虚构工具未返回的实时事实。
18. 工具失败可以受控降级。
19. 原有聊天、工具调用、停止生成和历史会话功能不受影响。
20. 后端测试、数据库测试、前端类型检查、Lint 和生产构建全部通过。

---

# 十七、交付说明

完成后请输出：

## 1. 架构说明

说明：

* 普通查询和行程规划如何分流。
* LangGraph 有哪些节点。
* State 如何流转。
* 现有 AgentExecutor 如何复用。

## 2. 文件清单

列出：

* 新增文件
* 修改文件
* 数据库迁移
* 测试文件
* 文档文件

## 3. 数据模型说明

说明：

* `TripRequest`
* `ItineraryPlan`
* `TripPlanningState`
* `ValidationIssue`
* 数据库 JSONB 结构

## 4. 节点实现说明

说明：

* 哪些节点使用 LLM。
* 哪些节点使用确定性逻辑。
* 哪些节点调用工具。
* 哪些查询并发执行。
* 自动修订如何限制循环。

## 5. 测试结果

给出实际执行命令和结果：

* 后端完整测试
* 数据库集成测试
* LangGraph 节点测试
* SSE 测试
* 前端类型检查
* 前端 Lint
* Next.js 生产构建
* `git diff --check`

## 6. 手工验证结果

逐项说明以下场景：

```text
完整行程规划
缺失日期追问
多轮补全需求
修改第二天
更换交通方式
更换酒店
普通单项查询
```

本阶段完成后，先不要继续拆分 Supervisor 或多个子 Agent。先确保结构化行程规划、校验、修改和版本持久化稳定，再评估是否需要将交通、住宿、景点规划拆成独立子图。
