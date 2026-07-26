# 真流式与 Redis 高德缓存执行计划

## 背景

两项独立的性能与体验改动，共享同一次执行但必须分别提交。

1. **真流式**：`AgentExecutor` 当前用 `ainvoke` 拿到完整回复后再切块下发，
   首字节延迟等于全量生成延迟，SSE 基建的收益没有兑现。
2. **高德缓存只在进程内**：`AmapCache` Protocol 早已留好 Redis 接缝，
   但唯一实现是 `InMemoryAmapCache`，且所有 namespace 共用 300 秒 TTL。

## 一、通用 Agent 链路真流式

### 现状

- `ToolEnabledModel` 只声明 `ainvoke`。
- `AgentExecutor.stream` 拿到完整 `AIMessage` 后判断分支；无工具调用时用
  `_split_text` 切 80 字符块下发。
- 带工具调用那一轮的文本被丢弃，用户看不到。

### 目标行为

- `ToolEnabledModel` 增加 `astream`，执行器累加 `AIMessageChunk`，
  文本增量在模型仍在生成时即下发。
- 带工具调用那一轮的文本同样下发（决策 A1），与最终回答一起构成
  持久化正文。这是有意的行为变更。
- 流结束后再根据累加结果判断是否进入工具轮，工具轮语义不变。

### 不做的事

- 不在执行器内做事件合并。合并会牺牲首字节延迟，而重复渲染的成本属于前端；
  若出现渲染卡顿，应在前端做渲染节流，而不是在协议层攒包。
- 不改规划链路。`StandardTripPlanner` 走结构化生成，按天流式属于独立改动。

### 影响面

- `_split_text` 成为死代码，删除。
- 空响应重试判断点从「`ainvoke` 返回后」移到「流结束后」，语义不变。
- 测试中的 fake model 需要提供 `astream`。规划链路的 fake 不经过
  `AgentExecutor`，不受影响。

## 二、RedisAmapCache 与分级 TTL

### 现状

- `AmapCache` Protocol：`get(key)` / `set(key, value)`。
- `InMemoryAmapCache`：固定 300 秒 TTL、512 条 LRU。
- `_cache_key` 产出 `{namespace}:{sha256}`，已可直接作为 Redis key。
- `redis_client` 只在 `auth_enabled` 时创建。

### 目标行为

- 新增 `RedisAmapCache`，实现同一 Protocol，值用 JSON 序列化，
  key 前缀 `amap:cache:`。
- `set` 增加 `ttl_seconds` 参数：策略归调用方（客户端知道 namespace），
  存储归缓存实现。
- 按 namespace 分级 TTL：

  | namespace | TTL |
  | --- | --- |
  | `geocode` / `reverse_geocode` / `coordinate_conversion` | 7 天 |
  | `place_search_v2` | 24 小时 |
  | `travel_time_matrix` | 12 小时 |
  | `route_plan` | 1 小时 |
  | `weather_forecast` | 30 分钟 |
  | `weather_current` | 5 分钟 |

  可通过 `AMAP_CACHE_TTL_OVERRIDES`（JSON 对象字符串）覆盖，
  沿用 `XHS_MCP_STDIO_ARGS` 的既有 JSON 环境变量惯例。
- Redis 客户端创建条件与 `auth_enabled` 解耦：配置了 `REDIS_URL` 即创建。

### 故障语义

与 `RedisOtpChallengeStore` 刻意不同。OTP 是安全关键，Redis 不可用必须抛错；
缓存是性能优化，Redis 不可用必须降级为未命中，不得影响请求成败。
`RedisAmapCache` 捕获 `RedisError` 与反序列化失败，记录 warning 后返回未命中。

### 不做的事

- 不做内存 L1 + Redis L2 两层缓存。本地 Redis 往返相对高德调用可忽略，
  不值得引入一致性心智负担。

## 提交划分

两项改动互不依赖，分别验证、分别提交。
