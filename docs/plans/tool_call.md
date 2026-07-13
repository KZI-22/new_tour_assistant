# 旅游聊天机器人：工具调用闭环改造任务

## 一、项目现状

当前项目已经具备网页版聊天机器人基础能力。

前端：

* Next.js 响应式聊天界面
* SSE 流式回复
* Markdown 渲染
* 停止生成
* 错误提示
* 历史会话加载、继续对话和删除
* 类型检查、Lint、生产构建均已通过

后端：

* FastAPI
* PostgreSQL 会话与消息持久化
* 7 个模型的配置、切换和可用性检查
* 当前 PostgreSQL 容器运行健康

主要入口：

* 前端：`apps/web/components/chat-shell.tsx`
* 后端接口：`apps/api/app/api/routes.py`
* 聊天服务：`apps/api/app/services/chat_service.py`
* 工具注册：`apps/api/app/tools/__init__.py`

当前已经注册 9 个 LangChain `StructuredTool`，并存放在：

```python
app.state.travel_tools
```

工具包括：

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

当前问题是：

`ChatService` 仍然直接调用：

```python
model.astream(...)
```

模型没有通过 `bind_tools()` 绑定工具，也没有工具调用循环，因此用户询问航班、火车、酒店、天气或路线时，模型不会真实执行工具。

---

# 二、本次开发目标

将当前聊天链路：

```text
用户消息
→ ChatService
→ model.astream()
→ 文本回复
```

升级为：

```text
用户消息
→ 模型判断是否调用工具
→ 执行一个或多个工具
→ 将工具结果写回消息上下文
→ 模型继续判断或生成最终回答
→ 通过 SSE 流式返回前端
```

本阶段只实现：

```text
单 Agent + Tool Calling
```

暂时不要实现：

* LangGraph
* 多 Agent
* 航班 Agent、酒店 Agent、路线 Agent 等子 Agent
* 完整复杂行程规划图
* 预算优化 Agent
* 行程审查 Agent

本次目标是先建立稳定、可测试、可扩展的工具执行基础设施。

---

# 三、总体设计要求

## 1. 不要破坏现有功能

以下现有能力必须继续工作：

* 普通聊天
* SSE 流式输出
* 模型切换
* 会话持久化
* 历史消息加载
* 继续对话
* 停止生成
* 错误提示
* Markdown 渲染
* 前后端现有测试和构建

对于不需要工具的问题，例如：

```text
你好
什么是旅游规划
帮我写一句旅行文案
```

模型应直接回答，不调用工具。

---

## 2. 工具从应用状态中获取

不要在 `ChatService` 中重复创建工具。

统一从 FastAPI 应用状态读取：

```python
request.app.state.travel_tools
```

或者根据当前项目依赖注入结构，将工具列表明确传入 `ChatService`。

工具名称必须作为唯一标识，构建：

```python
tool_map = {
    tool.name: tool
    for tool in tools
}
```

---

## 3. 使用 bind_tools

给当前选中的聊天模型绑定全部已注册工具：

```python
tool_enabled_model = model.bind_tools(tools)
```

要兼容当前项目已有的 7 个模型。

如果某个模型不支持 Tool Calling，应：

* 返回明确、脱敏的业务错误
* 不泄露供应商异常栈
* 不影响其他模型使用
* 最好在模型可用性检查中标记其 Tool Calling 支持情况

不要假设所有模型一定以完全相同的格式返回工具调用。

优先使用 LangChain 标准字段：

```python
AIMessage.tool_calls
```

---

# 四、实现工具调用循环

建议将工具执行逻辑从 `ChatService` 主流程中拆出来，形成独立组件，例如：

```text
services/
├── chat_service.py
├── agent_executor.py
└── tool_execution.py
```

具体文件名可以根据当前项目结构调整，但不要把全部逻辑堆进 `routes.py`。

核心循环逻辑：

```text
1. 准备 SystemMessage、历史消息和当前用户消息
2. 调用绑定工具后的模型
3. 获取模型返回的 AIMessage
4. 如果不存在 tool_calls：
   - 将该消息作为最终回复
   - 结束循环
5. 如果存在 tool_calls：
   - 将 AIMessage 加入消息上下文
   - 执行工具
   - 为每次调用构造 ToolMessage
   - 将 ToolMessage 加入消息上下文
6. 再次调用模型
7. 直到模型返回无 tool_calls 的最终回答
```

必须设置最大工具轮数，例如：

