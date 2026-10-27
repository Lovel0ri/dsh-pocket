# spikes/ — T1 ACP / 历史日志 / 桌面共存 实验包

本目录是 dsh-pocket 的**可复现实验包**。结论与契约见 [`../docs/protocol.md`](../docs/protocol.md) 与
[`../docs/api-contract.md`](../docs/api-contract.md)；这里只放能重跑的探针。

**安全边界**：所有实验使用隔离 `DSH_HOME`（默认 `../.dev-home`）。唯一接触 `~/.dsh` 的操作是
「只读复制」与「非阻塞 flock 试探」（不写、不改、不删）。禁止把 `DSH_HOME` 指向 `~/.dsh` 跑探针。

---

## 1. 文件

| 文件 | 作用 |
|---|---|
| `setup_dev_home.sh` | 建隔离 home：复制凭据 / `workspace.json` / 4 个真实会话；物化 `acp` profile；`--with-provider` 追加挂 `our-free-model` 的 `pocket` profile |
| `acp_client.py` | 零依赖 ACP 客户端（行分隔 JSON-RPC 2.0），逐帧记录 transcript，自动应答权限，`classify_error()` 错误分类 |
| `acp_probe.py` | 10 个场景：`initialize/new/prompt/permission/list/resume/config/cancel/close/eof`（+ `all`），产出 `_transcripts/*.summary.json` |
| `lock_probe.py` | 桌面共存：扫描 `~/.dsh` 的 `session.lock` 持有状态；用 helper 进程持锁前后对比 `session/resume` 行为 |
| `history_decode.py` | 解码 `session.v4.jsonl.zstd`（走 `zstd -dc`）→ 归一化 UI transcript / 工作区索引 / 事件直方图 / 分页 |
| `dump-asar.mjs` | 用 Electron 作为 Node 从 `app.asar` 抽出指定 `@deepseek-ai/*` 包源码（供 grep，输出 `_asar_src/`，只读） |
| `_transcripts/`、`_logs/`、`_out/`、`_asar_src/` | **生成物**，可随时删除重建 |

---

## 2. 快速开始

```sh
cd /Volumes/extra1tb/clawdbot-project/04_tools/dsh-pocket

# 1) 隔离 home（幂等；--with-provider 会额外准备 pocket profile）
sh spikes/setup_dev_home.sh --with-provider

# 2) 全部 ACP 场景（每个场景独立子进程；约 60–90 s，含真实模型调用）
python3 spikes/acp_probe.py --scenario all

# 3) 单场景
python3 spikes/acp_probe.py --scenario resume
python3 spikes/acp_probe.py --scenario prompt --out-dir /tmp/dsh-spike

# 4) provider 恢复实验（our-free-model）
python3 spikes/acp_probe.py --scenario resume --profile pocket --out-dir spikes/_transcripts/pocket

# 5) 桌面共存 / 写锁
python3 spikes/lock_probe.py
python3 spikes/lock_probe.py --scan-only       # 只看当前谁持锁（不启动 ACP）

# 6) 历史
python3 spikes/history_decode.py --home ~/.dsh --index
python3 spikes/history_decode.py --home ~/.dsh --list /Volumes/extra1tb/clawdbot-project
python3 spikes/history_decode.py --home ~/.dsh --stats <session-id>
python3 spikes/history_decode.py --home ~/.dsh --session <session-id> --compact --limit 20 --max-chars 600
python3 spikes/history_decode.py --home ~/.dsh --session <session-id> --before 613 --limit 10
```

### 前置条件

* macOS + 已安装 DeepSeek Harness（`/Applications/DeepSeek Harness.app`）。
* `zstd` CLI（`brew install zstd`）。Python 3.10+，无第三方依赖。
* `~/.dsh/.credentials.yaml` 存在（仅首次复制；否则真实模型调用场景会失败，其它场景仍可跑）。

---

## 3. 场景 → 结论速查

| 场景 | 证明什么 | 证据文件 |
|---|---|---|
| `initialize` | ACP v1 能力矩阵、无鉴权、未知方法 → `-32601`、坏参数 → `-32602` | `_transcripts/initialize.summary.json` |
| `new` | `session/new` 参数约束（`mcpServers` 必填、cwd 必须绝对路径、additionalDirectories 不支持、**不存在的 cwd 也成功**）、EOF 退出 0 | `new.summary.json` |
| `prompt` | 真实模型调用（1.1–1.5 s）、update 精确 JSON、块级而非 token 级 | `prompt.summary.json` |
| `permission` | `session/request_permission` 报文；`allow-once`/`reject-once`/`cancelled` 三种应答对工具结果的影响 | `permission.summary.json` |
| `list` | 排序/过滤、cursor = base64url([createdAt,id])、坏 cursor 拒绝、cwd 过滤 | `list.summary.json` |
| `resume` | 恢复最后 route、already-active / cwd-mismatch / 子代理 / provider 未挂载（`data.details`） | `resume.summary.json` |
| `config` | `set_config_option` 成功/失败全路径、生效于下一 turn、非 string 值被 SDK 拒 | `config.summary.json` |
| `cancel` | 在途 tool 时取消 → 0.01 s 返回 `cancelled`、并发 prompt 被拒、取消后会话可复用 | `cancel.summary.json` |
| `close` | close 语义、close 后 unknown session、日志仍可 list/resume | `close.summary.json` |
| `eof` | 关 stdin → 静默 quiesce → exit 0 | `eof.summary.json` |
| `--profile pocket` | 挂上 provider 插件后 `our-free-model` 会话可恢复 | `pocket/resume.summary.json` |
| `lock_probe` | 桌面端持锁 → resume `-32603` + `already owned by an active write handle`；解锁恢复；读日志始终可用 | `lock.summary.json` |

---

## 4. 已知限制（实验环境）

1. 探针运行在 DSH agent 的 workspace-write 沙箱内，ACP 子进程再套 `sandbox-exec` 偶发
   `sandbox_apply: Operation not permitted`（同一命令重试通常成功）。生产部署应从普通终端/launchd 启动
   pocket server 后复测（详见 `../docs/protocol.md` §15.6）。
2. 真实模型调用依赖 `~/.dsh/.credentials.yaml` 的复制；凭据泄漏风险由 `.dev-home` 权限（600）与
   「不提交」约束控制。
3. `session/new` 会往隔离 home 里**留下真实会话目录**（`_transcripts` 之外的 `../.dev-home/sessions/`）。
   想重置环境：`rm -rf ../.dev-home && sh spikes/setup_dev_home.sh`。
4. `history_decode.py` 只读日志；对**正在被桌面端写入**的日志，读到的是某一时刻的一致前缀（JSONL 行原子追加），
   极端情况下最后一行可能不完整 → 解析器应跳过最后一行解析失败（当前实现会抛错，接产品时按需容错）。
5. `dump-asar.mjs` 只能通过 Electron 运行（Electron 的 fs 才理解 asar）：

   ```sh
   ELECTRON_RUN_AS_NODE=1 "/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness" \
     spikes/dump-asar.mjs spikes/_asar_src
   ```
