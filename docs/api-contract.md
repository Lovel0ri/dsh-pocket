# dsh-pocket API 契约（REST + WebSocket v1）

> 状态：T1 初稿，可直接据此实现 backend / frontend。
> 协议事实来源：[protocol.md](./protocol.md)（含全部 ACP 实测报文与源码路径）。本文件的每个字段都能在 protocol.md 找到依据。
> 版本：`apiVersion = 1`。破坏性变更递增到 2，URL 前缀 `/api/v1`。

---

## 1. 角色与部署拓扑

```
┌──────────────┐  HTTP/WS + Bearer   ┌───────────────────────────┐  ACP stdio (JSON-RPC)   ┌──────────────────┐
│ 手机浏览器    │ ──────────────────► │ dsh-pocket server (Python) │ ─────────────────────► │ dsh --profile …  │
│ (移动端 Web)  │ ◄────────────────── │  · REST 控制面             │ ◄───────────────────── │ (ACP, 每工作区1个)│
└──────────────┘   WS 事件流          │  · WS 事件面               │                        └────────┬─────────┘
                                      │  · 历史读取器（只读日志）    │                                 │ 写
                                      └────────────┬──────────────┘                        ┌────────▼─────────┐
                                                   │ 只读                                    │ ~/.dsh/sessions/ │
                                                   └────────────────────────────────────────►│ …/session.v4.*   │
                                                                                             └──────────────────┘
```

三条硬约束（来自 protocol.md）：

1. **一个 ACP 子进程只服务一个工作区**，且子进程 `cwd = 工作区根`（沙箱根 = 进程 cwd，protocol.md §13.4）。
2. **历史永远从磁盘日志读**（ACP 不回放历史，§9.3），读取不加锁，可与桌面端并发。
3. **同一会话的写操作必须独占**：resume/prompt 前先探测 `session.lock`（§13.2）。

---

## 2. 认证与网络

### 2.1 威胁模型

手机与桌面在同一局域网；DSH 自带 webserver 只绑回环且**无鉴权**（`dsh-host-webserver`），因此 pocket server **必须自己鉴权**，且默认只监听内网网卡。

### 2.2 配对（首次）

1. server 首次启动生成 32 字节随机 `deviceToken`，存 `~/.dsh-pocket/token`（mode 600），终端打印 6 位**一次性配对码**（TTL 5 分钟）与访问 URL（含二维码）。
2. 手机打开 `http://<host>:<port>/`，输入配对码 → `POST /api/v1/auth/pair {code, deviceName}` → 返回 `{token}`，手机存 `localStorage`（或更稳妥：HttpOnly cookie + `SameSite=Lax`；WS 用 ticket，见 2.4）。
3. 配对码单次有效；配对成功后作废；`POST /api/v1/auth/revoke` 可踢掉设备（也支持命令行 `dsh-pocket token rotate`）。

### 2.3 鉴权头

```
Authorization: Bearer <token>
```

* 除 `/api/v1/health`、`/api/v1/auth/pair`、静态资源外，**所有** REST/WS 都要求 token。
* 失败：`401 {"error":{"code":"E_AUTH","message":"invalid or missing token"}}`。
* CORS：**不使用** `*`。允许 `http://<本机LAN地址>:<port>` 与 `http://localhost:<port>`；`Access-Control-Allow-Headers: authorization, content-type`。
* 限流：按 IP 对 `/auth/pair` 5 次/分钟；其余 60 req/s。

### 2.4 WebSocket 鉴权

浏览器 WS 不能自定义 header，采用**短时 ticket**：

```
POST /api/v1/auth/ws-ticket        → {"ticket":"<32B base64url>","expiresIn":30}
WS  /api/v1/ws?ticket=<ticket>     → 连接后 ticket 立即作废
```

服务端校验后发送 `hello`（§5.2）。ticket 一次性、30 秒有效、绑定 IP。

### 2.5 绑定地址

