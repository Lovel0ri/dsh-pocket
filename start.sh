#!/usr/bin/env bash
# dsh-pocket 唯一入口：建环境（首次）、探测局域网 IP、打印手机 URL 与二维码、启动服务。
#
#   ./start.sh                  # 默认 --host 0.0.0.0 --port 8766（手机可访问）
#   ./start.sh --mock           # 无 ACP 的内存 mock，用于验证前端/接口
#   ./start.sh --port 9000      # 其余参数原样透传给 python -m dsh_pocket
#   ./start.sh --host 127.0.0.1 # 只允许本机访问
#
# 环境变量：DSH_POCKET_HOST / DSH_POCKET_PORT 可改默认值（命令行参数优先）。
# Ctrl-C 停止（SIGINT 直接交给 uvicorn，优雅退出）。
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/.venv"
HOST="${DSH_POCKET_HOST:-0.0.0.0}"
PORT="${DSH_POCKET_PORT:-8766}"

say() { printf '%s\n' "$*"; }
fail() { printf '\n[start.sh] %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- 1
# 解析命令行里的 --host/--port（用于打印正确的手机 URL；参数随后原样透传）。
ARGS=("$@")
i=0
while [ "$i" -lt "${#ARGS[@]}" ]; do
  case "${ARGS[$i]}" in
    --host)   HOST="${ARGS[$((i + 1))]:-$HOST}"; i=$((i + 2)) ;;
    --host=*) HOST="${ARGS[$i]#--host=}"; i=$((i + 1)) ;;
    --port)   PORT="${ARGS[$((i + 1))]:-$PORT}"; i=$((i + 2)) ;;
    --port=*) PORT="${ARGS[$i]#--port=}"; i=$((i + 1)) ;;
    *) i=$((i + 1)) ;;
  esac
done

IS_MOCK=0
PROFILE="acp"
if [ "${#ARGS[@]}" -gt 0 ]; then
  for arg in "${ARGS[@]}"; do
    [ "$arg" = "--mock" ] && IS_MOCK=1
  done
  prev=""
  for arg in "${ARGS[@]}"; do
    [ "$prev" = "--profile" ] && PROFILE="$arg"
    case "$arg" in --profile=*) PROFILE="${arg#--profile=}" ;; esac
    prev="$arg"
  done
fi

# --------------------------------------------------------------------------- 2
# 首次运行：建 venv + 安装依赖（约 15 秒，需要一次联网）。
if [ ! -x "$VENV/bin/python" ]; then
  say "==> 首次运行：创建 .venv 并安装依赖（约 15 秒，需要联网）"
  command -v python3 >/dev/null 2>&1 || fail "找不到 python3。请先安装 Python 3.11+（brew install python@3.12），再重跑 ./start.sh"
  python3 - <<'PY' || fail "Python 版本过低，需要 3.11+。当前：$(python3 -V 2>&1)"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
  python3 -m venv "$VENV" || fail "创建 .venv 失败。检查磁盘空间/权限，或手动执行：python3 -m venv .venv"
  "$VENV/bin/python" -m pip install --disable-pip-version-check --quiet -e . \
    || fail "依赖安装失败。请检查网络/代理（公司网可设 HTTPS_PROXY），或手动执行：$VENV/bin/pip install -e ."
  say "==> 依赖安装完成"
fi

# venv 存在但依赖缺失（例如刚升级过 pyproject）时自动补装一次。
if ! "$VENV/bin/python" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
  say "==> 检测到依赖缺失，重新安装…"
  "$VENV/bin/python" -m pip install --disable-pip-version-check --quiet -e . \
    || fail "依赖安装失败。可删除 .venv 后重跑 ./start.sh，或手动执行：$VENV/bin/pip install -e ."
fi

# --------------------------------------------------------------------------- 3
# 端口预检：被别的程序占用时直接给出可操作提示（uvicorn 的报错藏在日志里，太绕）。
port_available() {
  "$VENV/bin/python" - "$HOST" "$PORT" <<'PYPORT'
import socket, sys

host, port = sys.argv[1], int(sys.argv[2])
probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    probe.bind((host, port))
except OSError as exc:
    print(exc)
    raise SystemExit(1)
finally:
    probe.close()
PYPORT
}

