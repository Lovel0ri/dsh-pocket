# dsh-pocket API 契约（REST + WebSocket）

> **状态：FROZEN（T7 简化版，v1.1）**
> **契约版本：`1.1`（`apiVersion = 1`）**
> **冻结日期：2026-10-27**
> **范围变化（T7，用户明确要求）：** 移除登录/配对/token；历史改为**手机自己的 SQLite**；不做桌面历史合并、不做桌面锁只读降级、不做多设备/TLS/撤销。
> **实现：** `dsh_pocket/`（Python 3.11+ / FastAPI / Uvicorn，单进程，一个 ACP 子进程服务当前工作区）。
> **备注：** 本文件与实现一致；`docs/api-contract.md`/`docs/protocol.md` 仅作 ACP 协议事实来源，T7 的认证与历史边界以本文件为准。

---

## 0. 范围与裁决

### 0.1 与 T2 契约的差异（破坏性）

| 变更 | T2（旧） | T7（现） |
|---|---|---|
| 认证 | 6 位配对码 → deviceToken（Bearer/cookie）+ WS ticket | **无鉴权**（个人 LAN 模式），所有 `/api/v1/*` 与 WS 直接可用 |
| 认证路由 | `/api/v1/auth/pair`、`/auth/ws-ticket`、`/auth/revoke` | **全部删除**（访问返回 404） |
| 历史来源 | 桌面 `session.v4.jsonl.zstd`（zstd/surfaceOp/flock/seq 分组） | **本地 SQLite**（`$DSH_POCKET_HOME/pocket.db`），只含手机新建会话 |
| 会话列表 | ACP `session/list` + 磁盘扫描 + archived/pinned 合并 | **SQLite 查询**（手机自己的会话） |
| 桌面共存 | flock 预检 + 只读降级 | **不做**；resume 失败返回清晰错误，用户可新建 |
| 工作区 | `workspace.json` + browse | 同左（`workspace.json` 最近项 + browse 选择目录），新建会话可传 `workspaceId` 或 `cwd` |

### 0.2 非破坏性新增（additive）

| # | 新增项 | 说明 |
|---|---|---|
| A1 | `GET /api/v1/browse` | 目录浏览（防穿越），供手机选择任意目录 |
| A2 | `POST /api/v1/sessions` 支持 `{"cwd": "/abs/path"}` | `workspaceId` 仍可用；cwd 不存在/不可写返回 409 |
| A3 | `E_INVALID_REQUEST`（400）/`E_PATH_FORBIDDEN`（403）/`E_PATH_INVALID`（400） | 本地校验与 browse 专用 |
| A4 | WS 服务端事件附加 `seq`（每 session 单调递增） | 前端排序/去重/诊断；不是历史游标 |
| A5 | `/health` 附加 `mode`、`apiVersion`、`auth:"none"` | 便于前端识别模式 |
| A6 | transcript 工具输出截断时附加 `"truncated": true` | 完整输出走 tool-calls 端点 |
| A7 | `POST /api/v1/sessions/{id}/close` 幂等 | 关闭后再 close 仍返回 `{"ok":true}` |

### 0.3 T7 明确**不做**（以后开发）

桌面历史读取/合并、桌面会话恢复、`session.v4.jsonl.zstd` 解析、`flock` 只读降级、`session/list` 合并、archived/pinned 从 `workspace.json` 透传、多设备/撤销/TLS、图片输入、非挂载 provider（如 `our-free-model`）兼容、桌面端实时输出。

---

## 1. 部署与无鉴权模式

```
手机浏览器 ──HTTP/WS（无鉴权）──► dsh-pocket server ──ACP stdio──► dsh --profile acp（当前工作区）
                                        │
                                        └─ SQLite: $DSH_POCKET_HOME/pocket.db（手机会话/历史）
```

