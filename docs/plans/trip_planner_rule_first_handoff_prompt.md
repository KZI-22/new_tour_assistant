# 新会话 Handoff 提示词

请在 `D:\A_Project\new_tour_assistant` 仓库继续完成 Trip Planner 的“规则优先需求提取”改造。

## 已确认决策

用户已经在上一会话确认采用第二版方案，不需要重新比较两版：

1. 代码先解析明确的城市、天数、日期、交通和酒店信息。
2. 区分“信息缺失”和“存在歧义”：
   - 表达明确但字段缺失：不调用 LLM，直接进入缺失项校验和追问。
   - 存在指代、冲突或多个合理理解：才调用 LLM。
3. LLM 只处理规则无法确定的歧义字段，不生成完整 `TripPlanningRequest`。
4. 代码最终统一组装和裁决，明确日期、天数和能力开关优先于 LLM。

开始前请完整阅读：

- `D:\A_Project\new_tour_assistant\AGENTS.md`
- `D:\A_Project\new_tour_assistant\docs\plans\trip_planner_rule_first_extraction_plan.md`
- `D:\A_Project\new_tour_assistant\test.md`

仓库存在 `.codegraph`，理解或定位代码时先使用 CodeGraph，再按需读取具体文件。先检查 `git status` 和现有实现，不要重复已经完成的顶层路由修复。

## 当前实现背景

- 顶层路由已经改成精确 JSON + Pydantic 校验，非法输出最多重试一次，再降级 `general_agent`。
- 相关提交：`c24be1e fix: retry invalid trip route JSON once`。
- 进入 `trip_planner` 后，当前 `ExtractRequirementsNode` 仍使用 `StructuredOutputService` 让 LLM 生成完整的 `TripPlanningRequest`。
- LLM 提取后，代码通过 `apply_trip_request_overrides` 覆盖明确日期/天数，再由 `ResolveCapabilitiesNode` 和 `ValidateRequirementsNode` 做确定性能力解析及缺失校验。
- 当前完整 Schema LLM 提取实测平均约 35 秒；规则和能力解析本身是毫秒级。

## 实现要求

请按计划文档实现，而不是只写设计说明：

1. 新增规则优先的需求提取服务，返回部分 `TripPlanningRequest`、字段来源、歧义列表、明确缺失项和能力证据。
2. 规则至少覆盖目的地、天数、日期、交通/酒店开关及主要槽位，并支持 `三天`、`3天`、`三日游`、`3日游`。
3. 城市识别复用现有数据或可靠的可维护词典，不要只硬编码测试城市。
4. 交通和酒店的肯定、否定按分句作用域处理。例如“不要飞机，查高铁和酒店”不能把全部能力关闭。
5. 明确但缺失的信息不触发 LLM。
6. 仅真实歧义调用小型 Resolver；输入只包含必要片段和候选值，使用小型 Pydantic Schema。
7. 结构化校验失败最多重试一次；仍失败或无法确定时字段置空并追问，不得回退到完整 Schema LLM 提取。
8. 合并优先级为：本轮明确更正 > 本轮其他明确规则值 > 有效历史明确值 > LLM 歧义解析 > 安全默认值。
9. 接入现有 `ExtractRequirementsNode`，保持 `ResolveCapabilitiesNode`、`ValidateRequirementsNode` 和后续链路协议兼容。
10. 增加结构化日志或指标，至少记录提取路径、规则耗时、LLM 耗时、LLM 调用次数、歧义字段和重试次数。

核心验收输入：

```text
去西安三天旅游攻略，顺便查一下去西安的高铁，以及西安北站附近的酒店。
```

期望为规则路径、0 次 LLM，并解析出：

```json
{
  "destination_city": "西安",
  "duration_days": 3,
  "need_transport": true,
  "transport_mode": "train",
  "transport_scope": "round_trip",
  "transport_origin": null,
  "need_hotel": true,
  "hotel_nearby": "西安北站"
}
```

之后由缺失校验追问出发地和出发日期。

## 测试与验证

优先补充：

- 规则提取器单元测试
- 歧义检测与小型 Resolver 测试
- 分句级交通/酒店肯定和否定测试
- `ExtractRequirementsNode` 集成测试
- Trip Planner 图回归测试
- 核心样例的 LLM 调用次数和耗时断言
- 一组未参与规则开发的盲测用例

使用项目 Conda `py312` 环境运行相关测试。若 pytest 的系统临时目录权限异常，把 `--basetemp` 指向仓库内的临时目录；清理前先确认绝对路径位于仓库内。

上一轮全量 API 测试曾出现两类与本改造无关的环境/时间问题：

- 系统临时目录权限导致约 40 个 error。
- 使用固定日期 `2026-07-25` 的 6 个测试因日期已经过去而失败。

不要把这些基线问题误判为本次回归，也不要未经用户同意顺手修复无关测试；应单独报告。

## 工作区保护与提交

开始时预计可能看到以下已有改动：

- 删除状态的 `1.png`
- 已修改的 `config/models.yaml`
- 未跟踪的 `472aa24a6acf60816863f63eb48894c8.png`
- 未跟踪的 `test.md`

这些属于已有工作或测试报告。不要覆盖、还原或夹带提交；实现前重新以 `git status` 为准。只暂存本次规则优先改造涉及的文件。

遵守 `AGENTS.md`：每个独立修复完成且测试通过后立即创建独立 Git 提交，提交前检查 diff 和暂存范围。最终汇报实现结果、测试证据、基准数据、提交哈希，以及仍存在的风险或未覆盖用例。
