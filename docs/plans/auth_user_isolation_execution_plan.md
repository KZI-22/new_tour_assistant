# 用户认证与多用户会话隔离执行计划

## 一、文档信息

- 状态：实施中（阶段 4 已完成）
- 编写日期：2026-07-22
- 适用范围：FastAPI 后端、PostgreSQL、Redis、Next.js Web 应用
- 前置共识：现有会话均为无价值开发数据，可以在本地重置数据库后执行新迁移
- 实施原则：按本计划分阶段开发、验证和提交；不在通用迁移中隐式删除已有业务数据

## 二、目标

本次实现两个互相依赖的后端能力，并完成 Web 端闭环：

1. 用户注册与登录
   - 中国大陆手机号模拟验证码登录
   - 首次验证码验证成功时自动注册
   - JWT 身份认证
   - 登录状态恢复
   - Refresh Token 轮换与服务端会话撤销
   - 当前设备退出登录
2. 多用户会话数据隔离
   - 新会话绑定当前登录用户
   - 用户只能列出、查看、继续和删除自己的会话
   - 跨用户访问统一表现为资源不存在
   - 消息、工具调用日志和旅行计划通过所属会话继承用户边界
3. Web 应用认证体验
   - 未登录显示手机号验证码登录界面
   - 页面刷新后恢复登录状态
   - 所有受保护请求携带 Cookie
   - Access Token 过期后自动刷新并重试一次
   - 退出时中断活动流、清理前端状态并撤销服务端会话

## 三、非目标

本次不实现：

- 真实短信供应商接入
- 密码登录、邮箱登录、第三方 OAuth
- 国际手机号
- 管理员后台和用户管理接口
- 用户注销账号与数据导出
- 多设备会话管理页面或“退出所有设备”按钮
- PostgreSQL Row Level Security
- 匿名会话及登录后认领匿名会话
- Redis 持久化和 Redis 高可用

## 四、已确认的产品决策

### 1. 注册与登录合并

手机号验证码验证成功后：

- 手机号不存在：在同一事务中创建用户并登录，响应 `is_new_user=true`。
- 手机号已存在：更新最后登录时间并登录，响应 `is_new_user=false`。
- 不单独提供注册接口，避免两套重复验证码流程。

### 2. 手机号规则

- 第一版仅接受中国大陆 11 位手机号。
- API 输入可以是 `13812345678` 或 `+8613812345678`。
- 服务端统一规范化为 E.164：`+8613812345678`。
- 数据库只保存规范化值，并建立唯一约束。

### 3. 模拟验证码

- 每次生成随机 6 位数字，不使用固定万能验证码。
- 有效期 5 分钟。
- 同一手机号 60 秒内不能重复发送。
- 单个验证码最多失败 5 次。
- 验证成功后一次性消费。
- 按手机号和客户端 IP 分别限流。
- 仅 `local`/`test` 环境在发送响应中返回可选 `debug_code`。
- 非本地环境如果仍配置模拟提供方，应用启动必须失败。

### 4. Token 与 Cookie

- Access Token：JWT，默认 15 分钟。
- Refresh Token：密码学安全随机值，默认 30 天。
- Access Token 与 Refresh Token 均存入 `HttpOnly` Cookie。
- 生产 Cookie 使用 `Secure`、`SameSite=Lax`，不设置宽泛的 `Domain`。
- CSRF Token 使用可由前端读取的 Cookie，并由写请求通过 `X-CSRF-Token` 回传。
- Token、验证码和完整手机号不得写入日志。

### 5. 可撤销会话

- Refresh Token 原文只存在客户端 Cookie，数据库仅保存 SHA-256 哈希。
- Access JWT 包含 `sid`，指向 `user_sessions.id`。
- 每个受保护请求在验证 JWT 后检查用户和会话仍有效。
- 退出登录将当前 `user_session` 标记为撤销，因此尚未过期的 Access JWT 也不能继续使用。
- Refresh Token 成功使用后轮换，旧值立即失效。

### 6. 会话隔离

- 任何用户可控请求都不能从请求体或查询参数指定 `user_id`。
- 当前用户只来自服务端认证依赖。
- 用户访问其他用户会话时返回 404，不泄露该会话是否存在。
- 不允许 `conversations.user_id` 为 `NULL`。

## 五、目标数据模型

### 1. `users`

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | UUID | 主键 |
| `phone_e164` | VARCHAR(20) | 非空、唯一 |
| `status` | VARCHAR(20) | `active` / `disabled` |
| `display_name` | VARCHAR(100) | 可空 |
| `phone_verified_at` | TIMESTAMPTZ | 非空 |
| `last_login_at` | TIMESTAMPTZ | 非空 |
| `created_at` | TIMESTAMPTZ | 非空 |
| `updated_at` | TIMESTAMPTZ | 非空 |

