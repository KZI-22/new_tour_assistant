请帮我实现一个 Python 版 FlyAI CLI 客户端，并将其封装为后续可供 LangGraph 调用的旅游工具。

## 一、当前环境

* 操作系统：Windows
* Python 为主要开发语言
* FlyAI CLI 已全局安装
* `FLYAI_API_KEY` 已通过 FlyAI CLI 配置成功
* 当前采用的调用链路：

```text
LangGraph Agent
→ Python Tool
→ FlyAIClient
→ subprocess
→ flyai CLI
→ FlyAI 服务
→ JSON 结果
```

FlyAI CLI 不需要单独启动常驻 Node.js 服务。每次调用时，由 Python 启动一个短生命周期的 CLI 子进程，查询完成后退出。

## 二、实现目标

请分成两层实现：

### 1. FlyAIClient 客户端层

负责调用本机 FlyAI CLI，建议放在：

```text
flyai_client.py
```

需要支持以下能力：

```text
execute
search_flight
search_train
search_hotel
search_poi
keyword_search
ai_search
```

其中：

* `execute` 是底层统一执行入口；
* 其他方法负责将业务参数转换为对应的 FlyAI CLI 参数；
* 不要在每个 Tool 中重复编写 subprocess 逻辑。

### 2. LangGraph Tool 层

建议放在一个tools文件夹里：

```text
tools/
├── flight_tool.py
├── train_tool.py
├── hotel_tool.py
├── poi_tool.py
└── travel_search_tool.py
```

第一版重点实现：

```text
search_flight
search_train
search_hotel
search_poi
```

`keyword_search` 和 `ai_search` 可以保留客户端接口，后续再接入 Agent。

## 三、subprocess 调用要求

### 1. 查找 CLI

Windows 全局 npm 安装的命令可能是：

```text
flyai
flyai.cmd
```

应用启动时应自动查找可执行文件，并优先缓存绝对路径。

如果找不到 CLI，返回明确的 `CLI_NOT_FOUND` 错误。

### 2. 安全执行

必须使用参数数组调用 subprocess，不要把命令拼接成一个字符串。

必须：

```text
shell=False
```

禁止使用：

```text
cmd /c
powershell -Command
shell=True
```

避免命令注入、中文引号和空格解析问题。

### 3. 输出处理

需要分别捕获：

```text
stdout
stderr
returncode
```

约定：

* `stdout` 用于读取 FlyAI 返回的 JSON；
* `stderr` 用于错误、警告和 CLI 提示；
* 不要把 stdout 和 stderr 合并；
* 统一使用 UTF-8；
* 对 stdout 执行 JSON 解析。

### 4. 超时控制

建议默认超时为 60 秒。

复杂搜索可以允许单独指定更长超时时间，例如 90 秒。

超时后必须终止子进程，避免残留 Node.js 进程。

### 5. 退出码检查

处理顺序：

```text
启动进程
→ 等待执行
→ 判断是否超时
→ 检查 returncode
→ 解析 stdout JSON
→ 返回统一结果
```

如果 `returncode != 0`，优先视为执行失败。

## 四、统一返回结构

不要让上层代码直接依赖 FlyAI 原始输出格式。

建议定义统一结果对象，至少包含：

```text
success
provider
command
data
error_code
error_message
duration_ms
```

成功时：

```text
success = true
provider = "flyai"
command = 实际执行的命令
data = FlyAI 返回的数据
```

失败时：

```text
success = false
provider = "flyai"
data = null
error_code = 错误类型
error_message = 可读错误信息
```

建议定义以下错误码：

```text
CLI_NOT_FOUND
CLI_TIMEOUT
CLI_EXIT_ERROR
INVALID_JSON
INVALID_ARGUMENT
AUTH_ERROR
RATE_LIMITED
REMOTE_SERVICE_ERROR
EMPTY_RESULT
UNKNOWN_ERROR
```

不要在日志或错误结果中输出 API Key。

## 五、参数校验

在启动 subprocess 之前完成参数校验。

### 航班和火车

校验：

* 出发地不能为空；
* 目的地不能为空；
* 出发地与目的地不能相同；
* 日期格式必须为 `YYYY-MM-DD`；
* 日期不能早于当前日期。

### 酒店

校验：

* 目的地不能为空；
* 入住日期不能为空；
* 离店日期不能为空；
* 入住日期必须早于离店日期；
* 入住日期不能早于当前日期；
* 价格参数必须为正数。

### POI

校验：

* 城市不能为空；
* 查询关键词不能为空；
* 枚举参数只能使用 CLI 实际支持的值。

