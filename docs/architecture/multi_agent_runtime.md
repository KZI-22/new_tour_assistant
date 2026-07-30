# 多 Agent Runtime 实现说明

本实现对应
[`multi_agent_travel_chain_rearchitecture_plan.md`](../plans/multi_agent_travel_chain_rearchitecture_plan.md)，
采用功能开关渐进接管标准会话，显式小红书原帖模式保持独立。

## 运行边界

- 主 Agent 只读取最近会话并输出 `direct` 或一组自然语言专业任务，不绑定业务工具。
- 行程、交通、酒店任务分别创建新的模型实例、消息上下文和 `ToolExecutor`。
- 行程 Agent 只看见 `ai_search`、`search_poi`、`keyword_search`、`amap_get_weather`。
- 交通 Agent 只看见 `search_flight`、`search_train`。
- 酒店 Agent 只看见 `search_hotel`。
- 回答 Agent 不绑定工具，只接收当前请求和结构化任务结果。

子 Agent 的模型消息和工具输出不会传给兄弟 Agent。只有主 Agent 持有的任务快照负责并行、追问、
恢复和最终结果选择。

## 部分完成与恢复

专业 Agent 返回 `success`、`partial`、`needs_input` 或 `failed`，缺失字段使用结构化
`missing_fields`。当任一任务等待输入时：

1. 其他可执行任务继续完成并持久化。
2. 主 Agent 对缺失字段去重后统一追问。
3. 下一条用户消息只恢复 `partial`、`needs_input`、`waiting` 任务。
4. 先前成功的工具 artifact 会从恢复 Runtime 的工具绑定中移除，因此不会重复调用。
5. 所有必要任务结束后才启动回答 Agent。

行程任务由此支持先保存 `ai_search` 攻略草案、后补日期并只执行天气查询。

## 事件与持久化

同一套运行事件同时用于 SSE、消息 `debug_trace_json` 和 `agent_runtime_events`：

- `agent_status`：面向普通用户的简洁 Agent 状态。
- `agent_trace`：脱敏的拆解、Agent 生命周期和工具执行诊断。
- `tool_call` / `tool_result`：具体工具进度。

`tool_call_logs` 通过 `conversation_id`、`assistant_message_id`、`agent_run_id` 和
`agent_task_id` 关联到请求、Runtime 与子任务。FlyAI 额外记录：

- `process_status`：CLI 进程退出层。
- `provider_status`：供应商显式状态。
- `parse_status`：JSON 解析层。
- `business_status`：业务数据可用层。

当 FlyAI 已写出可解析且可用的 JSON、但外层进程返回非零时，结果仍作为业务成功保留，同时把
`process_status=failed` 写入诊断，避免丢弃已返回数据。

Trace 不保存模型内部思维、完整系统提示词、鉴权信息或未脱敏供应商响应。

## 上线顺序

1. 执行 `alembic upgrade head`，确认数据库到 `20260730_0011`。
2. 保持 `MULTI_AGENT_ENABLED=false` 部署并完成健康检查。
3. 在验证环境启用开关，检查 `agent_status`、`agent_trace` 和工具审计关联。
4. 验证组合请求、缺字段追问和补充后恢复，再逐步扩大流量。
5. 回滚只需关闭开关；旧 `general_agent` 和 `trip_planner` 入口暂不删除。
