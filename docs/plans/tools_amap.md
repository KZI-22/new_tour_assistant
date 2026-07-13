# 高德地图工具接入计划

## 一、当前项目状态

项目当前已经具备：

* 网页聊天机器人前端。
* FastAPI 后端。
* `FlyAIClient`。
* 航班、火车、酒店、POI 四个 FlyAI 结构化 LangChain Tool。
* FastAPI 启动时将 FlyAI Tool 注册到 `app.state.travel_tools`。
* `.env` 可确保通过 Node.js 22 调用 FlyAI。
* FlyAI 航班真实查询已经验证成功。

项目当前尚未实现：

* LangGraph 工作流。
* Multi-Agent 编排。
* 自动行程规划。
* FlyAI 与高德工具的自动串联。
* 多工具连续调用。
* 景点自动分组和排序。
* 根据天气自动调整行程。
* 完整的 Tool Calling Agent。

本次任务只完成高德地图工具层建设，不提前实现 Agent 编排。

---

## 二、本次开发目标

接入高德地图 Web Service API，为后续旅游 Agent 提供地理空间和市内出行能力。

本次需要完成：

1. 高德配置管理。
2. 统一的 `AmapClient`。
3. 高德结构化数据模型。
4. 五个高德 LangChain Tool。
5. 客户端 IP 获取能力。
6. 当前日期、时间和时区上下文。
7. 工具注册。
8. 单元测试。
9. 可选真实高德 API 集成测试。
10. 保证现有 FlyAI 功能不受影响。

本次不实现：

* LangGraph。
* Multi-Agent。
* 自动旅游行程生成。
* 自动调用多个 FlyAI 和高德工具。
* FlyAI POI 与高德 POI 的自动匹配工作流。
* 景点距离矩阵驱动的自动排序算法。
* 天气驱动的自动行程调整。
* 完整路线生成工作流。
* 前端地图展示。
* 浏览器 GPS 定位。
* 用户长期位置记忆。
* 节假日和调休查询。
* Redis 缓存正式接入。

---

## 三、FlyAI 与高德的职责划分

### FlyAI 负责

* 航班查询。
* 火车查询。
* 酒店查询。
* 景点、餐厅等旅游 POI 候选查询。

### 高德地图负责

* 根据 IP 推测用户所在城市。
* 地点和 POI 搜索。
* 地点标准化。
* 经纬度获取。
* 市内路线规划。
* 多地点之间的距离和预计耗时计算。
* 当前天气和天气预报查询。

整体定位：

> FlyAI 提供旅游资源，高德提供地理位置、市内交通和天气信息。

本阶段只准备这些工具，不实现二者之间的自动编排。

---

## 四、高德配置

在现有配置系统中增加高德相关环境变量：

```env
AMAP_API_KEY=
AMAP_BASE_URL=https://restapi.amap.com
AMAP_TIMEOUT_SECONDS=15
AMAP_MAX_RETRIES=1
```

要求：

* 不要将 API Key 硬编码到代码。
* `.env.example` 中只保留空值示例。
* API Key 不得出现在日志中。
* API Key 不得出现在异常信息中。
* 保持现有配置加载方式，不要额外引入另一套配置框架。

---

## 五、创建统一的 `AmapClient`

参考现有 `FlyAIClient` 的项目结构和代码风格，创建统一高德客户端。

建议职责：

* 组装高德请求参数。
* 发送 HTTP 请求。
* 设置超时。
* 处理网络异常。
* 处理 HTTP 异常。
* 校验高德业务状态码。
* 解析高德返回数据。
* 统一异常类型。
* 记录请求耗时。
* 对敏感参数脱敏。
* 为后续缓存预留接口。

建议至少提供以下客户端方法：

```text
ip_location
search_places
plan_route
travel_time_matrix
get_weather
geocode
reverse_geocode
convert_coordinates
```

其中部分方法作为内部能力，不全部暴露为 LangChain Tool。

---

## 六、统一异常处理

建议定义高德相关异常，例如：

```text
AmapError
AmapConfigurationError
AmapRequestError
AmapTimeoutError
AmapRateLimitError
AmapInvalidParameterError
AmapEmptyResultError
```