具体 CLI 参数名称不要凭空猜测。先通过以下命令查看本机真实帮助信息：

```powershell
flyai --help
flyai search-flight --help
flyai search-train --help
flyai search-hotel --help
flyai search-poi --help
flyai keyword-search --help
flyai ai-search --help
```

然后按照本机实际命令参数实现映射。

## 六、Schema 设计

建议在：

```text
app/schemas/travel.py
```

定义结构化输入模型，例如：

```text
FlightSearchInput
TrainSearchInput
HotelSearchInput
PoiSearchInput
```

Agent 只能填写结构化业务字段，例如：

```text
origin
destination
departure_date
```

不能让大模型直接生成或执行完整 CLI 命令。

## 七、Tool 层职责

LangGraph Tool 只负责：

```text
接收结构化参数
→ 参数校验
→ 调用 FlyAIClient
→ 对结果做适量整理
→ 返回给 Agent
```

Tool 中不要直接写 subprocess。

调用关系应为：

```text
flight_tool
→ FlyAIClient.search_flight
→ FlyAIClient.execute
→ flyai CLI
```

## 八、并发与重试

第一版增加简单的并发限制：

* 同时最多执行 2～3 个 FlyAI CLI 子进程；
* 防止单次请求启动过多 Node.js 进程；
* 航班、酒店和 POI 查询后续可以并行执行。

重试规则：

可以重试一次：

```text
CLI_TIMEOUT
RATE_LIMITED
REMOTE_SERVICE_ERROR
```

不要自动重试：

```text
INVALID_ARGUMENT
AUTH_ERROR
CLI_NOT_FOUND
INVALID_JSON
```

暂时不需要实现复杂重试框架，保持代码简单。

## 九、缓存

可以预留缓存接口，但第一版不强制接入 Redis。

后续可对完全相同的查询做短时间缓存，例如：

* 航班查询：5～10 分钟；
* 酒店查询：5～10 分钟；
* POI 查询：30～60 分钟。

缓存 Key 应基于命令和规范化后的参数生成。

## 十、日志要求

日志中记录：

```text
command
参数摘要
returncode
duration_ms
error_code
```

不要记录：

* FlyAI API Key；
* 完整环境变量；
* 其他认证信息。

stderr 需要适量截断，避免日志过长。

## 十一、目录建议（这只是建议，以项目实际目录为准）

```text
app/
├── clients/
│   └── flyai_client.py
├── tools/
│   ├── flight_tool.py
│   ├── train_tool.py
│   ├── hotel_tool.py
│   ├── poi_tool.py
│   └── travel_search_tool.py
├── schemas/
│   └── travel.py
├── exceptions/
│   └── flyai_exceptions.py
├── graph/
│   └── travel_graph.py
└── main.py

tests/
├── test_flyai_client.py
├── test_travel_schemas.py
└── test_travel_tools.py
```

第一阶段先不用实现完整 LangGraph 流程，只需要把 Client、Schema、Tool 和测试做好。

## 十二、测试要求

测试时不要全部依赖真实 FlyAI 网络请求。

需要使用 mock 覆盖以下情况：

1. CLI 正常返回合法 JSON；
2. CLI 不存在；
3. subprocess 超时；
4. CLI 返回非 0 退出码；
5. stdout 不是合法 JSON；
6. stdout 为空；
7. stderr 有警告但 stdout 正常；
8. 中文城市参数可以正常传递；
9. 日期格式错误；
10. 出发地和目的地相同；
11. 酒店入住日期晚于离店日期；
12. API Key 或认证错误；
13. 限流错误；
14. 重试最多执行一次。

另外提供一个可选的真实集成测试，但默认不在普通测试中执行，避免频繁消耗 FlyAI 请求额度。

## 十三、验收标准

完成后应满足：

* Python 可以通过 subprocess 调用本机 FlyAI CLI；
* Windows 下可以正确找到 `flyai.cmd`；
* 参数以数组传递，未使用 shell；
* stdout 和 stderr 分开处理；
* 正常结果可以解析为 JSON；
* 超时后子进程能被终止；
* 错误被转换为统一错误码；
* API Key 不会出现在代码、日志或返回值中；
* LangGraph Tool 不直接依赖 subprocess；
* Client、Tool、Schema 分层清晰；
* 单元测试覆盖主要成功与失败场景；
* 代码具有类型注解；
* 不要过度设计，不要引入不必要的框架。

请先检查本机 `flyai --help` 和各子命令的帮助信息，再开始实现，避免使用错误的命令名称或参数名称。实现完成后，输出项目结构、核心设计说明、测试结果，以及真实调用的手工验证方式。