* 默认监听 `127.0.0.1:8799`；手机使用需显式 `--host 0.0.0.0`。
* **无鉴权**：能访问端口的人即可使用 agent。启动横幅醒目提示「仅限可信局域网，不要公网暴露/端口转发」。二维码只编码访问 URL。
* `GET /api/v1/health` 与全部业务端点都不需要 token/cookie/登录；不存在 401 流程。
* 仅保留一个**非认证**的防刷限流（默认 60 请求/秒/IP，`--api-rate-limit` 可调），超限 `429 E_RATE_LIMITED`。
* 服务器只读 `$DSH_HOME/storages/workspace.json`；不写 `~/.dsh`、不删除任何文件。ACP 子进程自身会按 DSH 的机制写入它自己的 home（这是 DSH 行为）。
* 结构化日志：不记录请求/响应 body、不记录工具输出；ACP 子进程 stderr 落 `$DSH_POCKET_HOME/acp/<workspace>-*.stderr.log`。

### 1.1 启动

```sh
# 无手机联调（mock，无需 DSH）
python3 -m dsh_pocket --mock --host 127.0.0.1 --port 8799

# 真实模式（隔离 home；默认读取 $DSH_HOME 或 ~/.dsh）
python3 -m dsh_pocket --host 0.0.0.0 --port 8799 --dsh-home /path/to/.dev-home
# 可选：--dsh-bin /path/to/dsh  --profile acp  --prompt-timeout 900  --permission-timeout 120
```

真实模式：同一时间只保留**一个** `dsh --profile acp` 子进程，`cwd=当前工作区根`；切换工作区（新建/resume/继续会话）时关闭旧子进程并启动新子进程。子进程异常退出后，下一次请求会自动重启（最多 3 次初始化重试）。

---

## 2. 数据模型

时间均为毫秒 epoch（`createdAt`/`updatedAt`/`time`/`createdAt`(权限)`），`Workspace.updatedAt` 为 `workspace.json` 的 ISO8601 原值。

### 2.1 Workspace

```json
{
  "id": "66c31bee-f9a3-4f3a-a066-43c47a6c4103",
  "path": "/Volumes/extra1tb/clawdbot-project",
  "title": "clawdbot-project",
  "exists": true,
  "writable": true,
  "sessionCount": 2,
  "updatedAt": "2026-10-26T20:05:15.646Z",
  "defaultSessionId": "d2222c6b-9608-4034-80b2-41ada44841c3"
}
```

* 来源：`$DSH_HOME/storages/workspace.json` 的 `tables.workspaces[*]`（只读），按 `updatedAt` 最近优先。
* `sessionCount` / `defaultSessionId`：**手机 SQLite** 中该工作区的会话数/最近会话（不是桌面注册表数量）。
* 通过 `cwd` 新建且路径未登记时，`GET /workspaces` 会追加合成工作区：`id = uuid5(path)`、`title = basename(path)`、`sessionCount` 来自 SQLite。
* `browse` 不会修改 `workspace.json`。

### 2.2 SessionSummary

```json
{
  "sessionId": "d2222c6b-9608-4034-80b2-41ada44841c3",
  "workspaceId": "66c31bee-f9a3-4f3a-a066-43c47a6c4103",
  "cwd": "/Volumes/extra1tb/clawdbot-project",
  "title": "只回复 OK",
  "createdAt": 1793045200000,
  "updatedAt": 1793045260000,
  "archived": false,
  "pinned": false,
  "messageCount": 2,
  "lastSeq": 1,
  "lastMessagePreview": "OK",
  "model": {"provider": "deepseek-official", "model": "deepseek-v4-flash"},
  "origin": "root",
  "resumable": true,
  "activeLocally": true,
  "lockedElsewhere": false,
  "unread": false
}
```

* 全部字段来自手机 SQLite + 当前进程状态；**不包含桌面会话**。
* `archived`/`pinned` 目前恒 `false`（T7 不做归档 UI）；`lockedElsewhere` 恒 `false`；`origin` 恒 `"root"`。
* `lastSeq` = 已完成 turn 的序号（每 turn 一个 seq 组）；`messageCount` = transcript 条数。

### 2.3 ConfigOptions（**原样透传 ACP**）

```json
[
  {"id": "model", "name": "Model", "category": "model", "type": "select",
   "currentValue": "[\"deepseek-official\",\"deepseek-flash\"]",
   "options": [
     {"group": "deepseek-official", "name": "DeepSeek",
      "options": [
        {"value": "[\"deepseek-official\",\"deepseek-v4-flash\"]", "name": "deepseek-v4-flash"},
        {"value": "[\"deepseek-official\",\"deepseek-flash\"]", "name": "DeepSeek-V4.1-Flash"}
      ]}
   ]},
  {"id": "reasoning_effort", "name": "Reasoning effort", "category": "thought_level", "type": "select",
   "currentValue": "low",
   "options": [{"value": "off", "name": "Off"}, {"value": "low", "name": "Low"},
               {"value": "high", "name": "High"}, {"value": "max", "name": "Max"}]}
]
```

前端规则：不解析 `model.value`（不透明串，原样回传）；`""` = provider 默认；`reasoning_effort` 不存在时隐藏控件；收到 `config.update` 整表替换但保留仍有效的选择。

### 2.4 SessionDetail

```json
{
  "summary": { "sessionId": "…", "title": "…", "cwd": "…", "createdAt": 1793045200000, "updatedAt": 1793045260000 },
  "configOptions": [ /* 激活时 ACP 返回的完整表；detached 时为 null */ ],
  "route": {"provider": "deepseek-official", "model": "deepseek-v4-flash", "reasoningEffort": "high"},
  "state": "idle",
  "lockState": "free",
  "providerAvailable": true,
  "usage": {"used": 11569, "size": 1000000},
  "capabilities": {"image": false, "list": true, "resume": true, "close": true},
  "agentInfo": {"name": "deepseek-harness-acp", "version": "0.0.1"}
}
```

`state` ∈ `detached / idle / streaming / awaiting_permission / cancelling / error`。重启后详情为 `detached`，历史仍可读；发消息会自动 `session/resume` 续聊。

### 2.5 TranscriptItem

```json
{"kind": "user", "id": "f25859ea…", "seq": 1, "time": 1793045201000,
 "text": "只回复 OK", "source": {"kind": "user"}}

