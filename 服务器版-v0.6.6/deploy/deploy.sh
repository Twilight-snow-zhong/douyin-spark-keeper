#!/usr/bin/env bash
# ============================================================================
# 抖音续火花助手 · 服务器一键部署（Ubuntu 22.04 / 24.04；Debian 11+ 亦可用）
#
# 设计原则：
#   1) 分步执行、每步自检：哪一步失败就停在哪一步，并打印下一步该做什么
#   2) 可重复运行：已完成的步骤会自动跳过（幂等），出错修好后重跑同一命令即可
#   3) 国内网络友好：pip 走清华镜像、Playwright 浏览器走 npmmirror 镜像
#      （服务器带宽常见 1Mbps，用国外源极可能超时）
#   4) 只监听 127.0.0.1：为 nginx 反代做准备（安全，也避免公网直连绕过反代）
#
# 用法：
#   sudo bash deploy/deploy.sh            # 从头跑到尾（已完成的会自动跳过）
#   sudo bash deploy/deploy.sh --step 4   # 只跑第 4 步
#   sudo bash deploy/deploy.sh --list     # 看有哪几步
#
# 步骤：
#   1 环境检查    2 系统依赖    3 Python 环境    4 Chromium
#   5 交换空间    6 配置 .env   7 systemd 服务   8 自检
# ============================================================================
set -uo pipefail

SERVICE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$SERVICE_DIR/.venv"
UNIT_SRC="$SERVICE_DIR/deploy/douyin-spark.service"
UNIT_DST="/etc/systemd/system/douyin-spark.service"
LOG_FILE="$SERVICE_DIR/deploy/deploy.log"
TOTAL_STEPS=8
ONLY_STEP=""

PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
PLAYWRIGHT_CDN="https://cdn.npmmirror.com/binaries/playwright"

# 步骤执行明细记录到 deploy.log（排错用）
exec 3>>"$LOG_FILE"

c_ok()   { printf '\033[32m✅ %s\033[0m\n' "$*"; }
c_bad()  { printf '\033[31m❌ %s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m⚠️  %s\033[0m\n' "$*"; }
c_info() { printf '   %s\n' "$*"; }
log()    { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >&3; }
step_head() { printf '\n\033[1;36m===== 第 %s / %d 步：%s =====\033[0m\n' "$1" "$TOTAL_STEPS" "$2"; }
die() { c_bad "$1"; c_info "排查建议：$2"; c_info "（详细日志：$LOG_FILE）"; exit 1; }

# ---------------------------------------------------------------- 参数
while [ $# -gt 0 ]; do
  case "$1" in
    --step) ONLY_STEP="${2:-}"; shift 2 ;;
    --list)
      echo "1 环境检查  2 系统依赖  3 Python 环境  4 Chromium  5 交换空间  6 配置 .env  7 systemd 服务  8 自检"
      exit 0 ;;
    *) echo "未知参数：$1（可用：--step N / --list）"; exit 1 ;;
  esac
done

want() { [ -z "$ONLY_STEP" ] || [ "$ONLY_STEP" = "$1" ]; }

echo "============================================================"
echo " 抖音续火花助手 · 服务器部署"
echo " 程序目录：$SERVICE_DIR"
echo " 日志：$LOG_FILE"
echo "============================================================"