* 默认 `--host 0.0.0.0 --port 8765`（LAN 可访问），但**打印实际网卡地址**；也支持 `--host 127.0.0.1`（仅本机调试）。
* 日志中**不得**打印 token（只打印前 6 位指纹）。
* 可选 `--tls-cert/--tls-key`（自签），手机首访接受证书；不强制。

---

## 3. 数据模型

### 3.1 `Workspace`

```json
{
  "id": "66c31bee-f9a3-4f3a-a066-43c47a6c4103",
  "path": "/Volumes/extra1tb/clawdbot-project",
  "title": "clawdbot-project",
  "exists": true,
  "writable": true,
  "sessionCount": 7,
  "updatedAt": "2026-10-26T20:05:15.646Z",
  "defaultSessionId": "session-4c9f60f8-d40b-47da-beeb-4320c8b67ba4"
}
```

* 来源：`~/.dsh/storages/workspace.json` 的 `tables.workspaces[*]`（protocol.md §11.5）。
* `exists/writable`：server 启动时与切换时 `os.path.isdir` + `os.access(W_OK)` 校验；不存在则 `exists:false`，UI 置灰（ACP `session/new` 不校验 cwd 存在性，见 §4 备注）。
* `sessionCount`：注册表 `sessionIds` 长度（与磁盘目录数可能不同，§11.5）。
* `defaultSessionId`：最近活动的已登记会话（`updatedAt`/`createdAt` 排序），可空。

### 3.2 `SessionSummary`（列表项）

```json
{
  "sessionId": "session-3a77d3aa-2332-4758-9408-ada04dd21ed7",
  "workspaceId": "66c31bee-f9a3-4f3a-a066-43c47a6c4103",
  "cwd": "/Volumes/extra1tb/clawdbot-project",
  "title": "安装 dsh-our-free-model 仓库",
  "createdAt": 1792771698328,
  "updatedAt": 1792785764715,
  "archived": false,
  "pinned": false,
  "messageCount": 103,
  "lastSeq": 637,
  "lastMessagePreview": "再测一次（04:01–04:03）…",
  "model": {"provider": "deepseek-official", "model": "deepseek-flash"},
  "origin": "root",
  "resumable": true,
  "activeLocally": false,
  "lockedElsewhere": false,
  "unread": false
}
```

字段来源（protocol.md §12.1）：

| 字段 | 来源 | 备注 |
|---|---|---|
| `sessionId`/`cwd` | ACP `session/list` | 权威列表（含 ACP 新建、未登记会话） |
| `title` | 日志最后一条 `session/title`；缺失时取首条 user 文本前 40 字 | |
| `createdAt`/`updatedAt` | 日志 header `createdAt` / 最后事件 `time` | 毫秒 epoch |
| `archived`/`pinned` | `workspace.json` 的 `global.*SessionIds` | 未登记会话默认 false |
| `messageCount`/`lastSeq`/`lastMessagePreview` | 解码统计（按 `(mtime,size)` 缓存） | |
| `model` | 日志最后一条 `model/selection` | 可能为 `null`（老会话） |
| `origin` | header `origin ?? "root"` | `subagent` 的会话**不出现**在列表里 |
| `resumable` | `origin!="subagent"` 且无 `parentSession` 且 cwd 存在 | |
| `activeLocally` | pocket server 的 session 表 | |
| `lockedElsewhere` | 对 `session.lock` 做非阻塞 flock 试探 | 只读探测，不修改文件 |

### 3.3 `ConfigOptions`（**原样透传** ACP）

```json
[
  {"id":"model","name":"Model","category":"model","type":"select",
   "currentValue":"[\"deepseek-official\",\"deepseek-flash\"]",
   "options":[{"group":"deepseek-official","name":"DeepSeek",
               "options":[{"value":"[\"deepseek-official\",\"deepseek-flash\"]","name":"DeepSeek-V4.1-Flash"},
                          {"value":"[\"deepseek-official\",\"deepseek-v4-pro\"]","name":"DeepSeek-V4-Pro","description":"…"}]},
              {"group":"deepseek-account","name":"DeepSeek Account","options":[…]}]},
  {"id":"reasoning_effort","name":"Reasoning effort","category":"thought_level","type":"select",
   "currentValue":"low",
   "options":[{"value":"off","name":"Off"},{"value":"low","name":"Low"},
              {"value":"high","name":"High"},{"value":"max","name":"Max"}]}
]
```