索引：

- 唯一索引 `phone_e164`
- 普通索引 `status`

### 2. `user_sessions`

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | UUID | 主键，同时作为 JWT `sid` |
| `user_id` | UUID | 外键到 `users.id`，级联删除 |
| `refresh_token_hash` | VARCHAR(64) | 非空、唯一 |
| `csrf_token_hash` | VARCHAR(64) | 非空 |
| `expires_at` | TIMESTAMPTZ | 非空 |
| `revoked_at` | TIMESTAMPTZ | 可空 |
| `last_used_at` | TIMESTAMPTZ | 非空 |
| `created_at` | TIMESTAMPTZ | 非空 |
| `user_agent` | VARCHAR(500) | 可空 |
| `ip_address` | VARCHAR(64) | 可空，保存可信请求上下文结果 |

索引：

- 唯一索引 `refresh_token_hash`
- 复合索引 `(user_id, revoked_at, expires_at)`

### 3. `conversations`

新增：

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `user_id` | UUID | 非空、外键到 `users.id`，级联删除 |

索引：

- `(user_id, updated_at)`，用于用户会话列表

`messages`、`tool_call_logs`、`travel_plans` 和 `travel_plan_versions` 不重复保存 `user_id`，通过外键链继承 `Conversation` 的所有权。

## 六、Redis 数据协议

Redis 仅保存短期验证码和限流数据，不作为用户或会话事实来源。

建议键空间：

```text
auth:otp:challenge:{challenge_id}
auth:otp:cooldown:{phone_digest}
auth:otp:phone-rate:{phone_digest}:{window}
auth:otp:ip-rate:{ip_digest}:{window}
```

要求：

- Redis 键中不出现明文手机号或验证码。
- 手机号/IP 使用带服务端 secret 的 HMAC 摘要构造键。
- 验证码只保存 HMAC 摘要。
- challenge 记录包含手机号摘要、验证码摘要、失败次数与用途。
- 验证和消费必须原子化，避免并发重复登录。
- Redis 不可用时发送/验证接口返回 503，禁止降级为无验证码登录。
- 测试使用独立 fake store，不依赖真实 Redis。

## 七、API 合约

### 1. 发送验证码

```http
POST /api/v1/auth/sms-codes
Content-Type: application/json

{
  "phone": "13812345678"
}
```

成功：`202 Accepted`

```json
{
  "challenge_id": "uuid",
  "expires_in": 300,
  "resend_after": 60,
  "debug_code": "123456"
}
```

`debug_code` 只在本地模拟模式出现。限流返回 `429`，存储不可用返回 `503`。

### 2. 验证并登录

```http
POST /api/v1/auth/phone-login
Content-Type: application/json

{
  "challenge_id": "uuid",
  "phone": "13812345678",
  "code": "123456"
}
```

成功：`200 OK`，设置 Access、Refresh、CSRF Cookie。

```json
{
  "user": {
    "id": "uuid",
    "phone": "138****5678",
    "display_name": null
  },
  "is_new_user": true,
  "access_expires_in": 900
}
```

验证码无效、过期、已消费或手机号不匹配使用统一错误，不暴露内部状态。

### 3. 当前用户

```http
GET /api/v1/auth/me
```

- 有效 Access Cookie：返回当前用户。
- 缺失、过期、撤销或用户禁用：`401 Unauthorized`。

### 4. 刷新

```http
POST /api/v1/auth/refresh
X-CSRF-Token: <csrf-cookie-value>
```

- 校验 Refresh Cookie、CSRF、会话状态和过期时间。
- 原子轮换 Refresh Token 和 CSRF Token。
- 设置新的 Access、Refresh、CSRF Cookie。
- 返回当前用户和新的 Access 有效期。

### 5. 退出

```http
POST /api/v1/auth/logout
X-CSRF-Token: <csrf-cookie-value>
```

- 幂等：即使 Cookie 缺失，也清理响应 Cookie 并返回 `204 No Content`。
- Cookie 有效时撤销当前数据库会话。

### 6. 受保护接口

以下接口必须依赖当前有效用户：

```text
GET    /api/v1/conversations
GET    /api/v1/conversations/{conversation_id}
DELETE /api/v1/conversations/{conversation_id}
POST   /api/v1/chat/stream
```

`/health` 和 `/models` 保持公开。

## 八、后端模块边界

建议新增或调整：

