# Trip Planner 可选能力实施报告

实施日期：2026-07-23

对应计划：`docs/plans/trip_planner_optional_capabilities_execution_plan.md`

## 1. 实施结论

Task 0～Task 9 已按顺序执行。标准多日规划已从旧的单体地图规划器切换到
`TripPlannerGraph`，并形成以下执行边界：

- 地图与天气在统一必填字段完整后固定执行；
- 城际交通和酒店只有在用户明确要求查询、查找、推荐或比较实时选项时执行；
- 普通聊天及单项航班、火车、酒店查询继续进入 General Agent；
- 显式小红书原帖检索继续走既有 XHS 链路；
- FlyAI 第一版只保存和传递 Opaque Evidence，不解析或声明完成内部字段级校验；
- 可选能力失败或空结果不会伪造结果，也不会阻断可用的地图与天气行程；
- 确定性校验保留一次修订机会，第二次失败进入受控错误；
- 产品输出未增加 FlyAI 字段可靠性边界提示；
- 确定性校验失败会记录稳定错误码、问题路径、修订次数及逐问题安全详情，
  未记录完整模型输出、完整 Evidence、完整用户消息或供应商原始错误。

本次没有新增 Agent，也没有修改 General Agent 的 Prompt、执行器或小红书规划链路。
前端可以直接兼容：后端只复用了现有 SSE 事件类型、阶段名和 Trace 步骤，因此没有
新增前端改动。

## 2. Task 与提交

| Task | 结果 | Git 提交 |
| --- | --- | --- |
| Task 0 | 建立后端离线基线，无代码改动 | 无提交 |
| Task 1 | 新增能力、证据和校验契约 | `3fd18f8 Add trip planner capability contracts` |
| Task 2 | 实现显式能力解析、推导和统一缺失项 | `01dd764 Add optional trip capability resolution` |
| Task 3 | 新增并行收集、Join、单次修订的 LangGraph 骨架 | `73b9379 Add trip planner graph orchestration` |
| Task 4 | 将地图与天气收集抽为固定核心能力 | `d47df6b Move map planning into trip planner graph` |
| Task 5 | 新增按需交通和酒店节点及 Opaque Evidence | `f4bc12c Add optional transport and hotel graph nodes` |
| Task 6 | 汇合证据，统一生成、降级和渲染 | `0a250b5 Join optional evidence into trip itineraries` |
| Task 7 | 新增确定性校验错误码和安全结构化日志 | `c6702a8 Add deterministic trip validation diagnostics` |
| Task 8 | 接入 ChatService、SSE 和应用依赖装配 | `3973716 Integrate trip planner graph into chat streaming` |
| Task 9 | 全量验证与本报告 | 本报告提交 |

每个代码 Task 均在定向验证完成后立即独立提交，未把计划文档或其他用户改动混入
代码提交。

## 3. 核心实现

### 3.1 能力解析与执行

- `CapabilityPlan.map_weather_enabled` 固定为 `true`；
- Resolver 只接受当前轮或最近对话中的明确交通/酒店查询指令；
- “住在某区域”“准备坐飞机”“酒店已经订好”等背景或关闭表达不会启用查询；
- 缺失字段由统一 `RequirementCheck` 汇总，一次追问，并在追问前不调用任何供应商；
- 地图天气、交通和酒店分支并行执行，Join 只执行一次；
- 未启用的交通和酒店分支返回 `skipped` Evidence，不调用 FlyAI。

### 3.2 Evidence 与生成

- 地图与天气继续使用既有强类型证据；
- FlyAI `data` 以任意合法嵌套 JSON 的 Opaque Evidence 保存；
- 只有“能力已启用且 Evidence 为 `usable`”时，原始 Opaque `data` 才进入最终模型；
- `failed`、`empty`、`skipped` 状态由后端确定性渲染，不允许模型生成具体班次或酒店；
- 交通或酒店失败时，整体状态降级为 `partial`，地图天气方案继续生成；
- 地图核心失败时整体进入受控失败。

### 3.3 确定性校验与日志

已实现并测试以下稳定错误码：

- `DAY_INDEX_MISMATCH`
- `DAY_DATE_MISMATCH`
- `MAP_REFERENCE_ORDER_MISMATCH`
- `MAP_REFERENCE_UNKNOWN`
- `DUPLICATE_POI`
- `ROUTE_ENDPOINT_MISMATCH`
- `WEATHER_DATE_MISMATCH`
- `WEATHER_FACT_WITHOUT_EVIDENCE`
- `TRANSPORT_OUTPUT_WHILE_DISABLED`
- `TRANSPORT_FACT_WITHOUT_USABLE_EVIDENCE`
- `HOTEL_OUTPUT_WHILE_DISABLED`
- `HOTEL_FACT_WITHOUT_USABLE_EVIDENCE`
- `BOOKING_CLAIM_FORBIDDEN`

