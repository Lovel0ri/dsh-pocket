# dsh-pocket 协议事实与可行性结论（T1）

> 状态：T1 完成。所有结论均来自**可复现实验**（`spikes/`）或**安装包源码**，每条附命令 / 源码路径 / 记录文件。
> 阅读对象：backend（实现 pocket server）、frontend（实现移动端 UI）。
> 配套文档：[api-contract.md](./api-contract.md)（REST + WebSocket 契约，可直接照此实现）。

---

## 0. 结论先行（Go / No-Go）

| 问题 | 结论 | 证据 |
|---|---|---|
| 能否用 ACP 驱动本机 DeepSeek Harness？ | **Go**。`dsh --profile acp` 是标准 ACP v1 stdio server，`initialize/session/new/prompt/list/resume/close/set_config_option/cancel` 全部实测通过，含真实模型调用（0.65–1.5 s 返回） | `spikes/_transcripts/prompt.summary.json`、`initialize.summary.json` |
| 能否共享 `~/.dsh` 凭据 / 工作区 / 会话？ | **Go（凭据与工作区完全共享；会话需按锁状态分级）**。复制 `~/.dsh/.credentials.yaml` 到隔离 home 后模型调用成功；`session/list` 能列出真实桌面会话；`session/resume` 能恢复真实桌面会话 | `spikes/setup_dev_home.sh`、`resume.summary.json` |
| 能否读桌面历史？ | **Go**。`session.v4.jsonl.zstd` 可用 `zstd -dc` 流式解码；已解码 3 个真实会话并产出归一化算法与分页策略；读取**不加锁**，可与桌面并发 | `spikes/history_decode.py`、§11–§12 |
| 与桌面端同时运行是否安全？ | **有条件 Go**。**同一会话**同时被桌面持有写锁时，ACP `session/resume` 失败（`-32603` + `data.details` = `already owned by an active write handle`），但**读取历史不受影响**；必须按 §13 的锁探测 + 降级实现 | `spikes/_transcripts/lock.summary.json` |
| 非 DeepSeek 会话（`our-free-model`）能否恢复？ | **支持，但需要用户/部署侧配置**：在 ACP 所在 profile 里挂上对应 provider 插件即可。已用 `pocket` profile 实测：两个 `our-free-model` 会话 resume 成功、configOptions 正确 | `spikes/_transcripts/pocket/resume.summary.json`、§14 |
| ACP 是否提供历史回放 / 删除 / fork / 多工作区？ | **不提供**（官方明确不支持）。历史必须由 pocket server 自己读日志；删除/fork 不做；一个 ACP 进程一个工作区（§13.4） | `dsh-acp` README、`acp-index.js` |
| 服务端到浏览器是否安全？ | DSH 自带 webserver 只绑回环且**无鉴权**；pocket server 必须在 LAN 上自行做 token 鉴权（§api-contract §2） | `dsh-host-webserver/lib/index.js` |

**总判定：Go。** 唯一必须提前设计的风险是「同一会话的写锁冲突」和「provider 未挂载」，两者都有确定性的检测手段与降级路径（§15）。

---

## 1. 实验环境与复现

### 1.1 环境事实

| 项 | 值 |
|---|---|
| dsh CLI | `/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh`（shell 启动器，最终以 `ELECTRON_RUN_AS_NODE=1 …/MacOS/DeepSeek Harness --expose-internals …/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js "$@"` 运行） |
| 版本 | `@deepseek-ai/dsh-desktop-runtime` 0.2.1-rc.3（`app.asar/dsh/package.json`） |
| ACP server | `@deepseek-ai/dsh-acp` 0.2.1-rc.3（源码：`app.asar/dsh/node_modules/@deepseek-ai/dsh-acp/lib/index.js`，1 467 行；本仓库可读副本 `spikes/_asar_src/dsh-acp/lib/index.js`） |
| 桌面端 | 正在运行，不能退出；其活动会话持有 kernel `flock`（§13.1） |
| 隔离 home | `04_tools/dsh-pocket/.dev-home`（sandbox 只允许写 `04_tools/dsh-pocket`，且不去动 `~/.dsh`） |
| Python | 3.x，**无 `zstandard` 模块** → 历史解码统一走 `zstd -dc` 子进程 |

### 1.2 一条命令复现全部实验

```sh
cd /Volumes/extra1tb/clawdbot-project/04_tools/dsh-pocket

sh spikes/setup_dev_home.sh --with-provider     # 建隔离 home（凭据/工作区/4 个真实会话/acp+pocket profile）
python3 spikes/acp_probe.py --scenario all      # 10 个 ACP 场景，写 spikes/_transcripts/*.jsonl + *.summary.json
python3 spikes/acp_probe.py --scenario resume --profile pocket --out-dir spikes/_transcripts/pocket
python3 spikes/lock_probe.py                    # 桌面共存 / 写锁冲突
python3 spikes/history_decode.py --home ~/.dsh --index          # 工作区 → 会话索引
python3 spikes/history_decode.py --home ~/.dsh --session <id> --compact --limit 20
```

产物目录（均为生成物，可随时删除重建）：

```
spikes/_transcripts/<scenario>.jsonl          双向原始帧（dir=out/in/err，带相对时间戳）
spikes/_transcripts/<scenario>.stderr.log     server 侧日志
spikes/_transcripts/<scenario>.summary.json   结构化结论（文档中引用的证据）
spikes/_transcripts/pocket/…                  pocket profile 的同类产物
spikes/_transcripts/lock.summary.json         锁冲突实验
spikes/_out/*.transcript.json                 归一化后的历史（示例）
spikes/_asar_src/<pkg>/…                      从 app.asar 抽出的只读源码副本（供 grep）
spikes/_logs/*.jsonl                           3 个真实会话解压后的明文（供比对）
```

### 1.3 探测客户端

`spikes/acp_client.py` 是一个零依赖 ACP 客户端（行分隔 JSON-RPC 2.0），把**每一帧**原始报文写入 transcript；`session/request_permission` 由客户端应答，可选 `allow_once / reject_once / cancel`。backend 可以直接复用其协议处理思路（包括错误分类 `classify_error()`）。

---

## 2. 传输与握手

* 传输：**newline-delimited JSON-RPC 2.0**（SDK `ndJsonStream(Writable.toWeb(process.stdout), Readable.toWeb(process.stdin))`，`acp-index.js` 末尾）。stdout 只走协议，日志走 stderr。
* 角色：DSH 是 **agent** 侧；pocket server 是 **client** 侧，必须应答 server 发来的 `session/request_permission` 请求。
* 启动：`DSH_HOME=<home> dsh --profile acp`；**cwd 必须是目标工作区**（原因见 §13.3）。
* 一个连接可并行多个 session，互不影响；同一 session 同时只能有 1 个 prompt（§7.1）。

### 2.1 `initialize` 实测报文