高德接口返回：

```json
{
  "status": "0",
  "info": "...",
  "infocode": "..."
}
```

即使 HTTP 状态码是 200，也应按照高德的：

* `status`
* `info`
* `infocode`

判断业务是否成功。

错误输出要求：

* 能区分配置错误、超时、限流、非法参数和空结果。
* 不向用户暴露 API Key。
* 不直接返回完整原始响应。
* 日志可记录 `infocode`，但不能记录敏感参数。

---

## 七、统一坐标结构

所有高德返回坐标统一使用 GCJ-02，并明确标记坐标系。

建议结构：

```json
{
  "longitude": 118.796877,
  "latitude": 32.060255,
  "coordinate_system": "GCJ02",
  "source": "amap"
}
```

要求：

* 不使用未标注坐标系的经纬度对象。
* 外部坐标进入高德路线接口前，应明确其坐标系。
* 现阶段如果无法确认 FlyAI 坐标系，不要默认它就是 GCJ-02。
* 本阶段只实现坐标转换能力，不实现 FlyAI POI 与高德 POI 的自动转换工作流。

---

# 八、本阶段新增的五个 LangChain Tool

## 1. `amap_get_current_city`

### 用途

在用户没有明确提供城市，但询问当地天气、所在城市景点或当地服务时，根据客户端公网 IP 推测当前城市。

适用场景：

* “今天天气怎么样？”
* “我所在城市有什么景点？”
* “本地有什么特色美食？”

不适用场景：

* “离我最近的餐厅。”
* “附近一公里有什么？”
* “从我这里到机场怎么走？”

后面这些场景需要精确 GPS 坐标，IP 定位只能提供城市级推测。

### Tool 输入

模型不直接传入 IP。

建议无显式业务参数，IP 从 FastAPI 请求上下文中获取。

### 返回结构

```json
{
  "province": "江苏省",
  "city": "南京市",
  "adcode": "320100",
  "source": "ip",
  "accuracy_level": "city",
  "is_estimated": true
}
```

### 实现要求

* LLM 不能生成或猜测 IP。
* 不向 LLM 暴露完整客户端 IP。
* 本地开发环境如果获得 `127.0.0.1` 或局域网地址，应返回可识别的不可定位结果。
* 需要处理反向代理场景。
* 不能无条件相信客户端自行提交的 `X-Forwarded-For`。
* 只有在配置了可信代理时，才读取代理转发头。
* 返回结果必须说明属于 IP 推测，而不是精确定位。

---

## 2. `amap_search_places`

### 用途

* 搜索景点。
* 搜索酒店。
* 搜索餐厅。
* 搜索机场和火车站。
* 搜索指定坐标附近的 POI。
* 获取高德 POI ID、标准名称、地址、坐标和行政区编码。
* 为未来 FlyAI POI 标准化提供基础能力。

### 输入建议

```json
{
  "keywords": "南京博物院",
  "city": "南京",
  "adcode": "320100",
  "location": {
    "longitude": 118.796877,
    "latitude": 32.060255
  },
  "radius_meters": 3000,
  "poi_type": null,
  "limit": 10
}
```

### 返回建议

```json
{
  "pois": [
    {
      "poi_id": "",
      "name": "",
      "address": "",
      "province": "",
      "city": "",
      "district": "",
      "adcode": "",
      "poi_type": "",
      "distance_meters": null,
      "location": {
        "longitude": 0,
        "latitude": 0,
        "coordinate_system": "GCJ02",
        "source": "amap"
      }
    }
  ]
}
```

### 实现要求

* 支持关键词搜索。
* 支持按城市或 `adcode` 限定范围。
* 支持周边搜索。
* 结果数量必须限制。
* 空结果返回结构化结果，不要产生无法解析的字符串。
* 不在本阶段自动调用 FlyAI POI 后再调用高德 POI。

---

## 3. `amap_plan_route`

### 用途

查询两个地点之间的一条或多条详细市内路线。

支持模式：

```text
walking
driving
transit
bicycling
electric_bike
```

### 输入建议