# ---------------------------------------------------------------- 1 环境检查
if want 1; then
  step_head 1 "环境检查"
  [ "$(id -u)" -eq 0 ] || die "需要 root 权限" "改用：sudo bash deploy/deploy.sh"
  c_ok "root 权限"

  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    c_info "系统：${PRETTY_NAME:-未知}"
    case "${ID:-}" in
      ubuntu|debian) c_ok "系统类型受支持（apt 系）" ;;
      *) c_warn "当前不是 Ubuntu/Debian；本脚本用 apt-get，可能失败"
         c_info "这是阿里云 ECS 常见的坑：若系统是 Alibaba Cloud Linux 3（dnf 系），"
         c_info "建议在控制台「实例 → 更多 → 更换操作系统」换成 Ubuntu 22.04 64位（系统盘会重置）" ;;
    esac
  fi

  command -v apt-get >/dev/null || die "找不到 apt-get" "换 Ubuntu 22.04，或改用 dnf 版脚本"
  command -v systemctl >/dev/null || die "找不到 systemctl（容器里跑不了）" "换一台完整系统的服务器"

  MEM_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
  c_info "内存：${MEM_MB} MB"
  [ "$MEM_MB" -lt 1500 ] && c_warn "内存偏小，Chromium 可能被 OOM 杀掉（第 5 步会建 swap 兜底）"

  DISK_AVAIL=$(df -Pm "$SERVICE_DIR" | awk 'NR==2 {print $4}')
  c_info "可用磁盘：${DISK_AVAIL} MB"
  [ "${DISK_AVAIL:-0}" -lt 4000 ] && c_warn "磁盘不足 4GB：Python 依赖 + Chromium 约需 1GB，建议先清理"

  for f in app.py core/automation.py static/index.html requirements.txt; do
    [ -e "$SERVICE_DIR/$f" ] || die "缺少文件：$f" "确认上传的是完整的「服务器版」目录（不是单独的 app.py）"
  done
  c_ok "程序文件完整"
fi

# ---------------------------------------------------------------- 2 系统依赖
if want 2; then
  step_head 2 "安装系统依赖（apt）"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y >>"$LOG_FILE" 2>&1 || die "apt-get update 失败" "看 $LOG_FILE；公司/内网环境可能需先配好源"
  c_ok "apt 索引更新完成"
  if ! apt-get install -y python3 python3-venv python3-pip ca-certificates curl tzdata fonts-noto-cjk >>"$LOG_FILE" 2>&1; then
    die "系统依赖安装失败" "看 $LOG_FILE 尾部 30 行：tail -30 $LOG_FILE"
  fi
  c_ok "系统依赖就绪：$(python3 --version 2>&1)"
  PYV=$(python3 -c 'import sys;print("%d%d"%(sys.version_info[0],sys.version_info[1]))')
  [ "$PYV" -ge 38 ] || die "python3 版本过低（$PYV）" "Playwright 需要 3.8+；Ubuntu 22.04 自带 3.10"
fi

# ---------------------------------------------------------------- 3 Python 环境
if want 3; then
  step_head 3 "Python 虚拟环境与依赖（走清华镜像）"
  if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV" >>"$LOG_FILE" 2>&1 || die "创建虚拟环境失败" "apt 里是否装了 python3-venv？重跑第 2 步"
    c_ok "虚拟环境已创建：$VENV"
  else
    c_info "虚拟环境已存在，跳过创建"
  fi
  "$VENV/bin/pip" install --upgrade pip -i "$PIP_MIRROR" >>"$LOG_FILE" 2>&1 || c_warn "pip 升级失败（可忽略，继续）"
  if ! "$VENV/bin/pip" install -r "$SERVICE_DIR/requirements.txt" -i "$PIP_MIRROR" >>"$LOG_FILE" 2>&1; then
    die "Python 依赖安装失败" "tail -40 $LOG_FILE；1Mbps 带宽下超时较常见，重跑本步即可（已下载的会复用）"
  fi
  c_ok "依赖安装完成"
  "$VENV/bin/python" -c 'import fastapi, uvicorn, playwright, apscheduler; print("   已导入：fastapi/uvicorn/playwright/apscheduler")' \
    || die "依赖导入失败" "重跑本步；必要时 rm -rf $VENV 后从第 3 步重来"
fi