```python
MAX_TOOL_ROUNDS = 5
```

超过后停止执行，并返回可理解的错误信息，防止：

* 模型无限循环
* 重复调用相同工具
* 消耗失控
* 请求长期不结束

---

# 五、支持单工具、串行调用和并行调用

## 1. 单工具调用

例如：

```text
用户：查询 2026 年 7 月 20 日上海到北京的航班
```

预期调用：

```text
search_flight
```

---

## 2. 串行依赖调用

例如：

```text
用户：今天天气怎么样？
```

当用户没有明确地点时，可以：

```text
amap_get_current_city
→ amap_get_weather
```

第二个工具依赖第一个工具的结果，因此应通过下一轮模型判断继续调用，而不是强行在代码中写死。

再例如：

```text
用户：从南京南站怎么去总统府？
```

可能执行：

```text
amap_search_places：南京南站
→ amap_search_places：总统府
→ amap_plan_route
```

---

## 3. 同一轮多个独立工具调用

例如：

```text
用户：比较明天上海到北京的飞机和高铁
```

模型可能在同一轮请求：

```text
search_flight
search_train
```

对于同一轮中互不依赖的多个 `tool_calls`，应支持并行执行。

优先使用异步方式，例如：

```python
asyncio.gather(...)
```

但需要保证：

* 每个工具结果与自己的 `tool_call_id` 正确对应
* 一个工具失败不能导致其他成功结果丢失
* 返回的 `ToolMessage` 顺序稳定
* 异常被单独捕获并转换成统一结果

如果某些工具底层是同步阻塞调用，不要直接阻塞事件循环，应根据现有实现使用线程池或工具本身的异步接口。

---

# 六、工具执行安全要求

执行工具前必须校验：

* 工具名称是否存在
* 参数是否为合法字典
* 参数是否符合工具 Schema
* `tool_call_id` 是否存在
* 是否超过最大调用轮数

对于不存在的工具，不要直接抛出未处理异常。

应生成失败的 `ToolMessage`，让模型知道工具执行失败，例如：

```json
{
  "success": false,
  "tool_name": "unknown_tool",
  "error": {
    "code": "TOOL_NOT_FOUND",
    "message": "请求的工具不存在或当前不可用"
  }
}
```

禁止向模型或前端暴露：

* API Key
* 环境变量
* 完整供应商请求
* 完整异常堆栈
* 本机路径
* 内部代理配置
* 原始敏感响应
* 数据库连接信息

---

# 七、统一 ToolResult 协议

不要将 FlyAI 和高德的原始返回值无处理地直接交给模型。

建立统一的内部工具结果结构。

可以使用 Pydantic，例如：

```python
class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ToolResult(BaseModel):
    success: bool
    tool_name: str
    data: Any | None = None
    error: ToolError | None = None
    metadata: dict[str, Any] = {}
```

建议至少包含：

```json
{
  "success": true,
  "tool_name": "search_flight",
  "data": {},
  "error": null,
  "metadata": {
    "provider": "FlyAI",
    "duration_ms": 1250,
    "queried_at": "ISO 8601 时间"
  }
}
```

失败时：

```json
{
  "success": false,
  "tool_name": "search_flight",
  "data": null,
  "error": {
    "code": "PROVIDER_TIMEOUT",
    "message": "航班服务暂时响应超时",
    "retryable": true
  },
  "metadata": {
    "provider": "FlyAI",
    "duration_ms": 10000
  }
}
```

如果当前各工具已经返回了统一或半统一的数据结构，应在现有结构基础上适配，不要进行不必要的大规模重构。

---

# 八、ToolMessage 构造

每个工具执行完成后，应构造 LangChain 标准 `ToolMessage`。

示意：

```python
ToolMessage(
    content=serialized_tool_result,
    tool_call_id=tool_call["id"],
    name=tool_call["name"],
)
```

要求：

* `tool_call_id` 必须与模型请求一致
* `content` 应为可序列化字符串
* 优先使用 JSON
* 中文内容正常保留
* 不能将 Python 对象的 `repr()` 直接作为协议
* 处理日期、Decimal、枚举等非 JSON 原生类型

建议提供统一的安全 JSON 序列化函数。

---

# 九、参数缺失与追问原则

本次不要专门开发复杂的 Slot Filling 状态机，但需要通过 System Prompt 明确告诉模型：

对于不可可靠推测的必填参数，必须先询问用户，不能自行编造后调用工具。