```json
{
  "origin": {
    "longitude": 0,
    "latitude": 0,
    "coordinate_system": "GCJ02"
  },
  "destination": {
    "longitude": 0,
    "latitude": 0,
    "coordinate_system": "GCJ02"
  },
  "mode": "transit",
  "city": "南京",
  "strategy": null,
  "waypoints": []
}
```

### 返回建议

```json
{
  "mode": "transit",
  "distance_meters": 3200,
  "duration_seconds": 1200,
  "route_summary": "",
  "steps": [],
  "transfers": 1,
  "walking_distance_meters": 600,
  "taxi_cost": null,
  "polyline": null
}
```

### 实现要求

* 校验经纬度范围。
* 校验路线模式。
* 公交路线缺少城市参数时给出明确错误。
* 途经点只在支持的路线模式下使用。
* 不自动进行多段路线连续规划。
* 本阶段只保证 Tool 可被独立调用。

---

## 4. `amap_travel_time_matrix`

### 用途

批量计算多个地点之间的距离和预计耗时，为后续行程规划提供基础数据。

### 输入建议

```json
{
  "locations": [
    {
      "id": "hotel_1",
      "name": "酒店",
      "longitude": 0,
      "latitude": 0,
      "coordinate_system": "GCJ02"
    },
    {
      "id": "poi_1",
      "name": "中山陵",
      "longitude": 0,
      "latitude": 0,
      "coordinate_system": "GCJ02"
    }
  ],
  "mode": "driving"
}
```

### 返回建议

```json
{
  "mode": "driving",
  "matrix": [
    {
      "origin_id": "hotel_1",
      "destination_id": "poi_1",
      "distance_meters": 10000,
      "duration_seconds": 1800
    }
  ]
}
```

### 实现要求

* 对重复地点去重。
* 对地点数量设置合理上限。
* 根据高德接口限制自动拆批。
* 避免重复计算自己到自己。
* 支持部分请求失败时返回明确错误信息。
* 预留缓存接口。
* 第一阶段可以使用进程内缓存，但不要强制引入 Redis。
* 不实现基于矩阵的景点排序算法。
* 不实现每日行程自动分组。

---

## 5. `amap_get_weather`

### 用途

查询一个城市的当前天气或天气预报。

### 输入建议

```json
{
  "city": "南京",
  "adcode": "320100",
  "forecast": true
}
```

应优先使用 `adcode`，城市名称作为辅助输入。

### 返回建议

```json
{
  "city": "南京市",
  "adcode": "320100",
  "current": {
    "weather": "",
    "temperature": "",
    "humidity": "",
    "wind_direction": "",
    "wind_power": "",
    "report_time": ""
  },
  "forecast": [
    {
      "date": "2026-07-14",
      "day_weather": "",
      "night_weather": "",
      "day_temperature": "",
      "night_temperature": "",
      "day_wind_direction": "",
      "night_wind_direction": ""
    }
  ]
}
```

### 实现要求

* 当前天气和天气预报可以通过一个 Tool 的参数控制。
* 如果只提供城市名称，可以先通过内部地点解析获得 `adcode`。
* 如果城市存在歧义，应返回明确错误或候选结果。
* 本阶段不实现天气自动调整旅游行程。

---

# 九、内部辅助能力

以下能力作为客户端方法或服务层能力实现，不需要全部暴露成 LangChain Tool。

## 1. `resolve_location`

将地点名称解析为标准地点信息：

```text
地点名称
→ 高德 POI
→ 标准名称
→ POI ID
→ 标准地址
→ GCJ-02 坐标
→ adcode
```

本阶段用途：

* 为 POI 查询和路线测试提供统一地点解析。
* 为后续 FlyAI 和高德串联预留基础能力。

本阶段不要求：

* 自动解析全部 FlyAI 搜索结果。
* 自动进行模糊匹配和批量匹配。
* 自动选择多个同名 POI 中的正确地点。

---

## 2. `reverse_geocode`

输入 GCJ-02 坐标，返回：

* 省。
* 市。
* 区县。
* `adcode`。
* 标准地址。
* 附近 POI。

本阶段作为内部方法，不注册为 Tool。

