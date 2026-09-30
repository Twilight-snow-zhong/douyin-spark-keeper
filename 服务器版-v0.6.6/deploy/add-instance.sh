#!/usr/bin/env bash
# 火花助手 · 新增一个「独立实例」（给朋友用 / 自己多空间隔离）
#
# 为什么用多实例而不是"一个网站多用户"：
#   当前版本是**单令牌单空间**——一个令牌进去就能看到全部账号、好友、消息模板与日志。
#   要把"朋友各自独立"做成一个网站内的多用户，需要改认证模型 + 数据分层 + 界面，
#   属于大改造；而**多实例**是最快、最安全、互不影响的做法：一人一套程序、一个端口、一个令牌。
#
# 用法（服务器上，root）：
#   bash /root/douyin-spark/deploy/add-instance.sh friend1 8001
#
# 结果：
#   /root/dsp-friend1            ← 独立程序目录（自己的 data/、自己的账号与登录态）
#   douyin-spark-friend1.service ← 独立 systemd 服务（自动启动、开机自启）
#   http://127.0.0.1:8001        ← 自己的入口（用 Nginx 反代出去 / 或安全组放行该端口）
#   屏幕会打印该实例的访问令牌
#
# 注意：
#   * 每个实例发送时会各自启动一个 Chromium，2GB 内存的机器建议**两个实例以内**，
#     并让它们的定时发送时间错开（或升级到 4GB）。
#   * 本脚本可重复执行：已存在则只重启服务，不会覆盖已有数据与令牌。
set -euo pipefail

NAME="${1:-}"
PORT="${2:-}"

if [[ -z "${NAME}" || -z "${PORT}" ]]; then
  echo "用法: bash $0 <实例名> <端口>" >&2
  echo "例:   bash $0 friend1 8001" >&2
  exit 1
fi
if ! [[ "${NAME}" =~ ^[a-z0-9_-]{1,20}$ ]]; then
  echo "实例名只能是 1~20 位小写字母/数字/下划线/短横线" >&2
  exit 1
fi
if ! [[ "${PORT}" =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
  echo "端口必须是 1024~65535 的数字" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_DIR="/root/dsp-${NAME}"
SERVICE_NAME="douyin-spark-${NAME}"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
PY="${SRC_DIR}/.venv/bin/python"

echo "=== 火花助手 · 新增实例 ${NAME}（端口 ${PORT}）==="
echo "源目录: ${SRC_DIR}"
echo "新目录: ${ROOT_DIR}"

if [[ ! -x "${PY}" ]]; then
  echo "！找不到 ${PY}（主实例的虚拟环境）。请确认主实例已按 deploy.sh 装好。" >&2
  echo "  若主实例目录不是 ${SRC_DIR}，请把本脚本放到主实例的 deploy/ 下运行。" >&2
  exit 1
fi

# 端口占用检查（已在运行的旧实例除外）
if command -v ss >/dev/null 2>&1; then
  if ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}$"; then
    if ! systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
      echo "！端口 ${PORT} 已被其它程序占用，请换一个端口" >&2
      exit 1
    fi
  fi
fi

fresh=0
if [[ ! -f "${ROOT_DIR}/.env" ]]; then
  fresh=1
fi

mkdir -p "${ROOT_DIR}"

# 复制程序文件（不带 data/、.env、browsers/、日志，保证是"干净的新实例"）
if (( fresh == 1 )) || [[ ! -f "${ROOT_DIR}/app.py" ]]; then
  echo "-> 复制程序文件…"
  ( cd "${SRC_DIR}" && tar cf - \
      --exclude='./data' --exclude='./.env' --exclude='./browsers' \
      --exclude='./__pycache__' --exclude='./_refs3' --exclude='./dist' \
      --exclude='./build' --exclude='./*.log' --exclude='./*.zip' \
      . ) | ( cd "${ROOT_DIR}" && tar xf - )
  # 贴纸图标要带上（界面要用）
  mkdir -p "${ROOT_DIR}/data"
  if [[ -d "${SRC_DIR}/data/sticker_icons" ]]; then
    cp -r "${SRC_DIR}/data/sticker_icons" "${ROOT_DIR}/data/" 2>/dev/null || true
  fi
else
  echo "-> 已有程序文件，跳过复制（保留原有 data/ 与 .env）"
fi

# 生成/保留 .env（令牌只在首次生成，之后不动，避免朋友那边突然登录不上）
if (( fresh == 1 )); then
  TOKEN="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  cat > "${ROOT_DIR}/.env" <<EOF
HOST=127.0.0.1
PORT=${PORT}
AUTH_TOKEN=${TOKEN}
EOF
  echo "-> 已生成 .env（令牌已随机生成）"
else
  TOKEN="$(grep -E '^AUTH_TOKEN=' "${ROOT_DIR}/.env" | head -1 | cut -d= -f2- || true)"
  echo "-> 复用已有 .env 与令牌"
fi

# systemd 服务
echo "-> 写入 systemd 服务 ${SERVICE_NAME}"
cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=火花助手（实例 ${NAME}）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT_DIR}
ExecStart=${PY} app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=LANG=C.UTF-8

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}" >/dev/null 2>&1 || systemctl restart "${SERVICE_NAME}"
sleep 3

echo
echo "=== 健康检查 ==="
systemctl is-active "${SERVICE_NAME}" || true
for i in 1 2 3 4 5; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/health" || true)"
  if [[ "${code}" == "200" ]]; then break; fi
  sleep 2
done
echo "本机自检: ${code:-无响应}"

cat <<EOF

=== 实例 ${NAME} 就绪 ===
程序目录 : ${ROOT_DIR}
服务名   : ${SERVICE_NAME}   （开机自启已开启；查看日志：journalctl -u ${SERVICE_NAME} -f）
本机地址 : http://127.0.0.1:${PORT}
访问令牌 : ${TOKEN}

※ 令牌就是唯一钥匙，单独发给这位朋友，别和别人共用（共用 = 互相能看到账号与消息）。

※ 想让他从外网访问，二选一：
   ① 直接开端口：阿里云安全组放行 TCP ${PORT}，访问 http://服务器IP:${PORT}
   ② 用 Nginx 反代（推荐，配好证书后就是 https）：
      cp /root/douyin-spark/deploy/nginx-douyin-spark-8443.conf /etc/nginx/conf.d/dsp-${NAME}.conf
      # 然后改这三处：listen 端口、server_name（如 ${NAME}.你的域名）、proxy_pass http://127.0.0.1:${PORT}
      nginx -t && systemctl reload nginx

※ 内存提醒：每个实例发送时会各起一个 Chromium。2GB 内存的机器建议 2 个实例以内，
   并把各自的"设定每天发送时间"错开（例如 21:00 与 21:40）。
EOF