if ! PORT_ERROR=$(port_available); then
  say ""
  fail "端口 $PORT 已被占用（${PORT_ERROR}）。
       换一个端口： ./start.sh --port 8767
       或查占用者： nc -z 127.0.0.1 $PORT && echo 被占用
       或临时指定： DSH_POCKET_PORT=8767 ./start.sh"
fi

# --------------------------------------------------------------------------- 4
# 探测局域网 IPv4：en0 → en1 → 默认路由接口 → Python socket 兜底。
detect_lan_ip() {
  iface_ip=$(ipconfig getifaddr en0 2>/dev/null || true)
  [ -n "$iface_ip" ] && { printf '%s\n' "$iface_ip"; return 0; }
  iface_ip=$(ipconfig getifaddr en1 2>/dev/null || true)
  [ -n "$iface_ip" ] && { printf '%s\n' "$iface_ip"; return 0; }
  route_iface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}' || true)
  if [ -n "${route_iface:-}" ]; then
    iface_ip=$(ipconfig getifaddr "$route_iface" 2>/dev/null || true)
    [ -n "$iface_ip" ] && { printf '%s\n' "$iface_ip"; return 0; }
  fi
  "$VENV/bin/python" - <<'PY' 2>/dev/null || true
import socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    sock.connect(("8.8.8.8", 80))
    print(sock.getsockname()[0])
except OSError:
    pass
finally:
    sock.close()
PY
}

LAN_IP=$(detect_lan_ip || true)

# --------------------------------------------------------------------------- 5
# 启动前提示：模式、手机 URL、安全警告、可选二维码（只编码 URL）。
if [ "$IS_MOCK" = "1" ]; then
  say ""
  say "dsh-pocket（mock 模式：不连 ACP，只用于验证界面/接口）"
else
  say ""
  say "dsh-pocket（真实模式：ACP profile=${PROFILE}）"
fi

URL=""
case "$HOST" in
  127.0.0.1|localhost|::1)
    URL="http://127.0.0.1:$PORT"
    say "电脑打开： $URL"
    say "注意：当前只监听本机（--host $HOST），手机访问不到。要手机使用，去掉 --host 参数重跑。"
    ;;
  *)
    if [ -n "$LAN_IP" ]; then
      URL="http://$LAN_IP:$PORT"
      say "手机打开： $URL   （手机需与本机在同一 Wi-Fi）"
    else
      URL="http://<本机局域网IP>:$PORT"
      say "手机打开： $URL"
      say "未能自动探测局域网 IP，请用「系统设置 → Wi-Fi → 详细信息」里的 IP 替换。"
    fi
    [ "$HOST" != "0.0.0.0" ] && say "监听地址： $HOST:$PORT"
    ;;
esac

say ""
say "【安全】本工具没有登录/鉴权：能连到这个端口的人就能操作你的 DeepSeek Harness。"
say "        只在可信的家庭/个人局域网使用，不要做端口转发、不要暴露到公网。"
say ""

case "$URL" in
  http://*)
    if "$VENV/bin/python" -c 'import qrcode' >/dev/null 2>&1; then
      "$VENV/bin/python" - "$URL" <<'PY' || true
import sys
try:
    import qrcode
    qr = qrcode.QRCode(border=1)
    qr.add_data(sys.argv[1])
    qr.make(fit=True)
    qr.print_ascii(invert=True)
except Exception:
    pass
PY
      say "（扫码即可打开；下面 server 横幅会再打印一次 URL/二维码，取其一）"
    else
      say "（未安装 qrcode，跳过二维码；直接用上面的 URL）"
    fi
    ;;
esac

# --------------------------------------------------------------------------- 6
# 启动。exec 让 uvicorn 直接接管终端：Ctrl-C（SIGINT）走它自己的优雅退出路径。
# 先把透传参数搬到位置参数（空数组在 bash 3.2 + set -u 下不能直接展开）。
if [ "${#ARGS[@]}" -gt 0 ]; then
  set -- "${ARGS[@]}"
else
  set --
fi
exec "$VENV/bin/python" -m dsh_pocket --host "$HOST" --port "$PORT" "$@"