典型必填参数：

* 航班：出发地、目的地、出发日期
* 火车：出发地、目的地、出发日期
* 酒店：城市、入住日期、退房日期
* 路线：起点、终点
* 天气：城市；但用户只问“今天天气”时可以先尝试 IP 定位

可以从上下文推断：

* 用户上一轮已提供的城市
* 用户上一轮已提供的日期
* 当前日期和时间
* “今天”“明天”“后天”“下周末”等相对日期
* 当前时区
* 当前城市的 IP 推测结果

不得可靠推测：

* 用户具体出发日期
* 用户预算
* 用户酒店入住和离店日期
* 用户人数
* 用户真实精确位置
* 用户偏好的交通方式

IP 定位结果只能表述为推测，例如：

```text
根据当前网络位置推测你位于南京……
```

不能表述为绝对位置。

---

# 十、更新 Agent System Prompt

为工具调用增加明确的系统提示，但不要覆盖现有合理提示词。

系统提示至少应包含以下规则：

```text
你是旅游规划助手。

当问题涉及实时或外部数据时，必须优先调用工具，而不是依赖模型记忆，包括：
- 航班
- 火车
- 酒店
- 景点和 POI
- 天气
- 路线
- 距离和预计耗时
- 当前城市

当工具缺少必要参数时，先向用户追问，不要编造参数。

工具执行失败时：
- 如实说明失败
- 可以基于已成功的工具结果继续回答
- 不得伪造查询结果
- 不得声称已经查到实际数据

对于工具返回的数据：
- 清楚区分实时查询结果和一般建议
- 价格、余票、天气和耗时可能变化
- 不要向用户展示内部错误、API Key、供应商原始响应或技术堆栈
```

不要强制所有问题都调用工具。

---

# 十一、SSE 协议扩展

保留当前文本流式输出能力，同时增加工具状态事件。

建议支持以下 SSE 事件类型：

```text
message_start
tool_call
tool_result
message_delta
message_end
error
```

具体命名可以兼容当前已有协议，但含义要清晰。

## tool_call 事件

示例：

```json
{
  "type": "tool_call",
  "tool_call_id": "call_xxx",
  "tool_name": "search_flight",
  "display_name": "正在查询航班",
  "arguments": {
    "origin": "上海",
    "destination": "北京",
    "date": "2026-07-20"
  }
}
```

发送到前端的参数必须经过脱敏。

## tool_result 事件

示例：

```json
{
  "type": "tool_result",
  "tool_call_id": "call_xxx",
  "tool_name": "search_flight",
  "success": true,
  "summary": "查询到 18 个航班",
  "duration_ms": 1280
}
```

失败示例：

```json
{
  "type": "tool_result",
  "tool_call_id": "call_xxx",
  "tool_name": "search_flight",
  "success": false,
  "summary": "航班服务暂时不可用",
  "error_code": "PROVIDER_TIMEOUT"
}
```

不要通过 SSE 返回完整工具原始结果。

完整结果只需要写入 Agent 上下文，供模型生成最终回答。

---

# 十二、前端展示

在 `chat-shell.tsx` 现有聊天界面中，增加简单的工具调用状态展示。

第一阶段不需要开发复杂卡片，只需显示类似：

```text
正在查询航班……
已查询到航班信息
```

或者：

```text
✓ 查询航班
✓ 查询火车
```

要求：

* 不影响 Markdown 正文显示
* 工具执行状态和最终答案属于同一次助手回复
* 刷新历史会话时不能导致页面报错
* 停止生成时，前端能正确终止当前 SSE
* 同一轮多个工具可以分别显示状态
* 工具失败时显示友好提示，但最终是否继续回答由后端模型决定
* 不显示原始工具参数中的敏感信息

如果当前数据库只保存用户消息和助手最终消息，本次可以先不持久化前端工具状态，但代码结构应便于后续扩展。

---

# 十三、工具调用日志

增加工具调用日志记录。

优先新增数据库表；如果当前迁移体系暂不方便，也可以先建立仓库层和模型，但最终应完成数据库迁移。

建议字段：

```text
id
session_id
assistant_message_id
tool_call_id
tool_name
provider
arguments_json
status
result_summary
error_code
duration_ms
created_at
```

其中：

```text
status:
- pending
- success
- failed
```

要求：