---

## 3. `convert_coordinates`

负责将外部坐标转换为 GCJ-02。

输入应明确包含：

```json
{
  "longitude": 0,
  "latitude": 0,
  "source_coordinate_system": "WGS84"
}
```

输出：

```json
{
  "longitude": 0,
  "latitude": 0,
  "coordinate_system": "GCJ02",
  "source": "amap_conversion"
}
```

本阶段不默认对 FlyAI 坐标自动转换，只提供能力。

---

## 4. `extract_client_ip`

从 FastAPI 请求中提取真实客户端 IP。

需要处理：

* `127.0.0.1`。
* IPv6 loopback。
* 局域网地址。
* Nginx。
* CDN。
* 多层代理。
* 多值 `X-Forwarded-For`。
* 可信代理白名单。

建议将客户端 IP 放入请求级上下文，而不是写入全局变量。

---

# 十、当前时间和日期能力

日期能力需要建设，但本阶段只实现旅游工具调用所需的基础版本，不一次实现复杂的节假日系统。

## 1. FastAPI 自动注入当前时间

每次聊天请求进入时，将以下信息写入请求上下文或未来可复用的 Agent Context：

```json
{
  "current_datetime": "2026-07-13T21:30:00+08:00",
  "current_date": "2026-07-13",
  "timezone": "Asia/Shanghai",
  "weekday": "Monday"
}
```

要求：

* 不把当前日期硬编码到 System Prompt。
* 时间应在每次请求时动态生成。
* 时区必须明确。
* 默认时区通过配置管理。
* 为后续 LangGraph State 预留兼容结构。

环境变量建议：

```env
APP_TIMEZONE=Asia/Shanghai
```

---

## 2. `normalize_travel_dates`

实现一个基础日期标准化服务，不必注册为 LangChain Tool。

第一阶段至少支持：

* 今天。
* 明天。
* 后天。
* 明确的 `YYYY-MM-DD`。
* 明确的年月日中文日期。
* 住 N 晚。
* 入住日期加住宿晚数得到退房日期。

输出示例：

```json
{
  "original_expression": "明天去杭州，住三晚",
  "departure_date": "2026-07-14",
  "check_in_date": "2026-07-14",
  "check_out_date": "2026-07-17",
  "nights": 3,
  "timezone": "Asia/Shanghai",
  "is_ambiguous": false
}
```

以下复杂能力可以留到 Agent 编排阶段：

* 国庆节。
* 春节。
* 法定节假日。
* 调休。
* 本周末和下周末复杂语义。
* “下下周五”。
* 多城市、多段行程日期。
* 模糊时间范围。
* 用户自然语言中多个日期事件的完整抽取。

---

## 3. `validate_travel_dates`

第一阶段校验：

* 日期格式是否合法。
* 出发日期是否早于当前日期。
* 返程日期是否早于出发日期。
* 退房日期是否晚于入住日期。
* 住宿晚数是否为正数。
* 日期是否缺失。

如果表达有歧义，应返回：

```json
{
  "is_valid": false,
  "is_ambiguous": true,
  "message": "无法确定具体日期",
  "candidates": []
}
```

不要由 LLM 随意猜测。

---

# 十一、用户位置解析规则

可以实现统一的 `resolve_user_location` 服务接口，但本阶段不要求接入完整聊天决策流程。

未来位置优先级：

```text
1. 用户当前消息明确提供的位置
2. 当前旅游规划目的地
3. 用户授权的浏览器 GPS
4. 用户保存的常驻城市
5. IP 定位
6. 询问用户
```

本阶段只要求实现：

* 显式城市参数的解析能力。
* IP 城市定位能力。
* 为未来 GPS 和用户位置记忆预留数据结构。

不要求实现：

* 浏览器定位授权。
* 用户常驻城市存储。
* 多轮会话中的目的地继承。
* 自动判断使用当前城市还是旅游目的地。

---

# 十二、Tool 注册

当前已有：

```text
flyai_search_flights
flyai_search_trains
flyai_search_hotels
flyai_search_pois
```

新增：

