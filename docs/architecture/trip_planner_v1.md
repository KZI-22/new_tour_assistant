# 结构化行程规划工作流 V1

## 请求分流

`ChatService.stream` 在创建模型后先读取当前会话的行程记录，并通过规则优先分类器分流：

```text
用户消息
├─ 普通聊天 / 单项旅游查询 → 现有 AgentExecutor → 工具循环或直接回答
└─ 新建完整行程 / 修改已有方案 → LangGraph Trip Planner
```

分类不确定时回退到现有 `AgentExecutor`，避免误触发成本更高的完整规划。存在未完成的行程草稿时，
下一轮消息强制继续规划链路，以便补齐上一轮已经提取的结构化需求。

## 图节点和 State

图包含九个节点：

1. `understand_request`：LLM 结构化提取 `TripRequest`、地点原文证据和修改影响范围。
2. `check_required_fields`：确定性检查目的地、日期、天数、过去日期和 V1 范围。
3. `ask_clarification`：只追问当前必需信息并保存 version 0 草稿，然后结束本轮图执行。
4. `collect_travel_data`：复用 `ToolExecutor` 的参数校验、超时、脱敏、审计与并发执行。
5. `generate_itinerary`：LLM 基于结构化工具事实输出 `ItineraryPlan`。
6. `validate_itinerary`：确定性校验为主，LLM 体验校验为辅，统一输出 `ValidationIssue`。
7. `revise_itinerary`：仅修复问题范围，最多执行配置的修订次数。
8. `persist_itinerary`：事务内更新当前方案并追加不可变历史版本。
9. `finalize_response`：只把结构化方案渲染为 Markdown，不再改写方案事实。

`TripPlanningState` 分开保存需求、工具事实、当前/上一版方案、校验问题、修订计数和持久化版本，
不依赖自然语言 `messages` 作为唯一状态来源。

结构化 LLM 调用受独立超时保护。模型不支持结构化输出或调用超时时，需求提取和行程生成会切换到
保守的确定性降级逻辑，只使用已验证的工具候选；LLM 体验校验失败只产生 warning，不会丢失已完成
的确定性校验结果。

地点提取采用“LLM 理解、确定性验证”的边界：LLM 必须同时返回规范地点和用户原文中的证据片段；
后端验证证据、地点角色和明显非法任务措辞。需求提取的原生 structured output 不可用时，会继续
尝试严格 JSON 输出并用 Pydantic 校验；全部模型提取方式都失败时，规则降级只补充明确字段，不再从“规划一份去
杭州”等表达中猜测出发地。没有可信出发地时先保存草稿并追问，不调用航班或火车工具。

## 数据采集与事实边界

交通、酒店、第一轮 POI 和天气互不依赖，使用现有 `ToolExecutor.execute_many` 并发执行。POI
取得 GCJ-02 坐标后，才调用距离矩阵和代表性路线。所有事实保留工具名、供应商和查询时间；
FlyAI 返回形状通过保守适配器转换为 `TransportOption`/`HotelOption`，无法核验的字段保持为空。

确定性校验会拒绝工具结果中不存在的班次、酒店价格和 POI ID。部分工具失败时仍可生成区域级
建议，但缺失项进入 warnings，预算汇总不得假装已经包含未知实时费用。

## 方案修改和版本

`travel_plans` 按当前项目已有的 `conversation_id` 归属当前方案，`travel_plan_versions` 保存每次
完整快照。修改请求会提取 `affected_sections`：调整第二天节奏不会重新查询往返交通；酒店区域
变化只刷新酒店及相关路线；交通方式变化刷新交通并重新编排首尾日；日期变化刷新所有时效数据。

当前项目没有用户账户，因此 V1 不引入虚假的 `user_id`。删除会话时，行程和版本通过外键级联
删除。未来引入用户体系后，应增加所有权约束和相应迁移。

## SSE 和前端

图通过 LangGraph custom stream 发送 `planning_stage`，状态为 `running`、`success`、`failed`
或 `skipped`。工具事件继续使用原有 `tool_call`/`tool_result`，最终 Markdown 继续使用
`message_delta`。前端将规划阶段、工具状态和正文分区显示，且不会接收完整 State、Prompt、模型
思维过程或供应商原始响应。