**前端规则**：

* 不要解析 `model` 的 `value`（不透明串）；显示 `name`，回传 `value` 原文。
* `reasoning_effort` 不存在时隐藏该控件（只有声明了 reasoning 的模型才有）。
* `value: ""` 表示「Provider default」，显示名「默认」。
* 可能收到异步 `config.update` 事件（§5.3），收到即整表替换选择器内容，保留用户当前选择（若值仍在列表中）。

### 3.4 `TranscriptItem`（历史/实时统一结构，`compact` 形态）

```json
// 用户
{"kind":"user","id":"f25859ea-…","seq":9,"time":1792779550615,
 "text":"https://github.com/… 安装这个github","source":{"kind":"user"}}

// 助手
{"kind":"assistant","id":"7da09fc1-…","seq":20,"time":1792779552059,
 "turn":1,"step":1,"text":"完成：…","reasoning":"用户想要…",
 "toolCallIds":["call_00_chjwAR7…","call_01_R2PYccn…"],
 "model":"deepseek-flash",
 "usage":{"inputTokens":8551,"outputTokens":193,"cacheReadTokens":11520,"cacheWriteTokens":0,"totalTokens":20264}}

// 工具
{"kind":"tool","callId":"call_00_chjwAR7…","name":"bash","status":"completed","isError":false,
 "seq":21,"time":1792779552060,"turn":1,"step":1,
 "arguments":"{\"command\": \"pwd && ls -la | head -40\", \"description\": \"Show working directory and contents\"}",
 "outputText":"/Volumes/extra1tb/clawdbot-project\ntotal 72\n…"}
```

规则（protocol.md §12）：只保留 append-origin 的用户/助手/工具三类；`system/message`、`request/header` 等一律不下发；`tool_call` 与 `assistant/message` 里的 `tool-call` 块按 `callId` 合并为一条 `tool`；同一 `seq` 内排序 user → assistant → tool。

### 3.5 `SessionDetail`

```json
{
  "summary": { /* SessionSummary */ },
  "configOptions": [ /* ConfigOptions，来自 resume/new；未激活时为 null */ ],
  "route": {"provider":"deepseek-official","model":"deepseek-flash","reasoningEffort":"low"},
  "state": "idle",                       // §6 状态机
  "lockState": "free",                   // free | held-elsewhere | held-locally
  "providerAvailable": true,             // 可选：是否能在当前 profile 恢复
  "usage": {"used":129424,"size":1048576},
  "capabilities": {"image":false,"list":true,"resume":true,"close":true},
  "agentInfo": {"name":"deepseek-harness-acp","version":"0.0.1"}
}
```

### 3.6 `PermissionRequest`（服务端增强后下发）

ACP 原始请求只有 `{sessionId, toolCall:{toolCallId}, options[]}`（protocol.md §6.2，**没有** reason/标题）。服务端用 `toolCallId` 关联自己缓存的 `tool_call` update 补全：

```json
{
  "requestId": "perm_8f2c1d",           // 服务端生成，用于应答与去重
  "sessionId": "dc61635d-…",
  "toolCallId": "call_00_YYCo2s…",
  "toolName": "bash",
  "argumentsPreview": "printf 'hello\\n' > /Users/bing/x.txt",
  "options": [{"optionId":"allow-once","name":"Allow once","kind":"allow_once"},
              {"optionId":"reject-once","name":"Reject","kind":"reject_once"}],
  "createdAt": 1793045686532,
  "timeoutMs": 120000
}
```