请求：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}
```

响应（0.16–0.41 s）：

```json
{
  "protocolVersion": 1,
  "agentInfo": {"name": "deepseek-harness-acp", "version": "0.0.1"},
  "agentCapabilities": {
    "mcpCapabilities": {"http": true},
    "promptCapabilities": {"image": false, "audio": false, "embeddedContext": false},
    "sessionCapabilities": {"close": {}, "list": {}, "resume": {}}
  },
  "authMethods": []
}
```

要点：

* `sessionCapabilities` 只有 `close / list / resume`；**没有** `load`、fork、delete、modes、commands、plans、terminals、client fs、elicitation（README 明确列为不支持）。调用 `session/load` 得到 `-32601 "Method not found": session/load`（实测）。
* `promptCapabilities.image=false`：`supportsAcpImagePrompts()` 只在「有 attachment store + 部署配置了精确的、声明 image 输入的 provider/model」时才为 true（`acp-index.js` §content）。本机默认 false，图片 prompt 会被拒。移动端一期不做图片输入。
* `authMethods: []` + `authenticate` 返回 `{}`：**ACP 层没有鉴权**，鉴权必须在 pocket server 自己做。

---

## 3. 方法矩阵（wire 事实）

| 方法 | 方向 | 参数（必填 ★） | 结果 | 实测错误 |
|---|---|---|---|---|
| `initialize` | C→S | `protocolVersion`★, `clientCapabilities` | capabilities（§2.1） | — |
| `authenticate` | C→S | `methodId` | `{}` | — |
| `session/new` | C→S | `cwd`★（绝对路径）, `mcpServers`★（数组，可为 `[]`） | `{sessionId, configOptions}` | `-32602 Invalid params`（缺 `mcpServers`/`cwd`，SDK schema 层）；`Invalid params: cwd must be an absolute path: relative/path`；`Invalid params: additionalDirectories is not supported` |
| `session/list` | C→S | `cwd`?（绝对）, `cursor`? | `{sessions:[{sessionId,cwd}], nextCursor?}` | `-32602 Invalid params: cwd must be an absolute path: relative`；`Invalid params: session/list cursor is invalid` |
| `session/resume` | C→S | `sessionId`★, `cwd`★, `mcpServers`?（可省） | `{configOptions}` | `Invalid params: session is not resumable: <id>`；`session cwd does not match: <cwd>`；`session is already active: <id>`；`-32603 Internal error` + `data.details`（provider 未挂载 / 写锁被占） |
| `session/close` | C→S | `sessionId`★ | `{}` | `Invalid params: unknown session: <id>`（已关闭/未知） |
| `session/set_config_option` | C→S | `sessionId`★, `configId`★(`model`\|`reasoning_effort`), `value`★(string) | `{configOptions}` | `unknown model option: …`、`unknown reasoning effort for <p>/<m>: …`、`unknown session config option: …`、`unknown session: …`；非 string → SDK 层 `Invalid params` |
| `session/prompt` | C→S | `sessionId`★, `prompt`★(`ContentBlock[]`) | `{stopReason}` | `a prompt is already in flight for this session`；`unknown session: …`；turn 失败 → `-32603` + `data.details` |
| `session/cancel` | C→S **通知** | `sessionId` | 无响应 | 未知 id 是 no-op（README + 实测） |
| `session/update` | S→C **通知** | `{sessionId, update}` | — | 只出现在 server 主动推送 |
| `session/request_permission` | **S→C 请求** | `{sessionId, toolCall:{toolCallId}, options[]}` | `{outcome:{outcome:"selected",optionId}}` \| `{outcome:{outcome:"cancelled"}}` | 客户端回 `-32601` 会导致该请求失败并降级为拒绝 |

> C→S = client→server。`mcpServers` 在 `session/new` 是**必填**（可传 `[]`），在 `session/resume` 可省略（源码：`params.mcpServers ?? []`）。
> `session/new` 对**不存在的绝对路径 cwd 不报错**（实测：`/…/__definitely_absent__` 建会话成功）→ pocket server 必须自己校验工作区存在性，否则会造出不可用会话（§15）。

---

## 4. 错误码与原因定位

JSON-RPC 2.0 标准码 + ACP SDK 的 `data.details`（关键！）：

```json
{"jsonrpc":"2.0","id":9,"error":{"code":-32603,"message":"Internal error",
  "data":{"details":"no adapter registered for provider \"our-free-model\""}}}