# ---------------------------------------------------------------- 4 Chromium
if want 4; then
  step_head 4 "安装 Chromium（约 150MB，镜像加速）"
  if "$VENV/bin/python" -c 'from playwright.sync_api import sync_playwright' >/dev/null 2>&1; then
    if PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright" "$VENV/bin/playwright" install --dry-run chromium >/dev/null 2>&1; then
      c_info "检查已有 Chromium…"
    fi
  fi
  export PLAYWRIGHT_DOWNLOAD_HOST="$PLAYWRIGHT_CDN"
  c_info "浏览器下载源：$PLAYWRIGHT_CDN"
  if ! "$VENV/bin/playwright" install --with-deps chromium >>"$LOG_FILE" 2>&1; then
    c_warn "带 --with-deps 安装失败，改用两步重试（常见于非 Ubuntu 系统）"
    "$VENV/bin/playwright" install chromium >>"$LOG_FILE" 2>&1 \
      || die "Chromium 下载失败" "tail -40 $LOG_FILE；可试：apt-get install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libasound2"
  fi
  # 自检：真的能起一次无头浏览器
  if "$VENV/bin/python" - <<'PY' >>"$LOG_FILE" 2>&1
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    pg = b.new_page()
    pg.set_content("<h1>ok</h1>")
    assert pg.inner_text("h1") == "ok"
    b.close()
print("chromium self-test ok")
PY
  then
    c_ok "Chromium 安装成功且能正常启动"
  else
    die "Chromium 已下载但启动失败（多数是缺系统库）" "tail -30 $LOG_FILE，按提示 apt-get install 缺的库后重跑本步"
  fi
fi

# ---------------------------------------------------------------- 5 交换空间
if want 5; then
  step_head 5 "交换空间（2GB，1~2GB 内存跑浏览器必需）"
  if swapon --show 2>/dev/null | grep -q .; then
    c_info "已有 swap，跳过"
    swapon --show | head -3
  else
    if fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 >>"$LOG_FILE" 2>&1; then
      chmod 600 /swapfile && mkswap /swapfile >>"$LOG_FILE" 2>&1 && swapon /swapfile >>"$LOG_FILE" 2>&1
      grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
      c_ok "swap 已创建并写入 /etc/fstab（重启后仍生效）"
    else
      c_warn "swap 创建失败（磁盘不足？）—— 1GB 内存机器上可能导致 Chromium 被杀"
    fi
  fi
fi

# ---------------------------------------------------------------- 6 .env
if want 6; then
  step_head 6 "生成配置文件 .env"
  if [ ! -f "$SERVICE_DIR/.env" ]; then
    TOKEN="$(head -c 24 /dev/urandom | sha256sum | head -c 32)"
    cat > "$SERVICE_DIR/.env" <<EOF
# 网页访问令牌（唯一钥匙，勿外泄）
AUTH_TOKEN=$TOKEN
PORT=8000
# 只监听本机：给 nginx 反代用，同时避免公网直连 8000 绕过反代
HOST=127.0.0.1
EOF
    c_ok "已生成 .env（令牌已随机生成）"
  else
    c_info ".env 已存在，保留原令牌"
    grep -q '^PORT=' "$SERVICE_DIR/.env" || echo 'PORT=8000' >> "$SERVICE_DIR/.env"
    grep -q '^HOST=' "$SERVICE_DIR/.env" || echo 'HOST=127.0.0.1' >> "$SERVICE_DIR/.env"
  fi
  TOKEN_VALUE="$(grep '^AUTH_TOKEN=' "$SERVICE_DIR/.env" | cut -d= -f2- | tr -d '\r\n')"
  [ -n "$TOKEN_VALUE" ] || die ".env 里没有 AUTH_TOKEN" "删掉 $SERVICE_DIR/.env 后重跑第 6 步"
  c_ok "访问令牌：$TOKEN_VALUE"
fi