> 移动端必须显示 `toolName` + `argumentsPreview` + 「一次」字样；默认按钮 = 拒绝。超时（默认 120 s）由服务端回 `{"outcome":{"outcome":"cancelled"}}`。

---

## 4. REST 端点

所有响应 `application/json; charset=utf-8`；时间均为**毫秒 epoch**（`createdAt`/`updatedAt`/`time`），除非字段名以 `At` 结尾且为 ISO8601 的注册表原始值（如 `Workspace.updatedAt`，沿用 workspace.json 原值）。

| # | 方法 & 路径 | 说明 | 请求 | 成功响应 |
|---|---|---|---|---|
| 1 | `GET /api/v1/health` | 存活探针（免鉴权） | — | `{"ok":true,"version":"0.x.y","acpProfiles":["pocket"],"uptimeSec":123}` |
| 2 | `POST /api/v1/auth/pair` | 配对换 token | `{"code":"123456","deviceName":"iPhone"}` | `{"token":"…","expiresAt":null}` |
| 3 | `POST /api/v1/auth/ws-ticket` | WS 一次性 ticket | — | `{"ticket":"…","expiresIn":30}` |
| 4 | `GET /api/v1/workspaces` | 工作区列表 | — | `{"workspaces":[Workspace]}` |
| 5 | `GET /api/v1/sessions` | 会话列表（分页） | `?workspaceId=&cursor=&limit=30&includeArchived=false` | `{"sessions":[SessionSummary],"nextCursor":"…"}` |
| 6 | `POST /api/v1/sessions` | **新建**会话（ACP `session/new`） | `{"workspaceId":"…"}` | `201 {"session":SessionDetail-with-configOptions}` |
| 7 | `GET /api/v1/sessions/{id}` | 会话详情 | — | `SessionDetail` |
| 8 | `POST /api/v1/sessions/{id}/resume` | 恢复（ACP `session/resume`） | `{}` | `{"configOptions":[…],"state":"idle"}` |
| 9 | `POST /api/v1/sessions/{id}/close` | 关闭（释放 Agent，日志保留） | `{}` | `{"ok":true}` |
| 10 | `GET /api/v1/sessions/{id}/transcript` | 历史分页 | `?before=<seq>&limit=30&includeReasoning=true&maxChars=4000` | `TranscriptPage` |
| 11 | `GET /api/v1/sessions/{id}/tool-calls/{callId}` | 单条工具完整输出（不截断） | — | `{"callId":"…","name":"bash","status":"completed","outputText":"…","arguments":"…"}` |
| 12 | `POST /api/v1/sessions/{id}/messages` | 发消息（HTTP 侧；流式仍走 WS） | `{"clientMessageId":"uuid","text":"…"}` | `202 {"requestId":"turn_…","clientMessageId":"…"}` |
| 13 | `POST /api/v1/sessions/{id}/cancel` | 取消在途 turn | `{"requestId":"…"?}` | `{"ok":true}` |
| 14 | `PUT /api/v1/sessions/{id}/config` | 设置 model / reasoning_effort | `{"configId":"model","value":"[\"…\",\"…\"]"}` | `{"configOptions":[…]}` |
| 15 | `POST /api/v1/sessions/{id}/permissions/{requestId}` | 应答权限 | `{"decision":"allow-once"\|"reject-once"\|"cancel"}` | `{"ok":true}` |

### 4.1 分页

* **会话列表**：`cursor` 为不透明串（服务端实现：`base64url(JSON([createdAt, sessionId]))`，与 ACP `session/list` 同构，protocol.md §8）；`limit` 1..100，默认 30；返回 `nextCursor` 仅当还有更多。排序：`createdAt` 降序。
* **历史**：`before` = seq 游标（**不含**），`limit` = **seq 组数**（1..100，默认 30）。响应：

