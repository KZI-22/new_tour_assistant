# 行程规划可靠性修复实施记录

- 实施日期：2026-07-18
- 对应计划：`docs/plans/trip_planner_reliability_fix.md`
- 数据库迁移：`20260714_0003 -> 20260718_0004`，已在本地 PostgreSQL 成功执行
- 工作区说明：实施前已有未提交改动；本次没有 reset、checkout 或覆盖用户改动

## 已完成

1. 统一日期语义为首尾日期均包含，显式“玩 N 天”确定性计算结束日期；冲突日期组合不再静默通过。
2. 增加真实形状的 FlyAI 航班/火车契约 fixture，并实现 `itemList -> journeys -> segments` 专用适配。
3. 区分供应商调用结果与标准化数据状态，记录候选数、可用数、拒绝数和 schema 版本。
4. SSE 工具结果新增可选质量字段；前端显示 usable、partial、empty、invalid，并可从历史对话恢复工具卡片。
5. 未通过正式校验的结构化行程保存为 version 0 草稿，后续可重载；失败修订不会覆盖已有正式版本。
6. POI 保留城市、行政区、类型码和查询来源，按城市与旅行意图过滤明显低相关商业地点。
7. 最多对配置数量的无坐标酒店执行高德精确匹配；只有城市、名称及必要地址校验通过才保存坐标和置信度。

## 实际验证结果

| 验证项 | 命令/结果 |
| --- | --- |
| 后端全量离线测试 | `python -m pytest -m "not database and not flyai and not amap and not e2e"`：139 passed，13 deselected |
| PostgreSQL 集成测试 | `$env:RUN_DATABASE_TESTS='1'; python -m pytest -m database`：1 passed，150 deselected |
| Ruff | `python -m ruff check .`：通过 |
| 新增文件格式 | `python -m ruff format --check apps/api/alembic/versions/20260718_0004_add_tool_data_quality.py apps/api/app/services/flyai_transport_adapter.py apps/api/tests/test_trip_planner_reliability.py`：3 files already formatted |
| 前端类型检查 | `npm run typecheck`：通过 |
| 前端 Lint | `npm run lint`：通过 |
| 前端生产构建 | `npm run build`：通过，3/3 静态页面生成成功 |
| Alembic 迁移链 | `python -m alembic heads`：`20260718_0004 (head)` |
| Diff 检查 | `git diff --check`：通过 |

测试仅出现已有的 Starlette/httpx 弃用警告，不影响本次结果。

## 未执行的外部验证

- 真实 FlyAI marker 测试：未执行；会消耗真实供应商额度，当前没有单独授权。
- 真实 Amap marker 测试：未执行；会消耗真实供应商额度，当前没有单独授权。
- 真实模型端到端事故场景：未执行；会调用模型和旅行供应商，当前没有单独授权。

以上项目明确标记为“未验证”，不计入通过项。离线 fixture 已覆盖事故中的日期、FlyAI 层级、价格、分钟耗时、中转聚合、低相关 POI 和酒店坐标调用上限。

## 兼容性、监控与回滚

- 新 SSE、会话详情和数据库质量字段均为可选/可空，旧记录仍可读取。
- 建议监控 `PROVIDER_SCHEMA_MISMATCH`、`NO_RESULTS`、候选/标准化/拒绝计数、去返程覆盖率和 partial plan 比例。
- 回滚应用代码时，可保留新增可空列；若必须回滚数据库，应先回滚应用，再执行 Alembic downgrade 到 `20260714_0003`。
- 酒店坐标补全上限由 `TRIP_PLANNER_MAX_HOTEL_GEOCODES` 控制，默认 3。
