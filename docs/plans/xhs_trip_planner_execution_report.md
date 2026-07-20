# 小红书旅游攻略链路重构执行报告

## 执行结论

截至 2026-07-20，`xhs_trip_planner_execution_plan.md` 的阶段 0–6 已全部执行完成。
系统现仅保留 `general_agent` 与 `xhs_trip_planner` 两条顶层路径；小红书规划链路已完成二维码
登录、高点赞帖子选择、主辅帖生成契约、来源校验和最终渲染。旧结构化 Trip Planner 的生产代码、
运行时装配和专用测试已删除，历史数据库表、ORM 模型与 Alembic 迁移保持不变。

## 阶段与提交

| 阶段 | 结果 | 提交 |
| --- | --- | --- |
| 0：基线与契约测试 | 增加登录图片与点赞选择契约 | `86a4595` |
| 1：二分类路由 | 收敛为 General Agent / XHS Planner | `e7ae850` |
| 2：MCP 登录闭环 | 完成二维码、轮询、取消与 heartbeat | `5ba5d0a` |
| 3：高点赞帖子选择 | 完成固定搜索、点赞归一化、前五候选与单帖降级 | `068835f` |
| 4：主辅帖生成契约 | 完成角色、Prompt、来源白名单与渲染 | `2ae9bc8` |
| 5：删除旧 Planner | 删除旧图、服务、Schema、装配和专用测试 | `313f8c3` |
| 6：全量验证与报告 | 完成本报告所列最终门禁 | 本报告所在提交 |

## 最终质量门禁

| 检查 | 结果 |
| --- | --- |
| 后端测试 | `139 passed, 11 skipped`，无 `xfail` |
| Ruff | `All checks passed!` |
| 前端类型检查 | `npm run typecheck` 通过 |
| 前端 Lint | `npm run lint` 通过 |
| 前端生产构建 | `npm run build` 通过，静态路由 `/` 与 `/_not-found` 生成成功 |
| Git 差异检查 | `git diff --check` 通过 |
| 旧生产引用扫描 | 未发现旧 Planner 模块 import |
| 敏感日志扫描 | 未发现日志或 `print` 输出 Token、二维码 base64、`xsec_token` 或完整 `login_id` |

11 个跳过项均由真实高德、FlyAI、PostgreSQL 或完整 Tool Calling E2E 的显式环境开关控制。
本次默认验证没有访问真实外部供应商、触发扫码登录或消耗真实模型额度。后端测试存在一条上游
Starlette `TestClient` 弃用警告，不影响本次结果。

前端验证环境为 Node.js `v23.4.0`、npm `10.9.2`。生产构建在受限沙箱内编译成功后，因工作进程
触发 `spawn EPERM`；在获准的沙箱外环境重跑同一命令后完整通过。这是执行环境权限限制，不是
项目构建错误。

## 最终行为

- 搜索词固定为 `{城市} {天数}日游 攻略`，参数固定为 `most_liked/any/any/any/any`。
- 最多选择五篇详情候选，每批并发不超过两篇，取得两篇有效正文后停止。
- `source_1 / primary` 决定主体路线，`source_2 / supplementary` 只补充缺失信息。
- 单帖时明确降级；没有有效正文时不调用生成模型。
- 每个具体活动必须引用本次实际读取的来源，来源元数据由服务端重建。
- 最终回答明确搜索只覆盖首次加载结果，且未查询实时机票、火车票、酒店库存或价格。
- 二维码、MCP Token、Cookie、`xsec_token` 与帖子完整正文不进入普通日志或数据库。
- 普通航班、火车、酒店、高德 POI、天气与路线工具继续由 General Agent 使用。
- `travel_plans` 与 `travel_plan_versions` 历史表及迁移继续保留，但新链路不再写入。

## 工作区保护

实施过程中未暂存或提交用户原有的 `AGENTS.md` 改动和
`docs/plans/xhs_trip_planner_execution_plan.md` 文件。