# ---------------------------------------------------------------- 7 systemd
if want 7; then
  step_head 7 "安装并启动 systemd 服务"
  [ -f "$UNIT_SRC" ] || die "缺少 $UNIT_SRC" "确认 deploy/ 目录一起上传了"
  sed "s|__DIR__|$SERVICE_DIR|g; s|__VENV__|$VENV|g" "$UNIT_SRC" > "$UNIT_DST"
  systemctl daemon-reload
  systemctl enable douyin-spark >>"$LOG_FILE" 2>&1
  systemctl restart douyin-spark
  sleep 3
  if systemctl is-active --quiet douyin-spark; then
    c_ok "服务已启动（开机自启已开启）"
  else
    c_bad "服务启动失败，最近日志："
    journalctl -u douyin-spark --no-pager -n 25
    die "服务没起来" "多为 .venv 路径或依赖问题；修复后重跑第 7 步"
  fi
fi

# ---------------------------------------------------------------- 8 自检
if want 8; then
  step_head 8 "自检"
  PORT="$(grep '^PORT=' "$SERVICE_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '\r\n')"
  PORT="${PORT:-8000}"
  TOKEN_VALUE="$(grep '^AUTH_TOKEN=' "$SERVICE_DIR/.env" | cut -d= -f2- | tr -d '\r\n')"

  if curl -fsS --max-time 8 "http://127.0.0.1:$PORT/api/health" 2>/dev/null | grep -q ok; then
    c_ok "健康检查通过：http://127.0.0.1:$PORT/api/health"
  else
    die "本机访问 $PORT 端口失败" "systemctl status douyin-spark；journalctl -u douyin-spark -n 50"
  fi

  VER=$(curl -fsS --max-time 8 -H "X-Auth-Token: $TOKEN_VALUE" "http://127.0.0.1:$PORT/api/accounts" 2>/dev/null \
        | grep -o '"version"[^,]*' | head -1)
  [ -n "$VER" ] && c_ok "接口与令牌正常，$VER" || c_warn "令牌校验异常（见过 AUTH_TOKEN 是否含特殊字符）"

  HOST_CFG="$(grep '^HOST=' "$SERVICE_DIR/.env" | cut -d= -f2- | tr -d '\r\n')"
  if [ "$HOST_CFG" = "127.0.0.1" ]; then
    c_ok "只监听本机（公网无法直连 $PORT，需经 nginx）"
  else
    c_warn "HOST=$HOST_CFG：公网可直接访问 $PORT，建议改成 127.0.0.1 后重启服务"
  fi
fi

# ---------------------------------------------------------------- 结尾
if [ -z "$ONLY_STEP" ]; then
  LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo
  echo "============================================================"
  echo " 🎉 部署完成"
  echo "------------------------------------------------------------"
  echo " 访问令牌：$TOKEN_VALUE"
  echo " 配置位置：$SERVICE_DIR/.env"
  echo " 管理命令："
  echo "   systemctl status douyin-spark      # 看状态"
  echo "   journalctl -u douyin-spark -f      # 实时日志（Ctrl+C 退出）"
  echo "   systemctl restart douyin-spark     # 重启"
  echo "------------------------------------------------------------"
  echo " 下一步："
  echo " 1) 现在服务只监听 127.0.0.1，公网访问需要 nginx 反代："
  echo "    见 $SERVICE_DIR/域名与HTTPS部署.md"
  echo " 2) 先用 SSH 隧道在本地浏览器打开验证（在自己的电脑上跑）："
  echo "    ssh -L 8000:127.0.0.1:8000 root@<服务器IP>"
  echo "    然后本地打开 http://127.0.0.1:8000 ，输入上面的令牌"
  echo " 3) 网页里：账号管理 → 添加账号 → 「网页内二维码」扫码登录"
  echo "    （服务器无桌面，不要用「弹出浏览器窗口」模式）"
  echo " 4) 配好好友与消息模板 → 先「干跑测试」→ 再「立即发送」"
  echo " 5) 「🔔 通知」里配 webhook（ntfy / Server酱 / PushPlus）推手机"
  echo "============================================================"
  [ -n "$LAN_IP" ] && log "本机 IP：$LAN_IP"
fi