* 调用开始时记录 pending，或者在结束后一次性记录完整结果
* 参数入库前脱敏
* 不保存 API Key
* 不保存完整供应商原始响应
* 不保存异常堆栈
* 结果只保存摘要
* 单个工具失败也必须有日志
* 日志写入失败不应导致整个聊天请求失败，但需要服务端记录警告

如果现有数据库设计不适合 `assistant_message_id`，可先使用 `session_id + request_id` 关联。

---

# 十四、流式输出策略

工具调用阶段模型通常先返回包含 `tool_calls` 的 `AIMessage`，最终文本需要在工具执行完成后再次生成。

建议流程：

```text
第一阶段：
调用模型，判断工具调用
不向用户输出模型内部工具参数文本

第二阶段：
执行工具，并通过 SSE 输出 tool_call、tool_result 状态

第三阶段：
模型基于 ToolMessage 生成最终回答
最终回答使用现有 message_delta 方式逐 Token 输出
```

如果当前 LangChain 模型在流式模式下难以稳定聚合 `tool_calls`，可以采用：

```text
工具决策轮：ainvoke()
最终回答轮：astream()
```

这是可以接受的。

不要为了所有轮次都强行流式，而导致工具调用信息无法正确聚合。

重点是：

* 最终自然语言回复保持流式
* 工具调用可靠
* `tool_call_id` 完整
* SSE 事件顺序正确

---

# 十五、停止生成和取消处理

现有前端支持停止生成。

接入工具后，需要检查取消链路：

```text
浏览器取消 SSE
→ FastAPI 请求取消
→ 当前模型流停止
→ 尽可能取消尚未完成的工具任务
```

至少保证：

* 停止后不会继续向已关闭连接写 SSE
* 不产生未处理的 `CancelledError`
* 不将取消误记为普通供应商失败
* 已经开始但无法取消的外部请求完成后，不再继续下一轮模型调用
* 数据库中的最终助手消息不会被写成完整成功回答

如果项目已有部分消息保存机制，需要兼容中断状态。

---

# 十六、错误处理

统一处理以下错误：

* 模型不支持 Tool Calling
* 模型返回非法工具名
* 工具参数校验失败
* FlyAI 超时
* 高德超时
* 供应商限流
* 工具返回空数据
* 单个并行工具失败
* 最大工具轮数超限
* ToolMessage 序列化失败
* SSE 客户端断开
* 数据库日志写入失败

面向用户的错误应简洁，例如：

```text
航班查询服务暂时不可用，请稍后重试。
```

不要返回：

```text
Traceback
subprocess command
.env
API Key
完整 JSON 响应
供应商内部 URL
本机绝对路径
```

---

# 十七、测试要求

## 1. 工具执行器单元测试

使用 Fake Model 或 Stub Model，不要依赖真实模型的随机输出。

至少覆盖：

### 普通对话不调用工具

输入：

```text
你好
```

预期：

* 不执行任何工具
* 正常返回最终文本

### 单工具调用

模型返回：

```text
search_flight
```

预期：

* 工具执行一次
* 生成对应 `ToolMessage`
* 第二轮模型生成最终回答

### 串行工具调用

第一轮：

```text
amap_get_current_city
```

第二轮：

```text
amap_get_weather
```

第三轮：

```text
最终文本
```

预期工具和消息顺序正确。

### 并行工具调用

同一轮：

```text
search_flight
search_train
```

预期：

* 两个工具都执行
* 其中一个失败时另一个仍保留
* 两个 ToolMessage 的 `tool_call_id` 正确

### 非法工具名

预期：

* 不抛未处理异常
* 创建失败 ToolResult
* 模型可以基于失败信息继续回答

### 工具参数错误

预期：

* 返回参数校验失败
* 不调用真实供应商
* 不泄露 Pydantic 内部复杂异常

### 最大轮数

模型持续调用工具。

预期：

* 达到上限后停止
* 返回 `TOOL_LOOP_LIMIT`
* 不无限执行

### 工具超时

预期：

* 转换为统一错误
* SSE 返回失败状态
* 最终模型如可能继续生成解释

---

## 2. SSE 接口测试

至少验证事件顺序：

```text
message_start
tool_call
tool_result
message_delta
message_end
```

并测试：

* 普通聊天没有 tool_call 事件
* 工具失败仍有 message_end 或受控 error
* 多工具存在多个 tool_call 和 tool_result
* SSE JSON 格式合法
* 中途取消不会抛出未处理异常

---

## 3. 端到端场景

在本地配置 FlyAI 和高德的环境下，验证以下场景。