```text
amap_get_current_city
amap_search_places
amap_plan_route
amap_travel_time_matrix
amap_get_weather
```

FastAPI 启动时，将高德 Tool 与 FlyAI Tool 一起注册到：

```text
app.state.travel_tools
```

要求：

* 不修改现有 FlyAI Tool 的名称和行为。
* 不破坏现有 FlyAI 注册流程。
* 工具名称不能重复。
* 注册结果可通过测试验证。
* 工具列表顺序不应影响功能。
* 如果高德配置缺失，需要根据当前项目风格选择：

  * 启动失败并明确提示；或者
  * 不注册高德工具并记录警告。

优先保持与现有 FlyAI 配置处理方式一致。

---

# 十三、与聊天机器人的集成边界

本阶段只要求：

* 高德 Tool 已完成。
* 高德 Tool 已注册。
* Tool 可以通过单元测试或独立测试函数调用。
* 可选增加一个内部调试接口，用于验证单个 Tool。

本阶段不要求：

* 聊天机器人自动选择高德 Tool。
* LLM 自动调用多个 Tool。
* FlyAI 查询完成后自动调用高德。
* 用户一句话直接生成完整旅游规划。
* 对 Tool 结果进行自动串联。
* 构建 LangGraph State。
* 构建 Agent Router。
* 构建 Planner Agent。

如果当前聊天机器人已经存在通用 Tool Calling 机制，只需确保新高德 Tool 可以被现有机制发现，不要为此重构新的 Agent 编排系统。

---

# 十四、后续 Agent 编排参考

本节只说明当前工具未来可能的使用方式，不属于本次开发和验收范围。

未来可能的业务流程：

```text
用户旅游需求
→ 解析出发地、目的地、日期和偏好
→ FlyAI 查询航班、火车、酒店和 POI 候选
→ 高德完成地点标准化
→ 高德查询目的地天气
→ 高德计算地点之间的距离和耗时
→ 行程规划节点完成景点分组和排序
→ 高德查询最终路段详细路线
→ 生成完整旅游方案
```

明确限制：

* Codex 本次不得实现上述完整流程。
* 不得新增 LangGraph 编排。
* 不得新增 Multi-Agent。
* 不得新增自动行程规划算法。
* 不得因为未来流程而大规模重构当前后端。

---

# 十五、工程实现要求

请按照当前项目已有代码风格实现。

## 数据模型

* 使用 Pydantic 定义 Tool 输入模型。
* 使用 Pydantic 定义结构化输出模型。
* 输入字段添加必要描述。
* 枚举字段使用明确枚举类型。
* 不使用无约束的任意字典代替核心数据结构。

## HTTP 请求

* 复用项目已有 HTTP 客户端方案。
* 如果当前没有统一客户端，可选择 `httpx.AsyncClient`。
* 设置连接和读取超时。
* 只进行有限重试。
* 禁止无限重试。
* 参数错误不重试。
* 限流或临时网络错误最多进行少量重试。

## 日志

日志可以记录：

* Tool 名称。
* 接口名称。
* 请求耗时。
* 结果数量。
* 高德 `infocode`。
* 是否命中缓存。

日志不得记录：

* API Key。
* 完整客户端 IP。
* 完整原始高德响应。
* 用户敏感信息。

## 缓存

第一阶段只需预留缓存抽象。

适合缓存：

* POI 搜索。
* 地理编码。
* 逆地理编码。
* 坐标转换。
* 距离矩阵。
* 城市天气。

不要求本阶段正式接入 Redis。

---

# 十六、测试要求

## 1. `AmapClient` 单元测试

至少覆盖：

* 配置加载成功。
* 缺少 API Key。
* POI 搜索成功。
* POI 搜索空结果。
* IP 定位成功。
* IP 定位返回空城市。
* 天气查询成功。
* 路线规划成功。
* 路线模式参数错误。
* 距离矩阵拆批。
* 高德返回 `status=0`。
* 请求超时。
* 网络异常。
* API Key 不出现在异常中。
* API Key 不出现在日志中。

外部 HTTP 请求使用 Mock，不依赖真实网络。

---

## 2. LangChain Tool 单元测试