所有图节点记录 `started`、`completed`、`failed` 或 `cancelled` 事件以及安全指标。
校验失败记录汇总和逐问题日志；未知引用只记录 SHA-256 短指纹，避免把模型生成的
潜在敏感字符串直接写入日志。非预期节点异常保留服务端堆栈，但异常正文会被替换为
固定脱敏文本。

## 4. 验证记录

### 4.1 基线

| 命令/范围 | 退出码 | 结果 |
| --- | ---: | --- |
| Task 0 后端离线基线 | 0 | `206 passed, 14 deselected` |

首次在文件系统沙箱内运行时，pytest 临时目录 ACL 导致 40 个 setup error。改为在
仓库内创建隔离临时目录并以已批准权限运行后，上述基线通过；这不是代码失败。

### 4.2 各 Task 定向验证

| Task | 退出码 | 结果 |
| --- | ---: | --- |
| Task 1 Schema | 0 | 7 passed |
| Task 2 Resolver | 0 | 21 passed |
| Task 3 Graph | 0 | 9 passed |
| Task 4 地图天气与旧规划器回归 | 0 | 38 passed |
| Task 5 可选节点与 Graph | 0 | 24 passed |
| Task 5 既有 FlyAI Client/Schema | 0 | 17 passed |
| Task 6 Evidence Join、生成与渲染 | 0 | 19 passed |
| Task 7 校验、日志、图修订与降级 | 0 | 50 passed |
| Task 8 API SSE、路由和旁路扩大回归 | 0 | 101 passed |

各 Task 涉及文件的 Ruff check 和 format check 均通过。

### 4.3 Task 9 计划命令

后端：

| 命令 | 退出码 | 结果 |
| --- | ---: | --- |
| `python -m pytest -m "not database and not redis and not flyai and not amap and not e2e"` | 0 | `284 passed, 14 deselected` |
| `python -m pytest -m database` | 0 | `3 skipped, 295 deselected`；本机未配置数据库测试服务 |
| `python -m pytest -m redis` | 0 | `2 skipped, 296 deselected`；本机未配置 Redis 测试服务 |
| `python -m ruff check .` | 0 | 通过 |
| `python -m ruff format --check .` | 1 | 发现 7 个本次任务前已存在且未被本次改动触及的格式基线文件 |
| `git diff --check` | 0 | 通过 |

`ruff format --check .` 报告的既有文件：

- `apps/api/app/core/security.py`
- `apps/api/app/core/settings.py`
- `apps/api/app/db/models.py`
- `apps/api/app/services/attraction_planning_service.py`
- `apps/api/app/services/map_trip_collection_service.py`
- `apps/api/tests/test_attraction_planning_service.py`
- `apps/api/tests/test_auth_redis.py`

为遵守“不重新扩展范围”和“不夹带无关改动”，本次未机械格式化这些未修改文件。

前端：

| 命令 | 退出码 | 结果 |
| --- | ---: | --- |
| `npm run typecheck` | 0 | 通过 |
| `npm run lint` | 0 | 通过 |
| `npm run build`（沙箱内首次） | 1 | 编译成功，创建构建子进程时 `spawn EPERM` |
| `npm run build`（解除沙箱限制重跑） | 0 | 生产构建和静态页面生成通过 |

Next.js 构建生成的 `next-env.d.ts` 临时改写已恢复，未提交构建产物。

### 4.4 未执行的真实供应商测试

以下测试未执行，不能视为通过：

- `python -m pytest -m flyai`
- `python -m pytest -m amap`
- `python -m pytest -m e2e`

原因：本次没有获得明确的真实供应商额度消耗授权。实现使用 Fake Client 覆盖了
FlyAI 成功、空结果、超时、鉴权错误、CLI 不存在和任意嵌套 Opaque JSON；高德固定
分支继续由现有离线与回归测试覆盖。

## 5. 已知未验证项与风险

- 真实 FlyAI/Amap 的当前供应商响应、额度和网络行为未在本次执行中验证；
- 数据库与 Redis 标记测试因本机服务未配置而跳过；
- FlyAI 第一版按约定不做字段级语义校验，模型只可整理 Opaque Evidence 中可见内容；
- 全仓仍有 7 个与本次范围无关的 Ruff 格式基线文件，代码级 `ruff check` 已通过；
- 旧 `MapTripPlanner` 暂时保留供兼容测试和内部复用验证，但 ChatService 的标准多日
  路由已经只使用 `StandardTripPlanner` 和 `TripPlannerGraph`。

## 6. 回滚

这些提交按 Task 独立，可从最新提交开始逆序回滚：

1. 回滚 `3973716`：恢复 ChatService 使用旧标准地图规划器；
2. 回滚 `c6702a8`：移除统一校验诊断和节点结构化日志；
3. 回滚 `0a250b5`：移除统一 Evidence Join、生成与渲染；
4. 回滚 `f4bc12c`：移除交通与酒店可选节点；
5. 回滚 `d47df6b`、`73b9379`：移除固定地图天气节点和图骨架；
6. 回滚 `01dd764`、`3fd18f8`：移除能力解析及新增契约。

回滚时应使用普通 `git revert` 保留历史，不应使用破坏性重置。