```json
{"items":[TranscriptItem…],
 "hasMore":true,
 "nextBefore":589,
 "session":{"sessionId":"…","title":"…","lastSeq":637,"usage":{…}}}
```

  * 首次加载不传 `before` → 返回**最新**一页；向上滚动时把上一页 `nextBefore` 作为 `before`。
  * 一页可能包含多个同 seq 的 item（assistant + 其工具），不会被切断（protocol.md §12 分页算法）。
  * `maxChars` 控制单块/工具输出截断（默认 4000），完整内容走端点 11。

### 4.2 典型错误

```json
{"error":{"code":"E_SESSION_LOCKED","message":"会话正在桌面端使用，无法在此继续",
          "details":"session \"session-…\" is already owned by an active write handle",
          "acpCode":-32603,"recoverable":true,"suggestedAction":"open-readonly"}}
```

`code` 映射（实现时照抄，protocol.md §4）：

| API code | HTTP | 触发 | `suggestedAction` |
|---|---|---|---|
| `E_AUTH` | 401 | token 缺失/错误 | `re-pair` |
| `E_NOT_FOUND` | 404 | 未知 session/request | — |
| `E_WORKSPACE_UNKNOWN` | 404 | workspaceId 不存在 | `refresh-workspaces` |
| `E_WORKSPACE_MISSING` | 409 | 目录不存在（再校验） | `refresh-workspaces` |
| `E_SESSION_LOCKED` | 409 | ACP `-32603` + `already owned by an active write handle` | `open-readonly` |
| `E_PROVIDER_UNAVAILABLE` | 409 | ACP `-32603` + `no adapter registered for provider "x"` | `change-model-or-new-session` |
| `E_NOT_RESUMABLE` | 409 | ACP `-32602` + `session is not resumable` | `open-readonly` |
| `E_CWD_MISMATCH` | 409 | ACP `-32602` + `session cwd does not match` | `refresh-workspaces` |
| `E_ALREADY_ACTIVE` | 409 | ACP `-32602` + `session is already active` | `attach` |
| `E_PROMPT_IN_FLIGHT` | 409 | ACP `-32602` + `a prompt is already in flight` | `wait-or-cancel` |
| `E_ACP_INVALID` | 400 | 其它 `-32602`（含 SDK 层 `Invalid params`） | — |
| `E_ACP_INTERNAL` | 502 | `-32603` 其它 | `retry` |
| `E_ACP_UNAVAILABLE` | 503 | 子进程退出/启动失败 | `retry` |
| `E_TIMEOUT` | 504 | 服务端等待超时（prompt 默认 15 min，权限 120 s） | `retry` |
| `E_RATE_LIMITED` | 429 | 上游限流（turn error 含 `RATE_LIMIT`/429） | `retry-later` |

`retryable` 字段：`E_ACP_UNAVAILABLE`/`E_TIMEOUT`/`E_RATE_LIMITED` = true，其余 false。

### 4.3 备注（实现必须知道）

* **端点 6 的 cwd 校验**：ACP 不校验目录存在性（protocol.md §3），server 必须先 `isdir`，否则会创建「幽灵会话」。
* **端点 8/12 的锁预检**：resume/prompt 前对 `<sessions>/<projectKey>/<id>/session.lock` 做 `flock(LOCK_EX|LOCK_NB)` 试探：抢到 → 立刻 `LOCK_UN` 并继续（说明没别人写）；`EWOULDBLOCK` → 直接返回 `E_SESSION_LOCKED`（**不要**先 let ACP 试，省一次 30 s 超时）。
* **端点 12 与 WS 的关系**：两者等价；`requestId` 是服务端 turn 标识，后续 WS 事件用它关联。`clientMessageId` 用于重试幂等（同 id 已处理 → 返回原 `requestId`，不重复发）。
* **端点 14 生效于下一 turn**（protocol.md §10），当前在途 turn 不受影响；前端要提示。
* **端点 15 必须实现**：不实现会让 ACP 请求悬挂直到超时，工具被卡住。

---

## 5. WebSocket 协议

### 5.1 连接

```
WS /api/v1/ws?ticket=<one-time-ticket>
```