```text
apps/api/app/
├── api/
│   ├── auth_routes.py
│   ├── dependencies.py
│   └── routes.py
├── core/
│   ├── security.py
│   └── settings.py
├── repositories/
│   ├── auth_session_repository.py
│   └── user_repository.py
├── schemas/
│   └── auth.py
├── services/
│   ├── auth_service.py
│   ├── otp_service.py
│   └── conversation_service.py
└── db/
    └── models.py
```

边界要求：

- `security.py`：JWT、随机 token、HMAC/SHA-256、Cookie 常量，不访问数据库。
- `otp_service.py`：手机号规范化、验证码生成和挑战协议，不访问用户表。
- `auth_service.py`：验证成功后的用户 upsert、服务端会话创建、刷新、撤销。
- `dependencies.py`：从 Cookie 解析 Access JWT，加载有效用户和会话。
- Repository：集中封装用户和服务端会话查询，禁止路由直接拼认证 SQL。
- `conversation_service.py`：所有用户可调用方法显式接收 `user_id`。

## 九、会话服务改造

目标签名：

```python
list_conversations(user_id)
get_conversation(user_id, conversation_id)
delete_conversation(user_id, conversation_id)
start_turn(user_id, conversation_id, model_id, user_content, planning_source)
```

规则：

1. 新会话必须写入 `user_id`。
2. 继续会话使用 `id + user_id` 并保留现有行锁。
3. 列表只按 `user_id` 查询并按 `updated_at DESC` 排序。
4. 详情先按 `id + user_id` 获取父会话，再读取子记录。
5. 删除语句同时限定 `id + user_id`。
6. 找不到或不属于当前用户都抛出同一个 `ConversationNotFoundError`。
7. `finish_turn` 是已授权流内部使用的不可公开方法，可继续按内部 `assistant_message_id` 完成持久化。
8. SSE 在请求开始时完成认证；流执行期间 Access Token 到期不主动中断该次已授权执行。

## 十、迁移策略

新增 Alembic 迁移，依次：

1. 创建 `users`。
2. 创建 `user_sessions`。
3. 给 `conversations` 增加 `user_id`。
4. 创建外键、检查约束和索引。
5. 将 `conversations.user_id` 设为非空。

由于当前数据已确认是无价值开发数据：

- 实施前在本地显式重置 PostgreSQL 数据卷或清空开发数据库。
- 迁移文件不执行 `DELETE FROM conversations`。
- 非空数据库执行迁移时应失败并提示先完成显式数据迁移，不能静默产生无主会话。

## 十一、前端设计

### 1. 认证状态

```text
booting -> authenticated
        -> anonymous
```

- 应用启动先调用 `/auth/me`。
- Access 过期时调用一次 `/auth/refresh`，成功后恢复用户。
- 认证状态未确定前不挂载 `ChatShell`，避免提前读取会话。

### 2. API 客户端

- 所有认证请求使用 `credentials: "include"`。
- 写请求从 CSRF Cookie 读取值并发送 `X-CSRF-Token`。
- 统一处理一次 `401 -> refresh -> retry`。
- 多个并发 401 共享一个 refresh Promise，避免重复轮换导致其他请求失败。
- refresh 失败时清空认证状态，不无限重试。
- 流式聊天继续使用现有 fetch + ReadableStream，不改 SSE 事件协议。

### 3. 登录界面

- 手机号输入。
- 发送验证码按钮及 60 秒倒计时。
- 验证码输入。
- 本地模拟模式显示后端返回的验证码提示。
- 登录失败展示可操作错误，不显示内部异常。

### 4. 退出

- 中断活动 `AbortController`。
- 调用退出接口。
- 无论接口是否已经处于无会话状态，前端最终清空用户、当前会话、消息和历史列表。

## 十二、安全不变量

1. JWT 解码必须固定允许算法，验证 `exp`、`iss`、`aud`、`sub`、`sid` 和 token 类型。
2. JWT payload 不保存手机号、Refresh Token 或验证码。
3. Secret 只能来自环境变量，不提供可用于生产的默认值。
4. Token 和验证码响应使用 `Cache-Control: no-store`。
5. Cookie 认证写请求同时验证 CSRF 和可信 Origin。
6. CORS 只允许配置中的明确 Origin，并保持 `allow_credentials=True`。
7. 登录和验证码错误不泄露手机号是否已经注册。
8. 日志手机号必须掩码；不得记录 Authorization、Cookie、验证码和 token 原文。
9. 禁用用户不能登录、刷新或调用受保护接口。
10. 所有跨用户会话访问统一返回 404。

## 十三、测试计划

### 1. 单元测试