### 场景一：航班

```text
帮我查 2026 年 7 月 20 日上海到北京的航班
```

预期真实调用：

```text
search_flight
```

### 场景二：缺少日期

```text
帮我查上海到北京的航班
```

预期：

* 不立即调用 `search_flight`
* 先询问出发日期

### 场景三：天气

```text
今天天气怎么样？
```

预期可能调用：

```text
amap_get_current_city
amap_get_weather
```

最终回答明确说明 IP 定位属于推测。

### 场景四：路线

```text
从南京南站怎么去总统府？
```

预期调用地点搜索和路线规划相关工具。

### 场景五：飞机与高铁对比

```text
比较 2026 年 7 月 20 日上海到北京的飞机和高铁
```

预期调用：

```text
search_flight
search_train
```

并基于实际结果比较。

### 场景六：普通聊天

```text
给我一句关于旅行的文案
```

预期不调用工具。

---

# 十八、代码质量要求

完成后运行并确保通过：

后端：

```text
格式检查
Lint
类型检查
单元测试
集成测试
```

前端：

```text
类型检查
Lint
生产构建
```

不要通过：

* 删除测试
* 跳过失败测试
* 大量使用 `Any`
* 静默吞掉所有异常
* 硬编码测试结果
* 在业务代码中判断固定用户问题
* 将航班、天气等结果写死

来让测试通过。

新增代码应：

* 有清晰类型标注
* 职责拆分合理
* 避免超长函数
* 使用现有项目日志系统
* 遵循现有代码风格
* 复用现有超时、重试、脱敏能力

---

# 十九、建议实施顺序

按照以下顺序推进：

```text
Task 1：检查现有 ChatService、SSE 和消息持久化链路
Task 2：建立 ToolResult 统一协议
Task 3：实现工具名称映射和单工具执行
Task 4：实现 bind_tools 和基础工具循环
Task 5：加入最大工具轮数
Task 6：支持同轮多个工具并行执行
Task 7：扩展 SSE tool_call 和 tool_result 事件
Task 8：前端展示简单工具状态
Task 9：增加工具调用日志和数据库迁移
Task 10：增加 Fake Model 单元测试
Task 11：增加 SSE 集成测试
Task 12：完成 6 个端到端场景验证
Task 13：执行全量构建和测试
```

---

# 二十、完成标准

只有满足以下条件才算任务完成：

1. 用户查询实时航班时，后端真实执行 `search_flight`。
2. 用户查询火车时，真实执行 `search_train`。
3. 用户查询酒店时，真实执行 `search_hotel`。
4. 用户查询天气时，真实执行高德天气工具。
5. 用户查询路线时，真实执行高德地点和路线工具。
6. 不需要工具的问题不会调用工具。
7. 缺少必要参数时，模型先追问，不自行编造。
8. 支持一轮多个工具调用。
9. 支持多轮连续工具调用。
10. 工具调用循环有最大轮数限制。
11. 单个工具失败不会导致整个进程崩溃。
12. 最终回答仍通过 SSE 流式输出。
13. 前端可以展示工具执行状态。
14. 工具调用有可排查的持久化日志。
15. 不向前端、模型或数据库泄露敏感信息。
16. 现有聊天、历史会话、模型切换、停止生成等功能未被破坏。
17. 前后端测试、Lint、类型检查和生产构建全部通过。

---

# 二十一、交付说明

完成后请输出：

## 1. 改动摘要

说明：

* 新增了哪些模块
* 修改了哪些现有模块
* 工具调用主链路如何工作

## 2. 文件清单

列出：

* 新增文件
* 修改文件
* 数据库迁移文件
* 测试文件

## 3. 关键设计说明

重点说明：

* 为什么工具决策轮使用 `ainvoke()` 或 `astream()`
* 多工具如何并发
* 工具错误如何转换
* ToolMessage 如何关联 `tool_call_id`
* SSE 事件如何组织
* 取消请求如何处理

## 4. 测试结果

给出实际执行的命令及结果，包括：

* 后端测试
* 前端类型检查
* 前端 Lint
* 前端生产构建

## 5. 手工验证结果

分别说明以下问题实际调用了哪些工具：

```text
查航班
查火车
查天气
查路线
飞机和高铁对比
普通聊天
```

本阶段完成后不要继续实现 LangGraph。先保证这套单 Agent 工具调用闭环稳定，再进入下一阶段的行程规划工作流编排。