* 二进制/文本帧均为 UTF-8 JSON 文本；每个消息一个 `type`。
* 心跳：客户端每 20 s `{"type":"ping","nonce":n}`，服务端回 `{"type":"pong","nonce":n}`；服务端 60 s 无消息断开。
* 重连：客户端带 `lastEventSeq`（每 session 独立）→ 服务端回 `history.snapshot` 补齐（简化：重连即重新拉最近一页 transcript，服务端不保证事件重放）。

### 5.2 服务端 → 客户端

| `type` | 载荷 | 说明 |
|---|---|---|
| `hello` | `{"apiVersion":1,"serverVersion":"0.x","capabilities":{"image":false},"workspaces":[Workspace]}` | 连接后第一条 |
| `error` | `{"code","message","details"?,"sessionId"?,"requestId"?}` | 与 REST 错误码同表 |
| `session.state` | `{"sessionId","state","lockState"?}` | 状态机变化（§6） |
| `assistant.delta` | `{"sessionId","requestId","messageId","text"}` | ACP `agent_message_chunk`（**块级**，见 protocol.md §5.2）。UI 按 `messageId` 追加 |
| `assistant.thought_delta` | `{"sessionId","requestId","messageId","text"}` | `agent_thought_chunk` |
| `tool.started` | `{"sessionId","requestId","toolCallId","title","rawInput","status":"in_progress"}` | `tool_call` |
| `tool.finished` | `{"sessionId","requestId","toolCallId","status":"completed"\|"failed","output":[{"type":"text","text":"…"}]}` | `tool_call_update`；`output` 单条已截断（完整走端点 11） |
| `usage` | `{"sessionId","used","size"}` | `usage_update` |
| `config.update` | `{"sessionId","configOptions":[…]}` | `config_option_update`，可随时到达 |
| `turn.end` | `{"sessionId","requestId","stopReason":"end_turn"\|"max_tokens"\|"cancelled"}` | ACP prompt 的 result |
| `turn.error` | `{"sessionId","requestId","code","message","details"?,"class":"rate-limit"\|"internal"\|"provider"}` | prompt 以 JSON-RPC error 结束（如限流 429） |
| `permission.request` | `PermissionRequest`（§3.6） | 必须应答，否则工具有 120 s 超时 |
| `permission.resolved` | `{"requestId","outcome":"allow-once"\|"reject-once"\|"cancelled"\|"timeout"}` | 服务端确认（多端同步用） |
| `history.invalidated` | `{"sessionId"}` | 服务端发现日志被外部（桌面端）追加，提示前端重拉 |
| `history.snapshot` | `{"sessionId","items":[TranscriptItem],"hasMore":bool,"nextBefore":seq,"session":{…}}` | `subscribe` 的首次响应：**最新一页**（结构 = `TranscriptPage`） |
| `history.page.result` | `{"requestId","sessionId","items":[TranscriptItem],"hasMore":bool,"nextBefore":seq,"session":{…}}` | 响应客户端的 `history.page` |
| `session.list.changed` | `{"workspaceId"}` | 可选（P1）：会话列表可能变化，前端重拉 `GET /sessions` |
| `pong` | `{"nonce":n}` | 响应 `ping` |

> 语义：`requestId` 只在**本次 turn**内有效；服务端对同一 session 的 turn 串行（ACP 限制，§7.1）。

### 5.3 客户端 → 服务端

| `type` | 载荷 | 说明 |
|---|---|---|
| `subscribe` | `{"sessionId","before"?:seq,"limit"?:n}` | 订阅；服务端回 `history.snapshot`（最近一页）+ `session.state` |
| `unsubscribe` | `{"sessionId"}` | |
| `history.page` | `{"requestId","sessionId","before":seq,"limit":n}` | 请求更早一页；回 `history.page.result` |
| `prompt` | `{"requestId","sessionId","clientMessageId","text"}` | 发消息；回 `session.state` + 事件流 + `turn.end` |
| `cancel` | `{"sessionId","requestId"?}` | 取消在途 turn |
| `config.set` | `{"requestId","sessionId","configId","value"}` | 等价 REST 端点 14，回 `config.update` |
| `permission.respond` | `{"requestId","decision":"allow-once"\|"reject-once"\|"cancel"}` | 等价 REST 端点 15 |
| `ping` | `{"nonce":n}` | — |