```

| code | 含义 | `message` 形态 | `data.details` | 客户端处理 |
|---|---|---|---|---|
| `-32700` | JSON 解析失败 | — | — | 记录并重发 |
| `-32601` | 方法未实现 | `"Method not found": session/load` | — | 说明该能力不支持，UI 隐藏入口 |
| `-32602` | 参数错误 | `Invalid params: <detail>`；SDK schema 失败时只有 `Invalid params` | — | 视为 4xx，直接回显给调用方 |
| `-32603` | 内部错误 | `Internal error` | **`data.details` 才有真实原因** | 按 details 分类：`session-locked` / `provider-unavailable` / 其它 |
| `-32800` | 请求被取消 | `Request cancelled` | — | 对应客户端主动 abort |

实测 `data.details` 的两个关键字符串（**backend 必须按这两个串做分支**，`spikes/acp_client.py:classify_error` 已实现）：

* `session "<id>" is already owned by an active write handle` → 会话被别的进程（桌面端）写占用 → `session-locked`
* `no adapter registered for provider "<provider>"` → 该 profile 未挂载 provider → `provider-unavailable`

> 源码依据：普通 `Error` 被 SDK 序列化为 `RequestError.internalError({details: message})`（`@agentclientprotocol/sdk/dist/jsonrpc.js` → `errorToResult`）；handler 主动抛的 `RequestError.invalidParams(detail)` 则把 detail 拼进 message（`acp-index.js` 的 `invalidParams()`）。

---

## 5. `session/update` 语义

### 5.1 六种 update（5 种会话内容 + 1 种拓扑，实测，全部标准 ACP）

| `sessionUpdate` | 字段 | 触发（DSH 事件 → update） | 说明 |
|---|---|---|---|
| `agent_thought_chunk` | `messageId`, `content:{type:"text",text}` | `assistant/message` 里的 `reasoning` 块 | 思考内容，UI 折叠展示 |
| `agent_message_chunk` | `messageId`, `content:{type:"text",text}` | `assistant/message` 里的 `text` 块 | **块级**，见 §5.2 |
| `tool_call` | `toolCallId`, `title`(=工具名), `kind:"other"`, `status:"in_progress"`, `rawInput`(解析后的 JSON；解析失败给原字符串) | `tool/call` | 1 次调用 1 条 |
| `tool_call_update` | `toolCallId`, `status:"completed"\|"failed"`, `content:[{type:"content",content:{type:"text",text}}]` | `tool/result` | `isError` → `failed` |
| `usage_update` | `used`(totalTokens), `size`(contextWindow) | `assistant/message` 带 `usage` 且 session 有 contextWindow | 只在两者都有时发 |
| `config_option_update` | `configOptions`（完整状态） | provider 拓扑变化（如插件加载/刷新目录） | **可能在任何时刻插入**，UI 要能热更新模型选择器 |

实测样例（`prompt.summary.json`，真实模型调用，0.65–1.5 s）：

```json
{"sessionUpdate":"agent_message_chunk","messageId":"633db3d1-…","content":{"type":"text","text":"OK"}}
{"sessionUpdate":"usage_update","used":11569,"size":1000000}
```

带工具调用的一次 prompt（`permission`/`config` 场景）：

```
agent_thought_chunk  messageId=A  (推理)
usage_update         used=11783 size=1000000
tool_call            toolCallId=call_00_ipb7…, title="bash", status="in_progress", rawInput={...}
tool_call_update     toolCallId=call_00_ipb7…, status="completed", content=[{text:"hello\n"}]
agent_message_chunk  messageId=B  (最终回答)
usage_update         used=11828 size=1000000
```

### 5.2 是否「块级流式」

**不是 token 级增量，是「已提交内容块」级**：一次 `assistant/message` 事件里每个 `text` / `reasoning` 块各发 1 条 chunk，带完整块文本（`acp-index.js:assistantUpdates()`）。证据：

* 短回答「OK」只有 **1 条** `agent_message_chunk`；
* 同一 `messageId` 下 `agent_thought_chunk` + `agent_message_chunk` 各 1 条（`config.summary.json` 的 updates：两条同 `messageId=bb5d8203…`）；
* `tool-call` 块**不会**变成 `agent_message_chunk`，只由 `tool/call` 事件驱动 `tool_call`。

对 UI 的含义：**按 `messageId` 归并**追加文本，不要假设逐字到达；一条消息可能先思考后正文，也可能只有工具调用没有正文。

### 5.3 顺序与并发

* 每个 session 的 update **串行**投递（`outputTail` 链）。
* `tool_call` 一定先于其 `tool_call_update`。
* 权限请求在**已排队 update 全部 drain 之后**才发出（`record.drainUpdates().then(...)`），所以 UI 一定先看到 `tool_call` 再看到权限弹窗。
* 不同 session 的 update 交错到达，用 `sessionId` 分流。

---

## 6. 权限审批（`session/request_permission`）

### 6.1 触发条件

只有当工具的**审批策略**判定需要人确认时才发。ACP profile 的默认值（`dsh --profile acp --dump-config`，见 `.dev-home/profiles-acp.dump.yml`）：

```yaml
- id: sandbox-policy
  config: { mode: !!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write', workspaceRoot: !!js process.cwd() }
- id: approval
  config: { policy: !!js (process.env.DSH_PERMISSION_MODE ?? 'workspace-write') === 'danger-full-access' ? 'never' : 'ask' }
```

因此：**默认 `workspace-write` 下，工作区内写文件不弹审批**（实测 `/tmp` 写入、工作区内命令均直接完成）；**只有工具主动请求提权（`sandbox_permissions`）时才弹**——例如写工作区外路径（实测：写 `~/dsh-pocket-probe-outside.txt` 触发 1 次请求）。

### 6.2 精确报文（实测）

```json
{"jsonrpc":"2.0","id":2,"method":"session/request_permission","params":{
  "sessionId":"dc61635d-ecf8-43a9-af95-38d84081a077",
  "toolCall":{"toolCallId":"call_00_YYCo2sImEFvjQ22iMqGl3502"},
  "options":[
    {"optionId":"allow-once","name":"Allow once","kind":"allow_once"},
    {"optionId":"reject-once","name":"Reject","kind":"reject_once"}]}}
```

客户端三种应答与工具结果的映射（均实测，`permission.summary.json`）：

| 应答 | tool 结果 |
|---|---|
| `{"outcome":{"outcome":"selected","optionId":"allow-once"}}` | 命令执行（本次因实验环境嵌套 sandbox 仍 EPERM，见 §15.6） |
| `{"outcome":{"outcome":"selected","optionId":"reject-once"}}` | `failed`：`Error: the user rejected escalating this command to "danger-full-access"; it stays denied, so stop and explain instead of working around it` |
| `{"outcome":{"outcome":"cancelled"}}` | `failed`：`Error: approval for escalating to "danger-full-access" was cancelled` |
| 客户端回 `-32601`/超时 | 走 `next()` 或报错，等价于拒绝（不可依赖，务必实现应答） |

注意：`options` 固定只有 `allow-once` / `reject-once` 两项（源码 `acp-index.js` approval/request 段），**没有**「总是允许」；移动端一次点击只授权一次。`toolCall` 只带 `toolCallId`，标题需要 pocket server 用同一 `toolCallId` 关联 `tool_call` update 里的 `title/rawInput` 再展示。

---

## 7. 取消、关闭、进程生命周期

### 7.1 `session/cancel`

* 是**通知**（无 id、无响应），参数 `{sessionId}`。
* 在途 prompt 立刻返回 `{"stopReason":"cancelled"}`（实测：发出 cancel 后 **0.01 s** 返回）。
* 在途工具收到 abort：`tool_call_update status="failed"`，文本如 `Error: tool call aborted before dispatch`（`cancel.summary.json`）。
* 取消后 session **可继续使用**（随后再 prompt 正常 `end_turn`）。
* 未知 sessionId 是 no-op；无在途 prompt 时取消的是「自治工作」（源码 `AcpSession.cancel()`）。
* 同一 session 并发第二个 prompt 会被拒：`-32602 Invalid params: a prompt is already in flight for this session`（实测）。

### 7.2 `session/close`

* 语义：取消 → drain 已排队 update → 等待 agent idle → 排空子代理 → **flush 持久化** → 释放 Agent scope（源码 `AcpSession.close()`）。
* 成功返回 `{}`；**幂等性不成立**：第二次 close 同一 id 得到 `Invalid params: unknown session: <id>`（实测）。
* close 之后：`prompt` / `set_config_option` 都是 `unknown session`；但**日志仍在**，`session/list` 仍列出，`session/resume` 可再次恢复（实测 `close.summary.json`）。

### 7.3 进程退出

* 关闭 stdin（EOF）→ server 静默 quiesce 全部 session 后 **exit 0**（实测 `eof.summary.json`、`new.summary.json` 的 `exit/with-live-session`）。
* 因此 pocket server 重启/退出时应先 `close` 或用 EOF 优雅关闭；`SIGTERM` 不是必需路径（`acp_client.shutdown` 里保留了兜底）。

### 7.4 stopReason 映射（源码 `acp-index.js:turnEndToStopReason`）

| DSH turn 结束原因 | ACP `stopReason` |
|---|---|
| `completed` | `end_turn` |
| `max-tokens` | `max_tokens` |
| `aborted` | `end_turn` |
| `interrupted` | `cancelled` |
| `blocked` / `error` | `end_turn`（但 `error` 实际先被 reject 成 `-32603`，见下） |

`error` 结束的处理：`turn failed: <原因>` 以 `-32603` + `data.details` 返回（`settleAfterQuiescence`），**不会**返回 `end_turn`。实测的真实场景：`our-free-model` 限流时 turn/end reason = `{"kind":"error","error":{"message":"Free usage limit reached…","code":"RATE_LIMIT","status":429}}`（历史日志 `session-3a77d3aa…` seq 345）。UI 必须处理「prompt 以 JSON-RPC error 结束」。

---

## 8. `session/list`：分页、过滤、排序

* 结果条目**只有** `sessionId` 和 `cwd`（源码 `page.map(({sessionId, cwd}) => ...)`）——**没有** title / 时间 / 状态。标题等元数据必须 pocket server 自己补（§12.4）。
* 排序：`createdAt` 降序，同毫秒按 `sessionId` 的 UTF-8 字节序升序。
* 分页：服务端固定页大小 `sessionListPageSize`（profile 配置，默认 **100**，客户端**不能**传 limit）。满页时返回 `nextCursor`。
* 游标算法（源码 `encodeSessionListCursor`/`decodeSessionListCursor`）：

  ```
  cursor = base64url(JSON.stringify([createdAt, sessionId]))   # 无填充
  合法游标必须「重新编码后逐字节相等」，否则 -32602 session/list cursor is invalid
  下一页 = 排序结果中「严格排在游标之后」的条目（createdAt 更小，或相等且 id 字节序更大）
  ```

  实测：用 `session-1e813b14…`（createdAt 1792828522852）作游标，正确返回更旧的 `session-5f856f7f…`、`session-3a77d3aa…`（`list.summary.json` 的 `session/list/cursor-from-newest`）。
* `cwd` 过滤：按**物理目录身份**比较（`realpath`，失败时退回字符串 `resolve` 比较）。相对路径 → `-32602`。
* 过滤规则（源码 `listSessions`）：排除 ① 本进程已激活的 session ② `origin === "subagent"` 或带 `parentSession` 的子代理 session ③ 没有 `cwd` 或 `cwd` 非绝对的 session。**不排除**归档（archived）session；**不排除**其他进程（桌面端）持有的 session。
* 实测：桌面会话可被列出；子代理会话（`cad8b179-…`，`origin:"subagent"`）不出现；`resume` 它得到 `not resumable`。

---

## 9. `session/new` 与 `session/resume`

### 9.1 id 形态（重要）

* ACP `session/new` 生成的 id 是**裸 UUID**：`brandString(randomUUID())`，例如 `29a5e634-d5e2-40f6-822c-9f3fa1046aa6`。
* 桌面端会话常见 `session-<uuid>` 形态。
* **结论：id 必须当作不透明字符串**。不要用前缀/形态判断类型；类型看日志 header 的 `origin` / `parentSession`。
  * 实测反例：`.dev-home/sessions/…/` 下同时存在 `session-*` 目录（桌面/早期）与裸 UUID 目录（ACP 新建 + 子代理），仅凭形态无法区分。

### 9.2 `session/new`

请求：

```json
{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/Volumes/extra1tb/clawdbot-project","mcpServers":[]}}
```

响应：

```json
{"sessionId":"f7c42f4e-957b-453a-b7a8-adb88d763da8",
 "configOptions":[{"id":"model","name":"Model","category":"model","type":"select",
   "currentValue":"[\"deepseek-official\",\"deepseek-v4-flash\"]",
   "options":[
     {"group":"deepseek-official","name":"DeepSeek","options":[
        {"value":"[\"deepseek-official\",\"deepseek-v4-flash\"]","name":"deepseek-v4-flash"},
        {"value":"[\"deepseek-official\",\"deepseek-flash\"]","name":"DeepSeek-V4.1-Flash"},
        {"value":"[\"deepseek-official\",\"deepseek-v4-pro\"]","name":"DeepSeek-V4-Pro","description":"…"}]},
     {"group":"deepseek-account","name":"DeepSeek Account","options":[…]}]},
  {"id":"reasoning_effort","name":"Reasoning effort","category":"thought_level","type":"select",
   "currentValue":"high",
   "options":[{"value":"off","name":"Off"},{"value":"low","name":"Low"},
              {"value":"high","name":"High"},{"value":"max","name":"Max",…}]}]}
```

* 初始 route 来自 profile 配置：`acp` profile 为 `provider: deepseek-official / model: deepseek-v4-flash`（`--dump-config` 可见）。
* `configOptions` 是**完整拓扑快照**，模型按 provider 分组；当前值若不在目录中也会被「补」进对应分组首位（源码 `AcpModelControl.state`）。

### 9.3 `session/resume`

* 入参：`{sessionId, cwd, mcpServers?}`；`cwd` 必须与持久化 header 的 `cwd` **物理目录一致**。
* **恢复的是「最后一次记录的 route」**：`selectionFor(agent.session.requestHeader(), fallback)`，即日志里最后一个 `model/selection` 事件（含 `reasoningEffort`，若当时是 adapter 默认则省略）。
  * 实测：`session-3a77d3aa…`（最后一条 `model/selection` = `deepseek-official/deepseek-flash/low`）resume 后 `configOptions` 的 `model.currentValue="[\"deepseek-official\",\"deepseek-flash\"]"`、`reasoning_effort.currentValue="low"`。
  * 实测：`session-1e813b14…`（最后 route = `our-free-model/deepseek-v4.1-flash-free`）在 `acp` profile 下 resume 失败：`-32603` + `data.details="no adapter registered for provider \"our-free-model\""`；同一会话在 `pocket` profile（挂了该插件）下 resume 成功，`currentValue="[\"our-free-model\",\"deepseek-v4.1-flash-free\"]"`。
* **不回放历史 update**：README「its log is restored without replaying old updates」；要显示历史必须读日志（§11）。
* resume 成功后 `configOptions` 就绪，可立即 `prompt`（实测：resume 后 prompt「只回复 OK」→ `end_turn`，`used=129424/size=1048576`）。

---

## 10. 配置项：`model` 与 `reasoning_effort`

* 两个固定 id：`model`、`reasoning_effort`（源码常量 `MODEL_CONFIG_ID`/`REASONING_CONFIG_ID`）。其他 id → `unknown session config option: <id>`。
* `value` 必须是 **string**（非 string 直接被 SDK schema 拒成 `Invalid params`）。
* `model` 的 value 是**不透明字符串** `JSON.stringify([provider, model])`（紧凑、无空格）：`["deepseek-official","deepseek-flash"]`。**不要自己拼**，必须原样回传上一次 `configOptions` 里给的 `value`。
* `reasoning_effort` 的 value 是 `off|low|high|max` 之一，或 **空字符串 `""` 表示「provider 默认」**（常量 `PROVIDER_DEFAULT_REASONING_VALUE = ""`）；该选项只在当前模型声明了 reasoning 时出现。
* 生效时机：**下一次 turn**。进行中的 turn 用入队时快照的 route（README「a concurrent option change applies to the next turn」）。
* 返回值为**完整** `configOptions`；同时 UI 也可能收到 `config_option_update` 通知。
* 实测错误：`unknown reasoning effort for deepseek-official/deepseek-v4-flash: turbo`、`unknown model option: not-a-model`、`unknown session: …`。

---

## 11. 桌面历史日志格式（v4）

### 11.1 路径与目录编码

```
$DSH_HOME/sessions/<projectKey(cwd)>/<encodeSegment(sessionId)>/session.v4.jsonl.zstd
$DSH_HOME/storages/workspace.json           # 工作区注册表 + 归档/固定状态
```

* `projectKey(cwd)`：`/ \ :` 连续段折叠成单个 `-`，其它非 `[A-Za-z0-9._-]` 码元（含 `~`）转 `~XXXX`（大写十六进制），最后包成 `--<readable>--`，长度上限 251。
  例：`/Volumes/extra1tb/clawdbot-project` → `--Volumes-extra1tb-clawdbot-project--`；含中文目录 → `--…-6-18~65E5~4EA4~6613~77E5~8BC6~5E93--`。
  源码：`dsh-session-persistence-jsonl/lib/index.js:projectKey`（本仓库副本 `spikes/_asar_src/dsh-session-persistence-jsonl/lib/index.js`）。
* `encodeSegment(id)`：把 id 里所有非 `[A-Za-z0-9._-]`（以及 `.`/`..` 特例）转义成 `~XXXX`，避免路径穿越。
* 同一目录内另有 `session.lock`（0 字节，仅作 flock 载体，§13.1）。
* 压缩：zstd（JSONL 多帧，`zstd -dc` 可直接拼接解压）。文件随写入增长；**读它不需要锁**。

### 11.2 逻辑结构

第 1 行 = header：

```json
{"type":"session","version":4,"id":"session-1e813b14-…","createdAt":1792828522852,
 "cwd":"/Volumes/extra1tb/clawdbot-project","isSeeded":false,"delegationDepth":0,"agentPreset":"standard"}
```

子代理会话额外有：`"origin":"subagent"`、`"parentSession":"session-4c9f60f8-…"`、`delegationDepth>=1`（实测 `cad8b179-…`）。

后续每行 = 1 个事件，信封字段：

| 字段 | 说明 |
|---|---|
| `type` | 事件类型（见 §11.3） |
| `seq` | 单调递增整数；**分页/游标的基础** |
| `time` | epoch 毫秒 |
| `data` | 类型相关载荷 |
| `surfaceOp` | 仅 5 种 surface 事件有：`"append"` 或 `{"op":"replace","startSeq":n,"endSeq":m}` |
| `sourceEventSeqs` | 替换事件指向被替换的 seq（实测：`system/message` 替换 seq 8） |

### 11.3 事件类型 → UI 决策

统计口径：**单个关闭后的真实会话** `session-3a77d3aa-2332-4758-9408-ada04dd21ed7`（638 事件，已归档、无进程写，日志稳定可复算）。
复算：`python3 spikes/history_decode.py --home ~/.dsh --stats session-3a77d3aa-2332-4758-9408-ada04dd21ed7`。
另外两个样本（`session-1e813b14…` 21 事件、`cad8b179-…` 子代理会话）呈现完全相同的类型集合。

| 事件类型 | 计数 | surface | 进 UI | 处理 |
|---|---|---|---|---|
| `tool/call` | 83 | ✗ | **是**（工具卡片） | `data.callId` / `name` / `arguments`（JSON 字符串） |
| `tool/result` | 83 | ✅ append | **是** | `data.message.toolCallId` / `isError` / `content` |
| `job/registered` / `job/settled` | 75 / 75 | ✗ | 可选（后台任务条） | `jobId/kind/label/status` |
| `step/start` / `step/end` | 66 / 66 | ✗ | 否（trace） | `turn/step` |
| `session-log-deepseek/delivery-accepted` | 66 | ✗ | 否（遥测） | — |
| `assistant/message` | 65 | ✅ append | **是** | `data.message.content` 块：`reasoning`/`text`/`tool-call`；`data.usage` |
| `user/message` | 13 | ✅ append | **是** | `data.content`（块数组）+ `data.id` |
| `agent/inbox/spliced` | 12 | ✗ | 否（与 `user/message` 同 id，重复） | 入队记录；`removedCount` = 排队撤回 |
| `turn/start` / `turn/end` | 5 / 5 | ✗ | 否（用作 turn 边界与停止原因） | `data.turn`；`data.reason.kind` |
| `model/selection` | 5 | ✗ | 否（可做模型 chip） | `provider/model/reasoningEffort` |
| `system/message` | 3 | ✅ append/replace | **否（必须过滤）** | 单条约 7.8 KB，绝不能塞给手机 |
| `request/header` | 3 | ✗ | **否** | 单条约 39 KB 的工具/模型定义快照 |
| `request/context` | 3 | ✗ | 否 | `provider/model/contextWindow` |
| `session/title` | 2 | ✗ | 标题用 | `data.title` |
| `approval/asked` / `approval/decided` | 1 / 1 | ✗ | 可选（权限卡片） | 按 `data.id` 关联；`outcome` = `allowed-once` |
| `approval/policy` / `sandbox/mode` / `permission/preset` | 1 / 1 / 1 | ✗ | 否（会话级设置） | — |
| `session/end-seed` / `subagent/descriptor` | 1 / 1 | ✗ | 否 | `origin` / `parentSession` 判定用 |
| `session/title-llm-request` | 1 | ✗ | 否 | 标题生成请求 |
| `assistant/attempt` | 1 | ✗ | 否（失败重试流） | `stream[].chunk.finish.reason` |
| `developer/message` | 0（本机样本） | ✅ | 否（同 system） | — |
| `compaction/*` | 0（本机样本） | ✗ | 可选分隔线 | 允许的类型 |

> 完整「本 build 认识的 65 种类型」清单（含 `feedback/*`、`goal/change`、`team/*`、`working-directory/change`、`image/offload` 等）见 `spikes/_asar_src/dsh-session/lib/types/known-event-types.js`。读到**不在清单且无 `ignorable` 标记**的事件应视为「更新版本写的日志」并拒绝解释（源码注释：宁可报错也不静默跳过）。

### 11.4 `surfaceOp` 的两条硬规则

1. **`append`** = 该消息在日志位置进入「模型可见面」且未被替换 → 这是**人类对话记录**的可靠来源。
2. `{"op":"replace",…}` = 仅用于**遮蔽模型可见面**的替换副本。源码注释（`dsh-session/lib/types/surface.js`）：

   > “The model-visible surface deliberately shadows replaced ranges, so it is the wrong source for a human transcript — a landed replacement would erase conversation the user already saw. Append-origin events are that transcript's durable source material; replacement copies stay model-only.”

   → **历史 UI 只渲染 append 事件**，替换副本丢弃（实测 1 条：`system/message` seq 337 替换 seq 8，因模型切换导致系统提示变化；对 UI 无影响，但算法必须实现该分支，否则将来「编辑/删除消息」会产生错乱）。
3. 只有 5 种类型会带 `surfaceOp`：`system/message`、`developer/message`、`user/message`、`assistant/message`、`tool/result`。其余类型永远不带（可用于断言）。

### 11.5 id / 归档 / 固定状态

* 会话 id：不透明字符串；桌面端习惯 `session-<uuid>`，ACP 新建为裸 `<uuid>`（§9.1）。
* 工作区注册表 `~/.dsh/storages/workspace.json`（实测结构）：

  ```json
  {"unit":{"name":"workspace","version":2},
   "global":{"initialized":true,
             "workspaceIds":["66c31bee-…","56406bcd-…","8a5e55a7-…"],
             "archivedSessionIds":["session-5f856f7f-…", …],
             "pinnedSessionIds":[]},
   "tables":{"workspaces":{
     "66c31bee-…":{"path":"/Volumes/extra1tb/clawdbot-project","title":"clawdbot-project",
                   "sessionIds":["session-4c9f60f8-…", …],"createdAt":"2026-10-23T16:08:18.265Z",
                   "updatedAt":"2026-10-26T20:05:15.646Z"}}}}
  ```

  * 工作区列表 = `tables.workspaces[*]`（`path`/`title`/`sessionIds`/时间戳）。
  * `archived` / `pinned` 是**会话 id 级**的全局标记：`global.archivedSessionIds`、`global.pinnedSessionIds`。
  * **重要偏差**：实测该工作区 `sessionIds` 只有 7 条，磁盘上有 13 个会话目录；多出的 6 个正是 ACP/子代理创建的裸 UUID 会话（未登记）。而 `session/list` 会返回这些未登记会话。→ 列表以 `session/list` 为准，元数据（title/archived/pinned）用 `workspace.json` 关联，未登记会话给默认值。

### 11.6 `assistant/message` 的 `stream` 字段

`assistant/message.data.stream` 是 provider 的原始增量流（回放用）。**历史 UI 忽略它**（体积大、含 provider 私有格式），只读 `message.content`。

---

## 12. 历史 → UI 归一化算法

实现：`spikes/history_decode.py`（`normalize()` / `compact()` / `page()`），可直接被 backend 复用的伪代码：

```
读取：zstd -dc <log> | 逐行 json.loads
第 1 行 → header（id/cwd/createdAt/origin/parentSession/delegationDepth/agentPreset）
其余   → events[]（按 seq 升序，日志本身有序）

遍历 events：
  if type in {system/message, developer/message}: 丢弃（计数）
  elif type in {user/message, assistant/message, tool/result}:
        if surfaceOp != "append": 丢弃（替换副本，计数）
        user/message      → item{kind:"user", id, seq, time, blocks, text}
        assistant/message → item{kind:"assistant", id:message.id, seq, turn, step,
                                 text=join(text 块), reasoning=join(reasoning 块),
                                 toolCallIds=[块 id], model, usage}
                            usage → 会话最新 usage
        tool/result       → 关联 tool_calls[toolCallId]：status=completed|failed, output, resultSeq
  elif type == "tool/call": tool_calls[callId] = item{kind:"tool", callId, name,
                                 arguments(JSON 字符串→保留原文/解析展示), status:"running",
                                 seq=该事件 seq, turn, step}
  elif type == "turn/start"/"turn/end": 维护 turns[]（startSeq/endSeq/reason.kind）
  elif type == "session/title": title
  elif type == "model/selection": 当前 route
  elif type == "approval/asked"/"decided": approvals[]（按 id 关联 outcome）
  else: 计数丢弃

合并：items = 用户/助手项 + tool_calls 项；排序 key = (seq, rank)  rank: user 0 < assistant 1 < tool 2
      （同一 seq 会同时承载 assistant 消息与其 tool-call 块 → 必须稳定排序）

分页（page(items, limit, before)）：
  按 seq 分组（一个 seq 组 = 1 条 assistant 消息 + 它触发的工具卡片）
  取「seq < before」的最后一页；nextBefore = 该页第一组的 seq
  → 游标是 seq，不随新消息变化；一页不会被切断在 seq 组中间
```

要点与坑（全部实测）：

1. **不要用 `agent/inbox/spliced` 生成用户消息**：它与 `user/message` 的 `data.id` 相同，重复；只用 surface 事件（`user/message`）。`spliced` 里的 `removedCount` 是「排队中被撤回」的信息，历史不需要。
2. **工具调用配对**：`assistant/message` 里的 `tool-call` 块与 `tool/call` 事件同 `callId`；以 `tool/call` 为准（它有 `turn/step`），仅当缺失时用块兜底合成。`tool/result` 也可能没有对应 `tool/call`（fork 前缀/取消），要允许孤儿结果。
3. **`system/message` 必须过滤**：单条 7.8 KB，且会被替换副本重复；不过滤会让手机端首屏爆量。
4. **`request/header` 必须过滤**：单条 39 KB（工具 JSON Schema 全量）。
5. **工具输出可能极大**：单条 `tool/result` 文本可达数十 KB → 列表页截断（建议 2–4 KB），完整内容走「工具详情」接口按需取。
6. **失败 turn**：`turn/end.reason.kind == "error"` 时历史里仍保留 assistant 的部分内容 → UI 要显示错误条（如 `RATE_LIMIT 429`）。
7. **大日志**：样本最大 726 KB（压缩）/ 1.47 MB（解开，639 事件），流式解析 <10 ms 量级；服务端应在内存里保留「归一化结果 + seq 索引」，按请求切页。
8. **读并发安全**：读日志不加锁（§13.1），可与桌面端同时读；不要写、不要删 `session.lock`。

### 12.1 元数据补全（列表页）

`session/list` 只给 `{sessionId, cwd}`，因此列表页字段建议这样拼（backend 实现）：

| 字段 | 来源 |
|---|---|
| `sessionId` / `cwd` | ACP `session/list` |
| `createdAt` | 日志 header（第 1 行）；也可用 `session.v4.jsonl.zstd` 的 mtime 兜底 |
| `title` | 日志最后一个 `session/title` 事件；没有则截首条 user 文本 |
| `archived` / `pinned` | `workspace.json` 的 `global.archivedSessionIds` / `pinnedSessionIds` |
| `messageCount` / `lastSeq` | 解码时统计（可缓存，按 mtime+size 失效） |
| `activeLocally` | pocket server 自己的 session 表 |
| `lockedElsewhere` | 对 `session.lock` 做非阻塞 flock 试探（§13.2），只读、不修改文件 |
| `resumable` | `origin != subagent` 且无 `parentSession` 且 `cwd` 存在 |

---

## 13. 桌面共存与多进程风险

### 13.1 写锁机制（源码事实）

`dsh-session-persistence-jsonl/lib/index.js` → `SessionWriteLease.acquire`：

* 每个会话目录一个 `session.lock`（0 字节）。POSIX 上对 `open(path,"w")` 的 fd 做**非阻塞 `flock(LOCK_EX|LOCK_NB)`**；抢不到（`EAGAIN/EWOULDBLOCK`）→ `SessionAlreadyOwnedError`。
* 锁**按需获取**：写 handle 第一次「materializing write」（append/flush）时才拿；创建但未写入的会话没有文件足迹。
* **读 handle 从不碰锁**（注释明说 “Readers never touch the lock”）。
* 进程死亡时内核自动释放；没有超时/抢锁机制（注释：故意不给 wedged holder 过期，避免撕裂日志）。
* 锁释放**从不删除** `session.lock`（保留 inode 供后续校验）。

### 13.2 实测：桌面端持有的锁 + 冲突行为

`python3 spikes/lock_probe.py`（`spikes/_transcripts/lock.summary.json`）：

```
~/.dsh/sessions/--Volumes-extra1tb-clawdbot-project--/ 下 13 个会话，其中 3 个 held：
  session-4c9f60f8-…  held(errno=35)   ← 桌面端正在进行的会话
  session-d2953e65-…  held(errno=35)
  cad8b179-…          held(errno=35)   ← 并发子代理会话
其余 10 个 free。

隔离 home 中同一会话：
  baseline（无人持锁）   session/resume → ok
  helper 进程持 flock    session/resume → -32603 Internal error
                         data.details = 'session "<id>" is already owned by an active write handle'
  释放锁后               session/resume → ok
```

同时验证：**读取**被桌面端持锁的实时日志完全正常（`zstd -dc ~/.dsh/…/session-4c9f60f8…/session.v4.jsonl.zstd` 成功）。

### 13.3 结论与实现要求

| 场景 | 行为 | pocket server 必须做的事 |
|---|---|---|
| 桌面端正开着某会话（持锁） | 可列出、可读历史，**不可 resume** | resume 前用 flock 试探该 `session.lock`；被占则进入「只读历史」模式，UI 提示「该会话正在桌面端使用」；用户可选择「新建会话」继续 |
| 桌面端没开该会话 | resume 正常 | 正常恢复，可 prompt |
| pocket 自己同时开两个同会话 | 第二个失败（同 host 内进程内已激活 → `already active`；跨进程 → 锁冲突） | 进程内单例：`sessionId → 唯一 ACP session` 映射 |
| 读历史 | 永远安全（不加锁） | 只读打开日志；**绝不**写日志、不删 `session.lock` |
| ACP 子进程崩溃/被杀 | 内核释放锁，日志可能尾部截断（有 truncation-repair 机制） | 重启后重新起 ACP；历史解析要容忍最后一行不完整 |

**单实例**：ACP server 本身没有跨进程单例限制（可以起多个 `dsh --profile acp`），互不干扰，靠 flock 仲裁写。但 pocket server 自己要保证「一个进程一个 ACP 子进程池」，避免同一会话被两个子进程同时 resume。

### 13.4 沙箱根 = 进程 cwd（重要架构约束）

`--dump-config` 实测：

```yaml
- id: sandbox-policy
  config: { mode: workspace-write, workspaceRoot: !!js process.cwd() }
```

即 ACP 子进程的 bash/fs 沙箱根是**启动该子进程时的 cwd**，不是 `session/new` 的 `cwd`。实测：

* 子进程 cwd = 会话 cwd（`/Volumes/extra1tb/clawdbot-project`）→ `bash pwd` 正常返回；
* 会话 cwd 是子进程 cwd 的子目录（`…/spikes/_sandbox_probe`）→ 首次 `bash` 报
  `Error: sandbox mode "workspace-write" is requested but no sandbox backend is usable on this host; … runner failure: sandbox-exec: sandbox_apply: Operation not permitted`，重试后成功（见 §15 未验证项，属实验环境嵌套 sandbox 现象）。

**实现建议：一个 ACP 子进程只服务一个工作区，且启动 cwd = 该工作区根目录。** 手机端切换工作区时切换子进程（或按工作区懒启动、空闲超时回收）。这样沙箱边界与 `session/new(cwd)` 一致，避免「A 工作区的会话在 B 工作区的沙箱根下运行」这类越权/失败。

---

## 14. Provider 能力：`our-free-model` 会话能否恢复

### 14.1 根因

* `acp` profile 的 bundle 列表只有 `@deepseek-ai/dsh-base` + `@deepseek-ai/dsh-acp-app`（`$DSH_HOME/profiles/acp/package.json`，`--dump-config` 生成）。
* 桌面 profile 的 bundle 额外包含 `dsh-our-free-model`（`~/.dsh/profiles/desktop/package.json`），该插件通过 `ctx.llm.registerAdapter([ROUTE_MAIN, ROUTE_REGION], adapter)` 注册 `our-free-model` provider（`~/.dsh/profiles/desktop/node_modules/dsh-our-free-model/index.js` 第 ~423 行）。
* 因此 ACP profile 下 resume 一个「最后 route = our-free-model」的会话 → `no adapter registered for provider "our-free-model"`。

### 14.2 实测解法（已跑通）

`spikes/setup_dev_home.sh --with-provider` 生成 `pocket` profile：

```json
// .dev-home/profiles/pocket/package.json
{"name":"dsh-profile-pocket","private":true,"dependencies":{},
 "dsh":{"profile":{"bundles":["@deepseek-ai/dsh-base","@deepseek-ai/dsh-acp-app","dsh-our-free-model"]}}}
```

插件包用本地 tgz 安装（生产做法）：

```sh
dsh plugin --profile pocket add file:~/.dsh/profiles/desktop/vendor/dsh-our-free-model-2.5.9.tgz
```

（本次 spike 为免网络/免改动 ~/.dsh，采用 symlink `profiles/pocket/node_modules/dsh-our-free-model → ~/.dsh/profiles/desktop/node_modules/dsh-our-free-model`，只读引用。）

结果（`spikes/_transcripts/pocket/resume.summary.json`）：

```
session/resume/our-free-model    → ok，configOptions.model.currentValue = ["our-free-model","deepseek-v4.1-flash-free"]
session/resume/our-free-model-2  → ok，currentValue = ["our-free-model","combo:recommended-free"]
（acp profile 下同样两次调用均为 -32603 / provider-unavailable）
```

### 14.3 结论与 UI 降级

| 结论 | 说明 |
|---|---|
| **支持** | 只要 pocket 使用的 profile 挂上对应 provider 插件，非 DeepSeek 会话可恢复并继续对话 |
| **需部署配置** | 不是 ACP 默认能力；启动脚本必须允许 `--profile` 选择，并在 README/运行手册里写明「装了哪些 provider」 |
| UI 降级 | ① 模型选择器只显示 `configOptions` 里真实存在的 provider 分组（不要硬编码）；② resume 失败且 `class=provider-unavailable` 时，提示「该会话使用的模型（provider `xxx`）未在当前 profile 中启用」，提供「只读查看历史」+「用默认模型新建会话」两个动作；③ 历史里 `model/selection` 记录可显示「原模型」，让用户理解为何不能续聊 |

> 备选：把 `dsh-our-free-model` 之类插件装进与桌面**共用**的 profile（`dsh plugin --profile acp add …`）——但会改动 `~/.dsh`，本任务禁止；由 captain/用户决定。

---

## 15. 未验证项与实验环境限制（如实记录）

| # | 未验证 / 限制 | 原因 | 生产验证方法 |
|---|---|---|---|
| 15.1 | 图片 prompt（`promptCapabilities.image`） | 本机 acp profile 返回 `image:false`，未构造 image-capable route + attachment store | 一期不做图片输入；二期在部署里配 `input:[text,image]` 模型后重跑 `spikes/acp_probe.py --scenario prompt` 并断言 `image:true` |
| 15.2 | `mcpServers` 挂载（stdio / http） | 未测试；pocket 不需要 MCP | 若需要，`session/new` 传 `mcpServers:[{name,command,args,env}]`，观察启动失败回滚（README 提及 rollback） |
| 15.3 | 超长会话的历史分页（>100 页 / >100 MB） | 本机样本最大 726 KB 压缩 | 用 `spikes/history_decode.py --stats` 在更大日志上压测；必要时改为「按 seq 定位 + 流式跳过」 |
| 15.4 | Windows / Linux 上的锁与 sandbox 差异 | 仅 macOS 实测 | 源码已区分 win32（命名信号量）与 POSIX（flock） |
| 15.5 | 桌面端驱动会话时，pocket 能否收到实时更新 | **不能**：ACP 只上报本进程拥有的 session 的 update；桌面端写的会话对 pocket 不可见（下次 list/读日志才看到） | 定义为产品限制：手机端只对自己发起的 turn 有实时流；桌面端进行中的会话在手机上是「只读历史」 |
| 15.6 | 嵌套 sandbox 导致 bash 偶发失败 | spike 运行在本 agent 的 workspace-write sandbox 内，ACP 孙进程再套 `sandbox-exec` 偶发 `sandbox_apply: Operation not permitted`；同一命令重试通常成功（§13.4 实测） | **生产环境必须从普通终端/launchd 启动 pocket server（不套 DSH sandbox）后重测**；若仍失败，UI 需展示工具错误文本（我们本来就会展示 `tool_call_update` 的 failed 内容） |
| 15.7 | LAN + 手机真机联调 | T1 只做协议层；HTTP/WS 契约已给（api-contract.md） | T3/T4 联调 |
| 15.8 | `session/cancel` 在 agent 空闲时取消「自治工作」 | 未构造自治工作场景 | 源码语义记录在此，产品不依赖 |
| 15.9 | `config_option_update` 的触发时机 | 只观察到「插件加载后 provider 目录刷新」一次 | UI 按「随时可能来」处理 |
| 15.10 | `session/list` 的 `nextCursor` 满页翻页 | 隔离 home 里会话数远小于页大小（默认 100），未触发过 `nextCursor` | 游标算法已按源码逐行核对（§8）；压测时造 >100 个会话，断言第二页 `sessions` 非空且无重复 |

---

## 16. 风险清单

| 风险 | 影响 | 概率 | 缓解 |
|---|---|---|---|
| 会话被桌面端写占用 | resume 失败 | 高 | flock 预检 + 只读历史模式 + 新建会话引导（§13.3） |
| provider 未挂载（`our-free-model` 等） | 老会话不能续聊 | 高 | `pocket` profile 挂插件 + `provider-unavailable` 降级（§14） |
| ACP profile 与桌面 profile 配置漂移（模型默认值、沙箱模式不同） | 手机端行为与桌面不一致 | 中 | 用独立 profile 固定配置；启动脚本打印 `--dump-config` 摘要 |
| 服务端崩溃导致子进程残留 | 端口/进程泄漏 | 中 | pocket server 退出时 EOF 关闭 + 子进程 group kill |
| LAN 暴露无鉴权 | 任意同网设备可驱动你的 agent | **高** | 强制 token（首次配对码），只监听指定网卡，见 api-contract §2 |
| `/opt` 级大日志读放大 | 手机流量/首屏慢 | 中 | 列表只回元数据；transcript 分页 + 工具输出截断 + 详情按需 |
| 手机端误触发危险工具 | 数据损坏 | 中 | 权限弹窗必须显示 `toolName` + `rawInput` 摘要 + `reason`，默认拒绝；同时保持 DSH 自身 `approval=ask` |

---

## 附录 A：证据文件索引

| 文件 | 内容 |
|---|---|
| `spikes/_transcripts/initialize.summary.json` | initialize 全字段、authenticate、未知方法、坏参数 |
| `spikes/_transcripts/new.summary.json` | session/new + 各类参数错误 + EOF 退出 |
| `spikes/_transcripts/prompt.summary.json` | 真实模型调用（1.48 s）+ update 精确 JSON |
| `spikes/_transcripts/config.summary.json` | set_config_option 全成功/失败路径 + 生效于下一 turn |
| `spikes/_transcripts/resume.summary.json` | resume 成功/已激活/cwd 不符/子代理/未知/provider 未挂载 |
| `spikes/_transcripts/list.summary.json` | 过滤、排序、游标（含手工构造游标）/坏游标 |
| `spikes/_transcripts/close.summary.json` | close 语义、close 后 list/resume |
| `spikes/_transcripts/cancel.summary.json` | 取消 0.01 s、并发 prompt 拒绝、取消后可用 |
| `spikes/_transcripts/permission.summary.json` | 权限请求精确报文 + 三种应答的工具结果 |
| `spikes/_transcripts/eof.summary.json` | EOF → exit 0 |
| `spikes/_transcripts/pocket/resume.summary.json` | 挂载 provider 后 our-free-model 恢复成功 |
| `spikes/_transcripts/lock.summary.json` | 桌面端持锁 → resume 失败 + details；解锁恢复 |
| `spikes/_logs/*.jsonl` | 3 个真实会话明文事件流 |
| `spikes/_out/*.transcript.json` | 归一化后的历史（可直接看 UI 结构） |

## 附录 B：源码索引（本 build 0.2.1-rc.3）

| 主题 | asar 路径（权威） | 本仓库只读副本 |
|---|---|---|
| ACP server 全部逻辑 | `app.asar/dsh/node_modules/@deepseek-ai/dsh-acp/lib/index.js` | `spikes/_asar_src/dsh-acp/lib/index.js` |
| 会话日志写入/锁/路径编码 | `…/@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js` | `spikes/_asar_src/dsh-session-persistence-jsonl/lib/index.js` |
| surface / append vs replace | `…/@deepseek-ai/dsh-session/lib/types/surface.js` | `spikes/_asar_src/dsh-session/lib/types/surface.js` |
| 已知事件类型清单 | `…/@deepseek-ai/dsh-session/lib/types/known-event-types.js` | 同左 |
| SDK 错误序列化 | `…/@agentclientprotocol/sdk/dist/jsonrpc.js` | （未复制，用 Electron 读 asar） |
| DSH 自带 webserver（无鉴权、回环） | `…/@deepseek-ai/dsh-host-webserver/lib/index.js` | `spikes/_asar_src/dsh-host-webserver/lib/index.js` |
| 提取脚本 | — | `spikes/dump-asar.mjs` |

> 复现 asar 抽取：
> `ELECTRON_RUN_AS_NODE=1 "/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness" spikes/dump-asar.mjs spikes/_asar_src`
