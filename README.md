# dsh-pocket

把手机变成 DeepSeek Harness 的遥控器：电脑上跑一个本地服务，手机浏览器打开网页，就能在
**同一个工作区**里聊天、新建会话、看手机自己的历史、确认工具权限。无登录、无配对码，只走局域网。

> 一句话：本机 ACP + 手机 Web UI，个人可信网络自用。

## 能做什么

- **手机聊天**：流式回复（块级推送，不是逐字），代码块 / 表格正常渲染，思考过程可折叠。
- **选工作区**：工作区列表来自 `~/.dsh/storages/workspace.json`（桌面端用过的目录）。
- **新建会话**：在选中工作区里新建一个真正的 DSH 会话（走 ACP，使用默认模型）。
- **手机历史**：手机创建的会话与消息存在本地 SQLite（`~/.dsh-pocket/pocket.db`），随时回看。
- **权限确认**：工具需要提权时手机弹出卡片，点「允许一次」或「拒绝」；不点则 120 秒后自动拒绝。

## 三步开始

1. 在电脑上运行：`./start.sh`
   首次会自动建 `.venv` 并安装依赖（约 15 秒，需要联网一次）。
2. 手机连**同一 Wi-Fi**，扫终端里的二维码，或用打印的地址打开：`http://<电脑局域网IP>:8766`
   （URL 会打印两次：`start.sh` 的提示 + 服务自己的横幅，取其一即可）
3. 选工作区 → 新建会话 → 直接开聊。

只在本机试用（不开手机）：`./start.sh --host 127.0.0.1`，然后浏览器打开 `http://127.0.0.1:8766`。

## 日常使用

| 想做什么 | 怎么做 |
|---|---|
| 切换工作区 | 页面顶部的工作区名称 → 选择另一个目录（切换后 ACP 子进程会按新目录重启） |
| 新建会话 | 会话列表里的「新建 / +」→ 回到输入框开始聊 |
| 看手机历史 | 打开会话列表，点过去的会话即可（数据在本地，不上云） |
| 改模型 / 思考强度 | 手机端暂不支持，固定使用默认模型（后续可加） |
| 确认权限 | 卡片里看清工具名与参数摘要 → 「允许一次」或「拒绝」 |
| 停止服务 | 终端里按 `Ctrl-C` |
| 换端口 | `./start.sh --port 9000` |
| 不连真实 ACP，只调前端 | `./start.sh --mock`（内存后端，没有真实模型） |

其他参数原样透传，完整列表：`.venv/bin/python -m dsh_pocket --help`。

## 运行方式

- 默认使用 **`--profile acp`**：由本服务自己拉起 `dsh --profile acp` 子进程，自动继承 `~/.dsh` 的
  凭据、工作区注册表与模型目录。**不需要**安装 `pocket` profile，也**不需要**桌面端一直开着
  （但 `~/.dsh` 必须已经初始化过，否则看不到工作区）。
- 一个服务进程同一时刻服务**一个工作区**（对应一个 ACP 子进程）；切工作区 = 换子进程，旧会话保留在磁盘。
- 运行状态目录：`~/.dsh-pocket/`（SQLite、ACP stderr 日志），可用 `--state-dir` 改。

## 安全（重要）

- **没有登录、没有鉴权**：能连到这个端口的人 = 能操作你的 DeepSeek Harness（等同本机 shell 权限）。
- 只在**可信的家庭 / 个人局域网**使用；不要做端口转发，不要暴露到公网，不要在公共 Wi-Fi 上开。
- 电脑上的防火墙弹窗请只允许「本地网络」，不要允许「任何网络」。
- 不用时直接 `Ctrl-C` 关掉服务。

## 已知限制

- **暂不显示 / 不恢复桌面端历史**（以后再做）：手机只看自己创建的会话。
- ACP 推送是**块级**的（一段文字整体到达），不是逐字打字机效果。
- 手机端只显示自己创建的会话；桌面端已有会话（包括使用 `our-free-model` 等 provider 的）暂不显示。
- 不支持图片输入；不支持删除 / fork 会话；不支持多工作区并发。
- `--mock` 模式没有真实模型，只用于界面与接口自测。

## 常见问题

- **端口被占用**：`start.sh` 启动前会自检并直接告诉你；换端口 `./start.sh --port 8767`。
  想看是谁占的：`nc -z 127.0.0.1 8766 && echo 被占用`，或 `sudo lsof -i :8766`。
- **手机打不开**：确认同一 Wi-Fi、IP 是否是电脑当前 IP（`ipconfig getifaddr en0`）、
  防火墙是否拦了 Python；公司网络常隔离设备，换手机热点试试。
- **看不到工作区 / `~/.dsh` 权限问题**：确认 `~/.dsh/storages/workspace.json` 存在且可读；
  自定义 home 用 `./start.sh --dsh-home /path/to/.dsh`。
- **ACP 启动失败**：看 `~/.dsh-pocket/acp/*.stderr.log`；确认 `dsh` 可执行、profile 为 `acp`；
  先用 `./start.sh --mock` 排除前端因素。
- **首次安装依赖失败**：网络/代理问题，设置 `HTTPS_PROXY` 后重试，或手动
  `.venv/bin/pip install -e .`；实在不行删掉 `.venv` 重跑 `./start.sh`。

## 目录结构

```
start.sh         唯一入口（建环境 / 探 IP / 打印 URL+二维码 / 启动）
dsh_pocket/      后端：FastAPI + WebSocket + ACP 桥 + SQLite 历史
static/          手机端页面（原生 JS，无需构建）
tests/           pytest（mock 模式，不依赖真实 ACP）
docs/            协议事实（protocol.md）与 API 契约（api-contract.md）
~/.dsh-pocket/   运行时状态：pocket.db、acp/*.stderr.log
```

## 开发与自测

```sh
bash -n start.sh                                  # 脚本语法
.venv/bin/pip install -e '.[dev]'                 # 测试依赖（pytest/httpx）
.venv/bin/python -m pytest -q                     # 后端单测（mock，不碰真实 ACP）
./start.sh --mock --port 8791 &                   # 起 mock 服务（8766 默认端口被占用时换一个）
curl -s http://127.0.0.1:8791/api/v1/health       # {"ok": true, ...}
```