{"kind": "assistant", "id": "7da09fc1…", "seq": 1, "time": 1793045202000, "turn": 1, "step": 1,
 "text": "OK", "reasoning": "用户要求只回复 OK。",
 "toolCallIds": ["call_00_chjwAR7"], "model": "deepseek-v4-flash",
 "usage": {"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0, "totalTokens": 11569}}

{"kind": "tool", "callId": "call_00_chjwAR7", "name": "bash", "status": "completed", "isError": false,
 "seq": 1, "time": 1793045202500, "turn": 1, "step": 1,
 "arguments": "{\"command\": \"pwd\"}", "outputText": "/Volumes/extra1tb/clawdbot-project\n"}
```

* 每个 turn 一个 `seq`（user + 该 turn 的 assistant/tool 同 seq）；同一 seq 内排序 `user < assistant < tool`。
* 工具输出超过 `maxChars` 时截断并附加 `"truncated": true`；完整内容走 `tool-calls` 端点。

### 2.6 TranscriptPage

```json
{"items": [ /* TranscriptItem[]，seq 升序 */ ],
 "hasMore": true, "nextBefore": 2,
 "session": {"sessionId": "…", "title": "只回复 OK", "lastSeq": 3,
             "usage": {"used": 11569, "size": 1000000}}}
```

### 2.7 PermissionRequest

```json
{
  "type": "permission.request", "seq": 12,
  "requestId": "perm_8f2c1d4a",
  "sessionId": "d2222c6b-9608-4034-80b2-41ada44841c3",
  "toolCallId": "call_00_YYCo2s…",
  "toolName": "bash",
  "argumentsPreview": "{\"command\": \"printf 'hello\\\\n' > /Users/bing/x.txt\"}",
  "options": [
    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}
  ],
  "createdAt": 1793045686532,
  "timeoutMs": 120000
}
```

移动端必须显示 `toolName` + `argumentsPreview`；只有“允许一次/拒绝”；默认拒绝；超时由服务端回 ACP `{"outcome":{"outcome":"cancelled"}}` 并广播 `permission.resolved{timeout}`。

---

## 3. REST API

### 3.1 通用约定

* Base `/api/v1`；请求/响应 `application/json; charset=utf-8`。
* **无认证**：不需要任何 header/cookie。
* 请求体校验失败 → `400 E_INVALID_REQUEST`（含字段错误列表）。
* 错误统一结构：

```json
{"error": {"code": "E_NOT_FOUND", "message": "unknown session: …",
           "details": null, "retryable": false, "suggestedAction": null}}
```

### 3.2 端点总表

| # | 方法 & 路径 | 说明 |
|---|---|---|
| 1 | `GET /api/v1/health` | 存活探针（`ok/mode/auth/version/uptimeSec`） |
| 2 | `GET /api/v1/workspaces` | 工作区列表（`workspace.json` 最近项 + 合成项） |
| 3 | `GET /api/v1/browse?path=&showHidden=` | 目录浏览（扩展；防穿越，默认第一个 browse root/`$HOME`） |
| 4 | `GET /api/v1/sessions?workspaceId=&cursor=&limit=30&includeArchived=false` | 手机会话列表（SQLite，分页） |
| 5 | `POST /api/v1/sessions` | 新建会话：`{"workspaceId":"…"}` **或** `{"cwd":"/abs/path"}` |
| 6 | `GET /api/v1/sessions/{id}` | 会话详情 |
| 7 | `POST /api/v1/sessions/{id}/resume` | 恢复 ACP 会话（刷新后继续聊天） |
| 8 | `POST /api/v1/sessions/{id}/close` | 关闭 ACP 会话（历史保留；幂等） |
| 9 | `GET /api/v1/sessions/{id}/transcript?before=&limit=30&includeReasoning=true&maxChars=4000` | 历史分页（SQLite） |
| 10 | `GET /api/v1/sessions/{id}/tool-calls/{callId}` | 单条工具完整输出 |
| 11 | `POST /api/v1/sessions/{id}/messages` | 发消息（流式仍走 WS） |
| 12 | `POST /api/v1/sessions/{id}/cancel` | 取消在途 turn |
| 13 | `PUT /api/v1/sessions/{id}/config` | 设置 `model`/`reasoning_effort` |
| 14 | `POST /api/v1/sessions/{id}/permissions/{requestId}` | 应答权限请求 |

### 3.3 示例

#### 3.3.1 health

```json
{"ok": true, "version": "0.1.0", "acpProfiles": ["acp"], "uptimeSec": 12,
 "mode": "acp", "apiVersion": 1, "auth": "none"}
```

`mode`: `mock` | `acp`；mock 时 `acpProfiles` 为 `[]`。

#### 3.3.2 workspaces

```json
{"workspaces": [ /* Workspace[] */ ]}
```

#### 3.3.3 browse

```http
GET /api/v1/browse?path=/Volumes/extra1tb&showHidden=false
```

```json
{"path": "/Volumes/extra1tb", "parent": "/Volumes", "home": "/Users/bing",
 "roots": ["/Users/bing"], 
 "entries": [{"name": "clawdbot-project", "path": "/Volumes/extra1tb/clawdbot-project",
              "type": "directory", "symlink": false}]}
```

* 仅列目录，按名称（不区分大小写）升序；`showHidden=false` 跳过 `.` 开头项。
* 未传 `path` → 第一个允许根（默认 `$HOME`）。
* **防穿越**：`realpath` 归一化后必须位于允许根内；逃逸/符号链接逃逸 → `403 E_PATH_FORBIDDEN`；不存在/非目录/相对路径 → `400 E_PATH_INVALID`。

#### 3.3.4 会话列表

```http
GET /api/v1/sessions?workspaceId=66c31bee-…&limit=2
```

```json
{"sessions": [ /* SessionSummary[]（SQLite） */ ], "nextCursor": "WyJzZXNzaW9uLTMuLi4iXQ"}
```

排序 `createdAt` 降序（同毫秒 `sessionId` 升序）；`limit` 1..100（默认 30）；`nextCursor` 仅在还有更多时出现；非法 cursor → `400 E_INVALID_REQUEST`。

#### 3.3.5 新建会话

```http
POST /api/v1/sessions
Content-Type: application/json

{"workspaceId": "66c31bee-f9a3-4f3a-a066-43c47a6c4103"}
```

也支持 `{"cwd": "/Volumes/extra1tb/clawdbot-project"}`（绝对路径、必须存在且可写）。

```http
HTTP/1.1 201 Created
{"session": { /* SessionDetail（configOptions 非 null） */ }}
```

错误：workspace 未知 → `404 E_WORKSPACE_UNKNOWN`；目录不存在/不可写 → `409 E_WORKSPACE_MISSING`；ACP 启动失败 → `503 E_ACP_UNAVAILABLE`。

#### 3.3.6 详情 / resume / close

```http
GET /api/v1/sessions/{id}          → SessionDetail
POST /api/v1/sessions/{id}/resume  → {"configOptions": […], "state": "idle", "lockState": "free", "providerAvailable": true}
POST /api/v1/sessions/{id}/close   → {"ok": true}
```

`resume` 失败：会话已激活 → `409 E_ALREADY_ACTIVE`；会话不可恢复（从未落盘/日志缺失）→ `409 E_NOT_RESUMABLE`（提示新建）；provider 未挂载 → `409 E_PROVIDER_UNAVAILABLE`；其余 ACP 错误不会 500，至少返回 `502 E_ACP_INTERNAL` 或 `503 E_ACP_UNAVAILABLE`。

#### 3.3.7 历史分页

```http
GET /api/v1/sessions/{id}/transcript?limit=2
```

```json
{"items": [ /* TranscriptItem[]（seq 升序） */ ],
 "hasMore": true, "nextBefore": 1,
 "session": {"sessionId": "…", "title": "只回复 OK", "lastSeq": 3,
             "usage": {"used": 11569, "size": 1000000}}}
```

* `before`（seq，不含）省略时取最新一页；`limit` 为 seq 组数 1..100（默认 30），不会切断同一 turn。
* `includeReasoning=false` 时不返回 `reasoning`；`maxChars` 截断文本/工具输出。
* 会话不存在 → `404 E_NOT_FOUND`。

#### 3.3.8 工具详情

```json
{"callId": "call_b", "name": "bash", "status": "completed",
 "arguments": "{\"command\": \"cat README.md\"}", "outputText": "完整未截断输出…", "isError": false}
```

#### 3.3.9 发消息 / 取消 / 配置 / 权限

```json
POST /api/v1/sessions/{id}/messages
{"clientMessageId": "c0a8b6f4-…", "text": "只回复 OK"}
→ 202 {"requestId": "turn_9f2c1d4a8b7e", "clientMessageId": "c0a8b6f4-…"}

POST /api/v1/sessions/{id}/cancel      → {"ok": true}
PUT  /api/v1/sessions/{id}/config      {"configId": "model", "value": "[\"deepseek-official\",\"deepseek-flash\"]"}
     → {"configOptions": [ /* 完整表，currentValue 已更新 */ ]}
POST /api/v1/sessions/{id}/permissions/{requestId}  {"decision": "allow-once"}  // allow-once|reject-once|cancel
     → {"ok": true}
```

* 发消息与 WS `prompt` 等价；会话为 `detached` 时服务端自动 `session/resume` 后续聊；空文本 → 400；在途 turn → `409 E_PROMPT_IN_FLIGHT`。
* `config` 只接受 `model`/`reasoning_effort`；非法 → `400 E_ACP_INVALID`；下一 turn 生效。
* 权限应答必须实现（REST 或 WS 均可），否则工具等待到 120s 超时。

### 3.4 分页语义

| 对象 | 游标 | 单位 | 默认 |
|---|---|---|---|
| 会话列表 | `cursor`（不透明 `base64url(JSON([createdAt, sessionId]))`） | 会话 | 30（1..100） |
| 历史 | `before`（seq 整数，不含） | seq 组（每 turn） | 30（1..100） |

### 3.5 错误码表

| API code | HTTP | 触发 | retryable | suggestedAction |
|---|---|---|---|---|
| `E_NOT_FOUND` | 404 | 未知 session / permission / tool call | false | — |
| `E_WORKSPACE_UNKNOWN` | 404 | workspaceId 不存在 | false | `refresh-workspaces` |
| `E_WORKSPACE_MISSING` | 409 | 工作区目录不存在/不可写 | false | `refresh-workspaces` |
| `E_SESSION_LOCKED` | 409 | ACP 报会话被其它进程占用（如桌面端同时打开同一会话） | false | `new-session` |
| `E_ALREADY_ACTIVE` | 409 | 会话已在本进程激活 | false | `attach` |
| `E_NOT_RESUMABLE` | 409 | 会话无法 resume（无落盘记录等） | false | `new-session` |
| `E_PROVIDER_UNAVAILABLE` | 409 | ACP 报 provider 未挂载（`data.details` 参与判断） | false | `change-model-or-new-session` |
| `E_CWD_MISMATCH` | 409 | ACP 报会话 cwd 不匹配 | false | `refresh-workspaces` |
| `E_PROMPT_IN_FLIGHT` | 409 | 同一会话已有在途 prompt | false | `wait-or-cancel` |
| `E_ACP_INVALID` | 400 | 其它 ACP `-32602`（含非法 config） | false | — |
| `E_ACP_INTERNAL` | 502 | 其它 ACP `-32603` | true | `retry` |
| `E_ACP_UNAVAILABLE` | 503 | dsh 子进程启动失败/退出 | true | `retry` |
| `E_TIMEOUT` | 504 | 服务端等待超时 | true | `retry` |
| `E_RATE_LIMITED` | 429 | 请求限速或上游 RATE_LIMIT/429 | true | `retry-later` |
| `E_INVALID_REQUEST` | 400 | 本地 body/query/WS 消息校验失败 | false | — |
| `E_PATH_FORBIDDEN` | 403 | browse 越界/符号链接逃逸 | false | — |
| `E_PATH_INVALID` | 400 | browse 目标不存在/非目录/cwd 非绝对路径 | false | — |
| `E_INTERNAL` | 500 | 未分类服务端错误（不泄漏堆栈） | false | `retry` |

---

## 4. WebSocket

### 4.1 连接

```
WS /api/v1/ws
```

* 无 ticket、无认证；连接后先收 `hello`。
* 文本帧 UTF-8 JSON，`type` 区分消息；客户端 ≤20 s 发 `ping`，服务端 60 s 无消息则关闭。
* 重连不重放事件；重连后重新 `subscribe` 拉最新一页（SQLite）。

### 4.2 hello

```json
{"type": "hello", "apiVersion": 1, "serverVersion": "0.1.0",
 "capabilities": {"image": false}, "workspaces": [ /* Workspace[] */ ]}
```

### 4.3 客户端 → 服务端

| type | 载荷 | 说明 |
|---|---|---|
| `subscribe` | `{"sessionId","before"?,"limit"?}` | 回 `history.snapshot` + `session.state`；随后收该会话事件 |
| `unsubscribe` | `{"sessionId"}` | 退订（无响应） |
| `history.page` | `{"requestId","sessionId","before","limit"}` | 回 `history.page.result` |
| `prompt` | `{"requestId","sessionId","clientMessageId"?,"text"}` | 发消息；`requestId` 回显；未订阅会自动订阅 |
| `cancel` | `{"sessionId","requestId"?}` | 取消在途 turn |
| `config.set` | `{"requestId","sessionId","configId","value"}` | 等价 REST config |
| `permission.respond` | `{"requestId","decision":"allow-once"\|"reject-once"\|"cancel"}` | 应答权限 |
| `ping` | `{"nonce"}` | 回 `pong` |

消息无法解析/未知 type → `error{E_INVALID_REQUEST}`；未知 session → `error{E_NOT_FOUND}`（连接保持）。

### 4.4 服务端 → 客户端

| type | 载荷 | 触发 |
|---|---|---|
| `hello` | `apiVersion, serverVersion, capabilities, workspaces` | 连接后第一条 |
| `pong` | `nonce` | ping |
| `history.snapshot` | `sessionId, items, hasMore, nextBefore, session`（= TranscriptPage） | subscribe |
| `history.page.result` | `requestId, sessionId, items, hasMore, nextBefore, session` | history.page |
| `session.state` | `sessionId, state, lockState` | 状态变化 |
| `assistant.delta` | `sessionId, requestId, messageId, text` | ACP `agent_message_chunk`（**块级**，按 messageId 聚合） |
| `assistant.thought_delta` | `sessionId, requestId, messageId, text` | ACP `agent_thought_chunk` |
| `tool.started` | `sessionId, requestId, toolCallId, title, rawInput, status:"in_progress"` | ACP `tool_call` |
| `tool.finished` | `sessionId, requestId, toolCallId, status:"completed"\|"failed", output:[{"type":"text","text"}]` | ACP `tool_call_update`（output 已截断） |
| `usage` | `sessionId, used, size` | ACP `usage_update` |
| `config.update` | `sessionId, configOptions` | ACP `config_option_update` / config.set |
| `turn.end` | `sessionId, requestId, stopReason:"end_turn"\|"max_tokens"\|"cancelled"` | prompt 完成 |
| `turn.error` | `sessionId, requestId, code, message, details?, class:"rate-limit"\|"internal"\|"provider"` | prompt 以错误结束 |
| `permission.request` | §2.7 全部字段 | ACP `session/request_permission` |
| `permission.resolved` | `requestId, outcome:"allow-once"\|"reject-once"\|"cancelled"\|"timeout", sessionId` | 服务端确认 |
| `error` | `code, message, details?, sessionId?, requestId?` | 请求级错误（非致命） |

### 4.5 信封与 `seq`

* hub 推送事件（`session.state`/`assistant.*`/`tool.*`/`usage`/`config.update`/`turn.*`/`permission.*`）附加 `seq`：每 session 单调递增（进程内从 1 开始）；不是历史游标。
* `hello`/`pong`/`history.*`/`error` 不带 `seq`。
* 同一 session 的 turn 串行；在途期间第二个 prompt → `error{E_PROMPT_IN_FLIGHT}`。

### 4.6 关键时序

```
连接:   WS /api/v1/ws → hello
选会话: subscribe → history.snapshot + session.state
发消息: prompt → session.state(streaming)
       → assistant.thought_delta* / assistant.delta* / tool.started / tool.finished / usage*
       → (可能) permission.request → permission.respond → permission.resolved
       → turn.end → session.state(idle|error)
上翻:   history.page{before=nextBefore} → history.page.result
取消:   cancel → turn.end{cancelled} → session.state(idle)
关闭页: 直接重连/订阅（历史在 SQLite）
```

### 4.7 状态机

```
detached ──resume/prompt(自动resume)──► idle ──prompt──► streaming ──turn.end──► idle
   ▲                                    │                     │
   └──────── close ─────────────────────┘          permission 请求 → awaiting_permission
                                                    cancel → cancelling → turn.end(cancelled)→idle
子进程崩溃 → error（下一次发消息自动重新 resume）
```

| state | UI |
|---|---|
| `detached` | 历史可见；输入框可用（发消息会自动恢复会话） |
| `idle` | 可发消息 |
| `streaming` | 显示停止按钮 |
| `awaiting_permission` | 权限卡片（阻塞输入） |
| `cancelling` | 停止按钮 loading |
| `error` | 错误条 + 重试（重新发消息会尝试恢复） |

---

## 5. 模式

### 5.1 Mock 模式（前端自测）

```sh
python3 -m dsh_pocket --mock --host 127.0.0.1 --port 8799
```

* 工作区：`--mock-workspace`（可多次）/ 默认当前目录 + `state_dir/demo-workspace`；每个工作区 2 个演示会话。
* 每个 prompt 产生：thought → tool.started → permission.request（可拒绝）→ tool.finished → assistant.delta*3 → usage → turn.end；历史存内存（进程内），不做 SQLite 持久化。
* 用于 UI 渲染/交互联调；真实模式行为以下节为准。

### 5.2 真实模式（T7 个人版）

```sh
python3 -m dsh_pocket --host 0.0.0.0 --port 8799 --dsh-home /path/to/.dev-home
```

* **进程模型**：一个当前工作区的 `dsh --profile acp` 子进程；`cwd=工作区根`；切换工作区关闭旧子进程并重启；子进程崩溃后下一次请求自动重启（最多 3 次初始化重试）。
* **会话/历史**：`$DSH_POCKET_HOME/pocket.db`（SQLite）保存 sessions/messages/tool_calls；进程重启后历史仍在。
* **ACP 方法**：`initialize` / `session/new` / `session/resume` / `session/prompt` / `session/set_config_option` / `session/close` / `session/cancel`（notification）；server→client 只有 `session/request_permission`。
* **重启续聊**：详情为 `detached`，发消息自动 `session/resume`（同一 `$DSH_HOME` 下）；resume 失败给出明确错误（`E_NOT_RESUMABLE`/`E_PROVIDER_UNAVAILABLE` 等），前端提示新建会话。
* **工作区**：`$DSH_HOME/storages/workspace.json`（只读）+ browse；新建会话必须 `isdir` + 可写。
* **不做**：桌面会话/历史、`session/list` 合并、flock 只读、归档/固定透传、多设备/撤销/TLS、图片、未挂载 provider、桌面实时输出。

---

## 6. 安全与运维

1. **无鉴权**：仅可信局域网；默认绑定 `127.0.0.1`，手机访问需 `--host 0.0.0.0`；启动横幅/文档均提示不要公网暴露或端口转发。
2. **目录浏览**：`realpath` + 允许根校验；拒绝 `..`/符号链接逃逸。
3. **不写 `~/.dsh`**：只读 `workspace.json`；ACP 子进程按 DSH 自身机制写它的 home（T7 用隔离 home 验证）。
4. **日志**：不记录请求/响应 body、工具输出、用户消息内容；ACP stderr 落 `$DSH_POCKET_HOME/acp/*.stderr.log`（仅供排查）。
5. **限流**：仅防刷（非认证），429 + Retry-After。

---

## 7. 版本与变更

* `apiVersion = 1`；破坏性变更（路径/字段/事件语义）→ `apiVersion = 2` 且 `/api/v2`。
* 新增字段/端点（additive）不升版本，但必须更新本文档并通知 frontend。
* T7 已删除认证章节与认证端点；旧前端登录流程不再适用（无登录页）。

---

## 附录：T7 验收对照

| 验收项 | 落点 |
|---|---|
| 真实 ACP、单工作区子进程、切换/崩溃重启/退出清理 | `dsh_pocket/acp.py`、`acp_backend.py`；`tests/test_real_backend.py` |
| 新会话/prompt/块级 delta/thought/tool/usage/turn.end/cancel/config/permission | `acp_backend.py`、`ws.py`；`tests/test_real_backend.py`、`tests/test_ws.py` |
| 会话列表/历史分页来自 SQLite，titles/时间/模型/usage | `storage.py`、`acp_backend.py`；`tests/test_real_backend.py` |
| 重启后历史仍在 + 继续会话（resume） | `tests/live/test_live_acp.py`（`DSH_POCKET_LIVE=1`） |
| 工作区来自 `workspace.json` + browse + cwd 校验 + 不写 `~/.dsh` | `workspace_registry.py`、`browse.py`；`tests/test_rest.py` |
| 无鉴权、保留 API/WS 路径与事件名、mock 可用 | `app.py`、`ws.py`；`tests/test_rest.py`、`tests/test_ws.py`、`tests/test_cli.py` |
| mock 测试离线；真实 ACP 测试默认跳过 | `tests/conftest.py`、`tests/fake_acp.py`、`tests/live/` |