- 手机号规范化与非法号码拒绝。
- 验证码为 6 位数字且不固定。
- challenge 过期、错误次数、一次性消费、手机号不匹配。
- JWT 正常、过期、签名错误、issuer/audience/type 错误。
- Refresh Token 和 CSRF 摘要使用常量时间比较。
- Cookie 在 local/production 设置下的属性。

### 2. Auth API 测试

- 发送验证码成功和限流。
- 首次登录创建用户。
- 再次登录复用用户并创建新设备会话。
- `/me` 在有效、过期、撤销、禁用状态下的响应。
- refresh 成功轮换，旧 Refresh Token 不可重用。
- logout 幂等并撤销当前 session。
- 缺少或错误 CSRF 时拒绝 refresh/logout。
- 模拟验证码不在非本地环境返回。

### 3. 会话隔离测试

创建用户 A、B 后验证：

- A、B 的会话列表互不相见。
- A 读取 B 会话返回 404。
- A 继续 B 会话返回 404，且不新增消息。
- A 删除 B 会话返回 404，B 会话仍存在。
- 新会话自动绑定当前用户。
- 未登录访问所有受保护接口返回 401。
- A 可以正常查看、继续和删除自己的会话。
- 子级消息、工具日志、旅行计划不会通过接口跨用户泄露。

### 4. 前端测试与检查

- 未登录只显示登录界面。
- 登录成功进入 ChatShell 并加载当前用户会话。
- 刷新页面仍保持登录。
- 自动 refresh 不重复发起。
- 退出后清空界面且后退/刷新不能恢复私有数据。
- 流式聊天携带认证 Cookie，现有 SSE 展示不回归。

## 十四、实施阶段与提交边界

### 阶段 0：计划

- [x] 新增本执行计划。
- [x] 复核工作树并提交计划文档。

### 阶段 1：认证基础与验证码登录

- [x] Compose 增加 Redis 和健康检查。
- [x] 增加 Redis/JWT 直接依赖及环境配置。
- [x] 新增用户与服务端会话模型、迁移。
- [x] 实现 OTP store、发送和验证。
- [x] 实现手机号验证即注册/登录。
- [x] 完成相关单元/API 测试。
- [x] 独立提交。

### 阶段 2：登录保持、刷新与退出

- [x] 实现 Access JWT 认证依赖。
- [x] 实现 Refresh Token 原子轮换。
- [x] 实现 `/me`、`/refresh`、`/logout`。
- [x] 完成 Cookie、CSRF、撤销与禁用测试。
- [x] 独立提交。

### 阶段 3：多用户会话隔离

- [x] 开发环境显式重置旧数据。
- [x] `Conversation` 增加非空 `user_id`。
- [x] 路由注入当前用户。
- [x] Service 所有公开会话方法增加 `user_id`。
- [x] 补充跨用户数据库与 API 测试。
- [x] 独立提交。

### 阶段 4：Web 登录闭环

- [x] 增加认证 API 客户端和单次刷新机制。
- [x] 增加认证状态 Provider/Hook。
- [x] 增加手机号验证码登录界面。
- [x] ChatShell 只在认证后挂载。
- [x] 增加用户展示和退出入口。
- [x] 所有会话及流式请求携带凭证。
- [x] 运行 lint/build 并独立提交。

### 阶段 5：整体验收

- [ ] 执行 Ruff。
- [ ] 执行后端离线测试。
- [ ] 执行 PostgreSQL/Redis 集成测试。
- [ ] 执行前端 lint 和 production build。
- [ ] 检查 Alembic upgrade from empty database。
- [ ] 检查 `git status` 和最终提交历史。
- [ ] 更新本计划状态及完成项。

## 十五、验证命令

后端：

```powershell
conda activate py312
ruff check apps/api
python -m pytest
```

数据库与 Redis：

```powershell
docker compose up -d postgres redis
alembic upgrade head
$env:RUN_DATABASE_TESTS='1'
python -m pytest -m database
```

前端：

```powershell
Set-Location apps/web
npm run lint
npm run build
```

## 十六、完成标准

只有同时满足以下条件才可将任务描述为完成：

1. 模拟手机号验证码可以完成首次注册和再次登录。
2. 页面刷新后能恢复登录状态。
3. Refresh Token 可轮换，退出后当前会话立即失效。
4. 未登录用户不能使用聊天和会话 API。
5. 用户 A 无法列出、读取、继续或删除用户 B 的会话。
6. 现有 Agent、工具调用、XHS 登录等待和 SSE 流式行为没有回归。
7. 后端测试、数据库隔离测试、前端 lint/build 均通过。
8. 迁移可从空数据库完整执行，且没有隐式删除业务数据。
9. 各阶段改动按边界形成可独立审查和回滚的 Git 提交。