`history.snapshot` / `history.page.result` 结构 = `TranscriptPage`（§4.1）。

### 5.4 关键时序（前端照此实现）

```
连接:  hello → (用户选工作区) → subscribe{sessionId}
订阅:  history.snapshot（最新一页，nextBefore）
发消息: prompt{requestId} 
        → session.state: "streaming"
        → assistant.thought_delta* / assistant.delta* / tool.started / tool.finished / usage*
        → (可能) permission.request → permission.respond → permission.resolved
        → turn.end{stopReason} | turn.error{...}
        → session.state: "idle"
上滚:  history.page{before=nextBefore} → history.page.result
取消:  cancel → turn.end{stopReason:"cancelled"}（实测取消 0.01 s 内返回，protocol.md §7.1）
```

---

## 6. 服务端会话状态机

```
        ┌──────────┐  resume(锁空闲) ┌──────────┐   prompt    ┌───────────┐
  ─────►│ detached │ ──────────────► │   idle   │ ──────────► │ streaming │
        └──────────┘                 └──────────┘ ◄────────── └───────────┘
             ▲   ▲                        │  ▲      turn.end       │
   close/退出 │   │ resume(锁被占)          │  └───────────────────┘
             │   └── readonly ─────────────┘   permission 请求 → state=awaiting_permission
             │                                      │ 应答后回 streaming
             └──────────────────────────────────────┘
   失败分支：error（E_ACP_* 后保持 detached/idle，可重试）
```

状态定义（前端据此渲染）：

| state | 含义 | UI |
|---|---|---|
| `detached` | 只有历史，无 ACP session | 输入框禁用，按钮「继续对话」 |
| `readonly` | 会话被其它进程占用（锁）或 provider 不可用 | 只读历史 + 提示原因 + 「新建会话」 |
| `idle` | 已 resume/new，可发消息 | 输入框可用 |
| `streaming` | 有在途 turn | 显示停止按钮 |
| `awaiting_permission` | 有未应答权限请求 | 弹出权限卡片（阻塞输入） |
| `cancelling` | 已发 cancel，等 ACP 返回 | 停止按钮 loading |
| `error` | 上一步失败 | 错误条 + 重试 |

---

## 7. 服务端实现要点（backend checklist）

1. **进程池**：`workspaceId → AcpChild`（懒启动）。启动参数：`DSH_HOME=<home> dsh --profile <profile>`，`cwd=workspace.path`，stdin/stdout 管道，stderr 落日志文件。
2. **握手**：每个子进程启动后 `initialize`（超时 60 s）；记录 `capabilities`。失败 → `E_ACP_UNAVAILABLE`，指数退避重启（最多 3 次）。
3. **会话表**：`sessionId → {child, requestId, state, pendingPermission, toolCalls:{callId:{title,rawInput}}, usage, lastMessageIds}`。内存即可，重启后从磁盘恢复列表。
4. **锁预检**：见 §4.3；只在 resume/prompt 前做，探测后立刻释放。
5. **历史读取**：`zstd -dc` 子进程流式读取（不要一次性 `read()`），按 `(path, mtime, size)` 缓存归一化结果；列表接口只读 header + 尾部若干事件（实现建议：只解码最后 200 行即可拿 title/最后消息；大文件可 `zstd -dc | tail`）。
6. **工具输出截断**：`maxChars`（默认 4000）+ `truncated:true`；完整内容端点 11 按需重新解码并定位 `callId`（缓存最近 N 个会话的 callId→seq 索引）。
7. **权限**：收到 ACP `session/request_permission` → 生成 `requestId` → 广播 `permission.request` → 等待客户端决定（REST 或 WS 均可达）→ 回 ACP。超时/断开 → `{"outcome":{"outcome":"cancelled"}}`，并广播 `permission.resolved(timeout)`。
8. **turn 归属**：ACP update 不带 turn id，服务端按「每个 session 同时只有一个在途 prompt」把 update 归到当前 `requestId`（protocol.md §7.1 保证）。
9. **停止/退出**：先对所有 session `close`，再关 stdin（EOF）等 exit 0；超时 `SIGTERM`→`SIGKILL`（protocol.md §7.3）。
10. **日志**：结构化（JSON lines），记录 ACP 请求/响应摘要（**不含** token、不含完整系统提示/工具输出）。
11. **`session/list` 增量**：WS 连接期间每 30 s（或收到用户操作时）刷新一次列表并推 `session.list.changed`（可选，P1）。
12. **不写 `~/.dsh`**：pocket 自身状态放 `~/.dsh-pocket/`；会话日志只读；**绝不**删除 `session.lock`（protocol.md §13.1）。