至少覆盖：

* Tool 名称正确。
* Tool 描述正确。
* 输入 Schema 正确。
* 输出结构正确。
* 经纬度范围校验。
* 坐标系校验。
* 路线模式校验。
* IP 不由模型直接传入。
* 空结果处理。
* 高德异常能转换为工具层错误。

---

## 3. FastAPI 注册测试

至少覆盖：

* 应用启动后存在 `app.state.travel_tools`。
* 原有四个 FlyAI Tool 仍然存在。
* 五个高德 Tool 已注册。
* 工具名称无重复。
* 未配置高德时的行为符合设计。
* 现有启动流程不被破坏。

---

## 4. 日期能力测试

使用固定当前时间，禁止依赖真实系统日期。

例如固定：

```text
2026-07-13T10:00:00+08:00
```

至少覆盖：

* 今天。
* 明天。
* 后天。
* 明确日期。
* 住三晚。
* 退房日期计算。
* 已过去日期。
* 返程早于出发。
* 入住和退房同一天。
* 非法日期格式。

复杂节假日语义本阶段不强制测试。

---

## 5. 可选真实高德集成测试

默认跳过，通过环境变量启用：

```env
RUN_AMAP_TESTS=1
```

真实测试至少覆盖：

* 搜索一个真实 POI。
* 查询一个城市天气。
* 查询两个真实坐标之间的路线。
* 距离查询或小规模距离矩阵。

IP 定位真实测试：

* 只有能够获取有效公网 IP 时才运行。
* 本地或 CI 环境中无法获取公网 IP 时安全跳过。
* 不要将测试环境公网 IP 输出到日志。

---

# 十七、验收标准

本次任务完成后，应达到以下状态：

* 高德配置可以正常读取。
* `AmapClient` 可以独立使用。
* 五个高德 Tool 已创建。
* 五个高德 Tool 已注册到 `app.state.travel_tools`。
* 每个 Tool 都有明确的 Pydantic 输入和输出结构。
* 单元测试不访问真实高德 API。
* 可通过环境变量启用真实集成测试。
* 现有 FlyAI 航班、火车、酒店和 POI 工具不受影响。
* 现有 FlyAI 真实航班测试仍可运行。
* 项目仍然只是聊天机器人加工具层，没有新增 LangGraph 和 Multi-Agent。
* 没有实现完整旅游规划工作流。
* 没有无关的大规模重构。

---

# 十八、推荐实现顺序

1. 阅读现有项目结构。
2. 阅读 `FlyAIClient`。
3. 阅读现有四个 LangChain Tool。
4. 阅读 FastAPI 启动和工具注册逻辑。
5. 阅读现有测试目录和 Mock 风格。
6. 增加高德配置。
7. 创建高德 Pydantic 数据模型。
8. 实现 `AmapClient` 基础请求和异常处理。
9. 实现 POI 搜索。
10. 实现天气查询。
11. 实现 IP 城市定位。
12. 实现路线规划。
13. 实现距离矩阵。
14. 实现内部地理编码和坐标辅助能力。
15. 创建五个高德 LangChain Tool。
16. 实现请求级客户端 IP 上下文。
17. 实现当前时间和基础日期标准化能力。
18. 注册高德 Tool。
19. 编写单元测试。
20. 编写可选真实集成测试。
21. 运行全部现有测试。
22. 运行新增高德测试。
23. 运行类型检查、格式检查和静态检查。
24. 输出最终修改说明。

---

# 十九、Codex 最终输出要求

完成后请输出：

1. 当前项目结构分析。
2. 新增文件列表。
3. 修改文件列表。
4. 五个高德 Tool 的名称和用途。
5. 高德配置方式。
6. 客户端 IP 的获取方式。
7. 日期上下文的实现方式。
8. 单元测试结果。
9. 真实集成测试结果或跳过原因。
10. 现有 FlyAI 回归测试结果。
11. 当前已知限制。
12. 后续接入 LangGraph 时可以复用的接口。

实现前先阅读现有代码。

不要修改已经验证成功的 FlyAI 调用逻辑，不要重构无关模块，不要提前实现 Agent 编排。