---

## 8. 前端（移动端）契约要点

1. **首次进入**：`GET /workspaces` → 选工作区 → `GET /sessions?workspaceId=…` → 选会话/新建。
2. **列表项**：`title` + 相对时间（`updatedAt`）+ 模型 chip + 状态徽标（`lockedElsewhere` → 「桌面端使用中」；`archived` → 「已归档」）。
3. **会话页**：进入即 `subscribe`；顶部显示 `SessionDetail.configOptions` 的当前模型/思考强度（点击改 → `PUT /config` 或 WS `config.set`）。
4. **消息渲染**：
   * 用户/助手文本：Markdown（代码块、表格）；助手 `reasoning` 折叠（默认收起，显示「思考过程」）。
   * 工具卡片：`toolName` + 参数摘要（可展开）+ 状态（运行中/成功/失败）；点击 → 端点 11 拉完整输出（等宽字体、可复制）。
   * `usage`：显示在会话头部（`used/size` 百分比）。
5. **权限弹窗**：模态，必须显示工具名 + 参数摘要；按钮「允许一次」/「拒绝」；超时倒计时；不允许「全部允许」。
6. **流式**：`assistant.delta` 按 `messageId` 聚合追加，`tool.started/finished` 插在对应位置（服务端保证顺序）。
7. **重连**：断线重连后重新 `subscribe`（服务端回最新一页 + 状态），不要假设事件补发。
8. **只读模式**：`state=readonly` 时禁用输入，顶部提示原因（锁 / provider），提供「新建会话」按钮。
9. **深色模式 / 安全区 / 输入法**：移动端优先；消息列表用 `before` 游标上拉加载。
10. **错误提示**：按 `E_*` 码给中文文案 + 一个主行动按钮（见 §4.2 `suggestedAction`）。

---

## 9. 兼容性与版本

* 服务端 `hello` 里带 `apiVersion`；前端不匹配时提示刷新。
* ACP `agentInfo.version`（当前 `0.0.1`）与 DSH 版本（`0.2.1-rc.3`）建议在 `/health` 暴露，便于排查。
* 新增字段只加不改；`configOptions` 永远原样透传，不做服务端裁剪。

---

## 10. 与 T2/T3 的接口边界

| 模块 | 负责 | 依赖本契约 |
|---|---|---|
| T2 ACP 桥接层 | 子进程管理、JSON-RPC 收发、错误分类、`session/update` → WS 事件 | §4.3、§5.2、§7.1–7.8、protocol.md §3–§7 |
| T2 历史层 | 日志解码、归一化、分页、元数据补全 | §3.4、§4.1、protocol.md §11–§12 |
| T3 REST/WS 层 | 端点、鉴权、限流、状态机 | §2、§4、§5、§6 |
| T4 前端 | 页面、流式渲染、权限 UI | §3、§5.4、§8 |
| T5 启动脚本/手册 | 令牌、profile 选择、绑定地址、日志 | §2、§7.9、protocol.md §1.2 |
