#!/usr/bin/env bash
# =============================================================================
#  MCSync 服务端一键部署脚本 (Linux)
#  用法:
#    sudo bash deploy.sh                  # 自动选择 Docker / 原生模式
#    sudo bash deploy.sh --mode docker    # 强制 Docker Compose
#    sudo bash deploy.sh --mode native    # 强制 venv + systemd
#    sudo bash deploy.sh --port 9000 --dir /opt/mcsync --keep 10
#    sudo bash deploy.sh --public-ip 1.2.3.4    # 手动指定公网 IP（自动探测不到时用）
#    sudo bash deploy.sh --status         # 查看运行状态
#    sudo bash deploy.sh --uninstall      # 卸载服务（保留数据）
#    sudo bash deploy.sh --uninstall --purge   # 卸载并删除全部数据
# =============================================================================
set -euo pipefail

SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="mcsync"
SERVICE_NAME="mcsync"
INSTALL_DIR="${INSTALL_DIR:-/opt/mcsync}"
DATA_DIR="${DATA_DIR:-${INSTALL_DIR}/data}"
PORT="${PORT:-}"
KEEP_VERSIONS="${KEEP_VERSIONS:-0}"
VERIFY_BLOB="${VERIFY_BLOB:-0}"
MODE="auto"
ACTION="install"
PURGE="no"
PUBLIC_IP="${PUBLIC_IP:-}"
TOKEN="${TOKEN:-}"
ALLOW_IPS="${ALLOW_IPS:-}"

# ---------------------------------------------------------------- 输出助手
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
  C_BLU=$'\033[36m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_DIM=""; C_BOLD=""
fi
log()  { printf '%s[•]%s %s\n' "$C_BLU" "$C_RESET" "$*"; }
ok()   { printf '%s[✓]%s %s\n' "$C_GRN" "$C_RESET" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_YEL" "$C_RESET" "$*"; }
err()  { printf '%s[✗]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
step() { printf '\n%s==>%s %s%s%s\n' "$C_GRN" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"; }
die()  { err "$*"; exit 1; }

# ---------------------------------------------------------------- 参数
while [ $# -gt 0 ]; do
  case "$1" in
    --mode)      MODE="${2:-auto}"; shift 2;;
    -p|--port)   PORT="${2:-}"; shift 2;;
    --token)      TOKEN="${2:-}"; shift 2;;
    --allow-ip)   ALLOW_IPS="${ALLOW_IPS:+$ALLOW_IPS,}${2:-}"; shift 2;;
    --show-token) ACTION="show-token"; shift;;
    --rotate-token) ACTION="rotate-token"; shift;;
    --revoke-clients) ACTION="revoke-clients"; shift;;
    -d|--dir)    INSTALL_DIR="${2:-/opt/mcsync}"; DATA_DIR="${INSTALL_DIR}/data"; shift 2;;
    -k|--keep)   KEEP_VERSIONS="${2:-0}"; shift 2;;
    --verify)    VERIFY_BLOB="1"; shift;;
    --status)    ACTION="status"; shift;;
    --uninstall) ACTION="uninstall"; shift;;
    --purge)     PURGE="yes"; shift;;
    --public-ip) PUBLIC_IP="${2:-}"; shift 2;;
    -h|--help)   sed -n '2,14p' "$0"; exit 0;;
    *)           die "未知参数: $1 （用 --help 查看用法）";;
  esac
done

# ---------------------------------------------------------------- 环境检查
[ "$(uname -s)" = "Linux" ] || die "本脚本仅支持 Linux 服务端"
command -v python3 >/dev/null 2>&1 || die "未找到 python3，请先安装"
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; else die "请用 root 运行（或安装 sudo）"; fi
fi

have_cmd() { command -v "$1" >/dev/null 2>&1; }
compose_cmd() {
  if docker compose version >/dev/null 2>&1; then echo "docker compose";
  elif have_cmd docker-compose; then echo "docker-compose";
  else echo ""; fi
}
DOCKER_SUDO=""
detect_docker() {
  have_cmd docker || return 1
  if docker info >/dev/null 2>&1; then
    DOCKER_SUDO=""                      # 当前用户已在 docker 组
  elif [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; then
    DOCKER_SUDO="$SUDO"
  else
    return 1
  fi
  [ -n "$(compose_cmd)" ] || return 1
  return 0
}
local_ip() {
  local v=""
  v=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1 || true)
  if [ -z "$v" ]; then
    v=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
  fi
  printf '%s' "$v"
}
public_ip() { curl -fsS --max-time 4 https://api.ipify.org 2>/dev/null || echo ""; }
is_public_ip() {
  [ -n "$1" ] || return 1
  case "$1" in
    # 私有地址段 RFC1918 / 环回 / 链路本地 / CGNAT 100.64.0.0-10 / 未指定
    10.*)                            return 1;;
    172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 1;;
    192.168.*)                       return 1;;
    127.*|0.*|255.*)                 return 1;;
    169.254.*)                       return 1;;
    100.6[4-9].*|100.[7-9][0-9].*|100.1[0-1][0-9].*|100.12[0-7].*) return 1;;
  esac
  # 必须是 x.x.x.x 形式
  echo "$1" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || return 1
  return 0
}

# 清掉旧版本遗留的服务进程：老服务可能还占着旧端口（如 8650），
# 会让"脚本打印 36854 但实际连 8650 才通"这类问题反复出现。
kill_old_servers() {
  if have_cmd systemctl; then
    $SUDO systemctl stop "${SERVICE_NAME}" >/dev/null 2>&1 || true
  fi
  local pids=""
  pids=$(ps -eo pid,args 2>/dev/null | grep -E 'uvicorn app:app' | grep -v grep | awk '{print $1}' || true)
  for pid in $pids; do
    [ -n "$pid" ] || continue
    $SUDO kill -9 "$pid" >/dev/null 2>&1 || true
  done
  sleep 1
}

# 探测服务**真实**监听的端口。不信任配置值，只相信实际 health 响应。
probe_port() {
  if [ -n "${PORT}" ] && curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo "${PORT}"; return 0
  fi
  local ports=""
  ports=$(ss -ltn 2>/dev/null | awk '{print $4}' | grep -oE '[0-9]+$' | sort -un || true)
  if [ -z "$ports" ]; then
    ports=$(netstat -ltn 2>/dev/null | awk '{print $4}' | grep -oE '[0-9]+$' | sort -un || true)
  fi
  local lp=""
  for lp in $ports; do
    case "$lp" in
      22|80|443|3306|5432|6379|11211) continue;;
    esac
    if curl -fsS --max-time 2 "http://127.0.0.1:${lp}/api/health" 2>/dev/null | grep -q 'mcsync'; then
      echo "$lp"; return 0
    fi
  done
  echo ""
}

detect_public_ip() {
  if [ -n "${PUBLIC_IP}" ]; then echo "${PUBLIC_IP}"; return 0; fi

  # 云厂商元数据（内网可达，最快最准，阿里云/腾讯云/AWS 均支持）
  local m=""
  for u in http://100.100.100.200/latest/meta-data/public-ipv4 \
           http://metadata.tencentyun.com/latest/meta-data/public-ipv4 \
           http://169.254.169.254/latest/meta-data/public-ipv4; do
    m="$(curl -fsS --max-time 2 "$u" 2>/dev/null | tr -d '[:space:]' || true)"
    is_public_ip "$m" && { echo "$m"; return 0; }
  done

  # 公网回显服务，逐个尝试
  for u in https://api.ipify.org https://ifconfig.me/ip https://myip.ipip.net \
           https://ip.3322.net https://api.myip.com; do
    m="$(curl -fsS --max-time 3 "$u" 2>/dev/null | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -1 || true)"
    is_public_ip "$m" && { echo "$m"; return 0; }
  done
  echo ""
}

# ============================================================================
#  卸载
# ============================================================================
do_uninstall() {
  step "卸载 MCSync 服务"
  if [ -f "${INSTALL_DIR}/.mode" ] && [ "$(cat "${INSTALL_DIR}/.mode")" = "docker" ]; then
    ( cd "${INSTALL_DIR}" && ${DOCKER_SUDO:-$SUDO} "$(compose_cmd)" down --remove-orphans ) 2>/dev/null || true
    ok "Docker 容器已停止"
  fi
  if have_cmd systemctl && systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
    $SUDO systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    $SUDO systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    $SUDO rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    $SUDO systemctl daemon-reload 2>/dev/null || true
    ok "systemd 服务已移除"
  fi
  $SUDO pkill -f "uvicorn app:app" 2>/dev/null || true
  if [ "$PURGE" = "yes" ]; then
    $SUDO rm -rf "${INSTALL_DIR}"
    warn "已删除 ${INSTALL_DIR}（含全部云端数据）"
  else
    warn "云端数据保留在 ${DATA_DIR}，如需彻底删除请手动执行: sudo rm -rf ${INSTALL_DIR}"
  fi
  ok "卸载完成"
  exit 0
}

# ============================================================================
#  状态
# ============================================================================
do_status() {
  if [ -z "${PORT}" ] && [ -f "${ENV_FILE}" ]; then
    PORT=$(grep -E '^MCSYNC_PORT=' "${ENV_FILE}" | cut -d= -f2 || true)
  fi
  PORT="${PORT:-8650}"
DOMAIN="${DOMAIN:-}"
  step "MCSync 运行状态"
  if [ -f "${INSTALL_DIR}/.mode" ] && [ "$(cat "${INSTALL_DIR}/.mode")" = "docker" ] && detect_docker; then
    ( cd "${INSTALL_DIR}" && $DOCKER_SUDO "$(compose_cmd)" ps ) || true
  fi
  if have_cmd systemctl; then
    systemctl is-active --quiet "${SERVICE_NAME}" && ok "systemd 服务运行中" || warn "systemd 服务未运行"
    $SUDO systemctl status "${SERVICE_NAME}" --no-pager -l 2>/dev/null | head -20 || true
  fi
  if curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    ok "健康检查通过: http://127.0.0.1:${PORT}/api/health"
    curl -fsS "http://127.0.0.1:${PORT}/api/stats" 2>/dev/null | head -c 400 || true
    echo
  else
    warn "端口 ${PORT} 无响应"
  fi
  RP="$(probe_port)"
  printf '    实际监听端口: %s\n' "${RP:-未探测到服务}"
  printf '\n%s  客户端服务器地址：%s\n' "$C_BOLD" "$C_RESET"
  printf '    公网: http://%s:%s\n' "$(detect_public_ip || echo '未获取到')" "$PORT"
  printf '    内网: http://%s:%s   (仅同一内网)\n' "$(local_ip)" "$PORT"
  exit 0
}

if [ "$ACTION" = "uninstall" ]; then do_uninstall; fi
if [ "$ACTION" = "status" ]; then do_status; fi

# ============================================================================
#  选择模式
# ============================================================================
step "环境检测"
if [ "$MODE" = "auto" ]; then
  if detect_docker; then MODE="docker"; log "检测到可用的 Docker + Compose → 使用容器模式";
  else MODE="native"; log "未检测到可用 Docker → 使用原生 venv + systemd 模式"; fi
elif [ "$MODE" = "docker" ]; then
  detect_docker || die "指定 --mode docker，但 Docker 或 Compose 不可用"
fi
ok "部署模式: ${MODE}"

# ============================================================================
#  Docker 模式
# ============================================================================
deploy_docker() {
  step "以 Docker Compose 方式部署"
  $SUDO mkdir -p "${INSTALL_DIR}" "${DATA_DIR}"
  $SUDO cp -f "${SERVER_DIR}/app.py" "${SERVER_DIR}/store.py" "${SERVER_DIR}/protocol.py" \
              "${SERVER_DIR}/auth.py" "${SERVER_DIR}/accounts.py" "${SERVER_DIR}/requirements.txt" \
              "${SERVER_DIR}/Dockerfile" \
              "${SERVER_DIR}/docker-compose.yml" "${INSTALL_DIR}/"
  $SUDO cp -rf "${SERVER_DIR}/static" "${INSTALL_DIR}/"
  cat > /tmp/mcsync.env <<EOF
HOST_PORT=${PORT}
DATA_DIR=${DATA_DIR}
VERIFY_BLOB=${VERIFY_BLOB}
KEEP_VERSIONS=${KEEP_VERSIONS}
MCSYNC_TOKEN=${TOKEN}
MCSYNC_ALLOW_IPS=${ALLOW_IPS}
MCSYNC_AUTH=1
EOF
  $SUDO mv /tmp/mcsync.env "${INSTALL_DIR}/.env"
  echo "docker" | $SUDO tee "${INSTALL_DIR}/.mode" >/dev/null

  step "构建镜像（首次约 1-3 分钟）"
  ( cd "${INSTALL_DIR}" && $DOCKER_SUDO "$(compose_cmd)" up -d --build )
  sleep 3
  ( cd "${INSTALL_DIR}" && $DOCKER_SUDO "$(compose_cmd)" ps )
}

# ============================================================================
#  原生模式
# ============================================================================
deploy_native() {
  step "以 Python venv + systemd 方式部署"
  PYVER=$(python3 -c 'import sys;print("%d.%d"%(sys.version_info[0],sys.version_info[1]))')
  python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' \
    || die "需要 Python 3.9+，当前 ${PYVER}"
  ok "Python ${PYVER}"

  $SUDO mkdir -p "${INSTALL_DIR}" "${DATA_DIR}"
  $SUDO cp -f "${SERVER_DIR}/app.py" "${SERVER_DIR}/store.py" "${SERVER_DIR}/protocol.py" \
              "${SERVER_DIR}/auth.py" "${SERVER_DIR}/accounts.py" \
              "${SERVER_DIR}/requirements.txt" "${INSTALL_DIR}/"
  $SUDO cp -rf "${SERVER_DIR}/static" "${INSTALL_DIR}/"
  echo "native" | $SUDO tee "${INSTALL_DIR}/.mode" >/dev/null

  # 漏拷一个模块服务就起不来（历史上 accounts.py 就漏过一次，
  # 表现为 uvicorn 报 ModuleNotFoundError 然后反复重启）。这里显式校验。
  for m in app store protocol auth accounts; do
    if [ ! -f "${INSTALL_DIR}/${m}.py" ]; then
      echo "[✗] 缺少模块 ${m}.py，请检查 ${SERVER_DIR} 是否完整"
      exit 1
    fi
  done
  ok "服务端模块已就位（app / store / protocol / auth / accounts）"

  # ------------------------------------------------------------ pip 是否可用
  # 注意：不能只用 "python3 -c 'import venv'" 判断，venv 模块即使可导入，
  # 缺 ensurepip 时创建仍会失败（Debian/Ubuntu 把它拆成了 python3.X-venv 包）。
  if ! python3 -m pip --version >/dev/null 2>&1; then
    warn "python3 没有可用的 pip，尝试安装…"
    if have_cmd apt-get;  then $SUDO apt-get update -qq && $SUDO apt-get install -y -qq python3-pip
    elif have_cmd dnf;    then $SUDO dnf install -y python3-pip
    elif have_cmd yum;    then $SUDO yum install -y python3-pip
    elif have_cmd apk;    then $SUDO apk add py3-pip
    elif have_cmd pacman; then $SUDO pacman -Sy --noconfirm python-pip
    elif have_cmd zypper; then $SUDO zypper --non-interactive install python3-pip
    fi
  fi

  # ------------------------------------------------------------ 运行环境（多级兜底）
  step "准备 Python 运行环境"

  VENV_DIR="${INSTALL_DIR}/venv"
  VENV_PY="${VENV_DIR}/bin/python"

  # 关键：venv 创建成功 ≠ pip 可用。Debian/Ubuntu 上 python3.X-venv 与 pip 的
  # 捆绑 wheel 是分开打包的，常见“venv 建好了但 No module named pip”。
  # 因此每一步都必须用 -m pip --version 实测，而不是只看解释器在不在。
  venv_has_pip() {
    [ -x "${VENV_PY}" ] || return 1
    $SUDO "${VENV_PY}" -m pip --version >/dev/null 2>&1
  }

  create_venv() {
    $SUDO rm -rf "${VENV_DIR}" 2>/dev/null
    $SUDO python3 -m venv "${VENV_DIR}" >/tmp/mcsync-venv.log 2>&1
    [ -x "${VENV_PY}" ] || return 1
    return 0
  }

  # 为已有/新建的 venv 补出 pip：ensurepip → get-pip.py
  ensure_pip() {
    venv_has_pip && return 0
    log "venv 内缺少 pip，尝试自举…"

    $SUDO "${VENV_PY}" -m ensurepip --upgrade >/tmp/mcsync-pip.log 2>&1
    venv_has_pip && { ok "已用 ensurepip --upgrade 装好 pip"; return 0; }

    $SUDO "${VENV_PY}" -m ensurepip --default-pip >>/tmp/mcsync-pip.log 2>&1
    venv_has_pip && { ok "已用 ensurepip --default-pip 装好 pip"; return 0; }

    log "ensurepip 不可用，改用 get-pip.py 在线安装 pip…"
    if have_cmd curl; then
      $SUDO curl -fsSL --max-time 30 https://bootstrap.pypa.io/get-pip.py -o /tmp/mcsync-get-pip.py
    elif have_cmd wget; then
      $SUDO wget -q --timeout=30 -O /tmp/mcsync-get-pip.py https://bootstrap.pypa.io/get-pip.py
    fi
    if [ -s /tmp/mcsync-get-pip.py ]; then
      $SUDO "${VENV_PY}" /tmp/mcsync-get-pip.py -q >>/tmp/mcsync-pip.log 2>&1
      venv_has_pip && { ok "已用 get-pip.py 装好 pip"; return 0; }
    fi
    return 1
  }

  # 只在这一步判定成功：解释器存在 且 pip 可用
  try_venv() {
    if [ ! -x "${VENV_PY}" ]; then
      create_venv || return 1
    fi
    ensure_pip || return 1
    return 0
  }

  fixup_venv_pkgs() {
    log "正在补齐 venv / pip 相关系统包…"
    if have_cmd apt-get; then
      $SUDO apt-get update -qq
      $SUDO apt-get install -y -qq "python${PYVER}-venv" 2>/dev/null \
        || $SUDO apt-get install -y -qq python3-venv 2>/dev/null || true
      $SUDO apt-get install -y -qq python3-pip 2>/dev/null || true
    elif have_cmd dnf; then
      $SUDO dnf install -y python3-pip python3-devel 2>/dev/null || true
    elif have_cmd yum; then
      $SUDO yum install -y python3-pip python3-devel 2>/dev/null || true
    elif have_cmd apk; then
      $SUDO apk add py3-pip python3-dev 2>/dev/null || true
    elif have_cmd pacman; then
      $SUDO pacman -Sy --noconfirm python-pip 2>/dev/null || true
    elif have_cmd zypper; then
      $SUDO zypper --non-interactive install python3-pip python3-devel 2>/dev/null || true
    fi
  }

  PYBIN=""; ENV_KIND=""; PIP_BREAK=""

  # 上次失败可能留下一个“有解释器、没 pip”的残废 venv，先修再判断
  if [ -x "${VENV_PY}" ] && ! venv_has_pip; then
    warn "发现残留的无 pip 虚拟环境，正在修复…"
    fixup_venv_pkgs
    ensure_pip || { warn "修复失败，重建虚拟环境"; $SUDO rm -rf "${VENV_DIR}" 2>/dev/null; }
  fi

  if try_venv; then
    PYBIN="${VENV_PY}"; ENV_KIND="venv"
    ok "虚拟环境已就绪（venv, pip 可用）"
  else
    warn "venv 不可用：$(tail -1 /tmp/mcsync-pip.log 2>/dev/null | tr -d '\r\n')\
$(tail -1 /tmp/mcsync-venv.log 2>/dev/null | tr -d '\r\n')"
    fixup_venv_pkgs
    if try_venv; then
      PYBIN="${VENV_PY}"; ENV_KIND="venv"
      ok "补齐系统包后创建成功"
    else
      warn "venv 仍不可用，尝试 virtualenv 兜底…"
      python3 -m pip install --user -q virtualenv >/dev/null 2>&1 \
        || $SUDO python3 -m pip install -q --break-system-packages virtualenv >/dev/null 2>&1 \
        || $SUDO python3 -m pip install -q virtualenv >/dev/null 2>&1 || true
      $SUDO rm -rf "${VENV_DIR}" 2>/dev/null
      if python3 -m virtualenv --version >/dev/null 2>&1; then
        $SUDO python3 -m virtualenv -q "${VENV_DIR}" >/dev/null 2>&1 || true
      fi
      if [ -x "${VENV_PY}" ] && $SUDO "${VENV_PY}" -m pip --version >/dev/null 2>&1; then
        PYBIN="${VENV_PY}"; ENV_KIND="virtualenv"
        ok "已用 virtualenv 创建运行环境"
      fi
    fi
  fi

  # 最后兜底：装到系统 Python（PEP 668 环境需要 --break-system-packages）
  if [ -z "${PYBIN}" ]; then
    warn "无法创建隔离环境，改为把依赖装入系统 Python（最后兜底）"
    PYBIN="$(command -v python3)"; ENV_KIND="system"
    if python3 -m pip install --help 2>/dev/null | grep -q "break-system-packages"; then
      PIP_BREAK="--break-system-packages"
    fi
    if ! python3 -m pip --version >/dev/null 2>&1; then
      log "系统 Python 也缺 pip，尝试 get-pip.py…"
      if have_cmd curl; then
        $SUDO curl -fsSL --max-time 30 https://bootstrap.pypa.io/get-pip.py -o /tmp/mcsync-get-pip.py
      elif have_cmd wget; then
        $SUDO wget -q --timeout=30 -O /tmp/mcsync-get-pip.py https://bootstrap.pypa.io/get-pip.py
      fi
      if [ -s /tmp/mcsync-get-pip.py ]; then
        $SUDO python3 /tmp/mcsync-get-pip.py -q ${PIP_BREAK} >/dev/null 2>&1 || true
      fi
    fi
  fi

  step "安装依赖（${ENV_KIND}）"
  if ! "${PYBIN}" -m pip --version >/dev/null 2>&1; then
    die "pip 始终不可用，请手动安装后重试：
       sudo apt update && sudo apt install -y python${PYVER}-venv python3-pip
       sudo bash ${SERVER_DIR}/deploy.sh --mode native"
  fi
  $SUDO "${PYBIN}" -m pip install -q ${PIP_BREAK} --upgrade pip wheel 2>&1 | tail -3 || true
  $SUDO "${PYBIN}" -m pip install ${PIP_BREAK} -r "${INSTALL_DIR}/requirements.txt" \
    || die "依赖安装失败，可手动执行：
       sudo ${PYBIN} -m pip install ${PIP_BREAK} -r ${INSTALL_DIR}/requirements.txt"
  ok "依赖安装完成（${ENV_KIND}）"
  echo "${ENV_KIND}" | $SUDO tee "${INSTALL_DIR}/.envkind" >/dev/null

  # 运行用户：优先创建独立的低权限用户
  RUN_USER="root"
  if id -u "${APP_NAME}" >/dev/null 2>&1; then RUN_USER="${APP_NAME}";
  else
    if $SUDO useradd -r -s /usr/sbin/nologin -d "${INSTALL_DIR}" "${APP_NAME}" 2>/dev/null; then
      RUN_USER="${APP_NAME}"; ok "已创建运行用户 ${APP_NAME}"
    fi
  fi
  $SUDO chown -R "${RUN_USER}" "${INSTALL_DIR}" 2>/dev/null || true

  if have_cmd systemctl && [ -d /run/systemd/system ]; then
    step "写入 systemd 服务"
    log "清理可能残留的旧服务进程…"
    kill_old_servers
    cat > /tmp/mcsync.service <<EOF
[Unit]
Description=MCSync Minecraft Save Sync Server
Documentation=https://mcsync.local
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
# 关键变量再显式写一遍：EnvironmentFile 若因权限/路径问题未生效，
# 服务会回退到 app.py 的默认端口 8650，导致"脚本报 A 端口、实际跑 B 端口"。
Environment=MCSYNC_DATA=${DATA_DIR}
Environment=MCSYNC_PORT=${PORT}
Environment=MCSYNC_TOKEN=${TOKEN}
Environment=MCSYNC_AUTH=1
Environment=MCSYNC_ALLOW_IPS=${ALLOW_IPS}
Environment=MCSYNC_VERIFY_BLOB=${VERIFY_BLOB}
Environment=MCSYNC_KEEP_VERSIONS=${KEEP_VERSIONS}
Environment=TZ=Asia/Shanghai
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYBIN} -m uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips *
Restart=always
RestartSec=3
LimitNOFILE=65535
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    $SUDO mv /tmp/mcsync.service /etc/systemd/system/${SERVICE_NAME}.service
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now ${SERVICE_NAME}
    sleep 2
    $SUDO systemctl --no-pager status ${SERVICE_NAME} | head -12 || true
    ok "服务已设为开机自启"
  else
    warn "未检测到 systemd，改用 nohup 后台启动（不会开机自启）"
    $SUDO env $(grep -v '^#' "${ENV_FILE}" | xargs) \
      nohup "${PYBIN}" -m uvicorn app:app --host 0.0.0.0 --port "${PORT}" \
      >> "${INSTALL_DIR}/mcsync.log" 2>&1 &
    sleep 2
  fi
}

# ============================================================================
# ============================================================================
#  安全凭据：16 位随机口令 + 随机高位端口，持久化到 0600 环境文件
# ============================================================================
ENV_FILE="${INSTALL_DIR}/mcsync.env"

gen_token() {
  # 生成 16 位口令（62 字符集，约 95 bit 熵）
  # 注意：不能写 "tr -dc ... < /dev/urandom | head -c 16"。
  # head 读够 16 字节就关管道，tr 会收到 SIGPIPE(141)，在 set -o pipefail 下
  # 整条管道返回非 0，命令替换失败后 set -e 会立刻静默终止脚本。
  local s=""
  if have_cmd python3; then
    s=$(python3 -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(16)))' 2>/dev/null || true)
  fi
  if [ ${#s} -lt 16 ]; then
    # 兜底：dd 读满一块再交给 tr 过滤，管道不会被提前关闭
    local chunk=""
    local guard=0
    while [ ${#s} -lt 16 ] && [ ${guard} -lt 20 ]; do
      guard=$((guard + 1))
      chunk=$(dd if=/dev/urandom bs=256 count=1 2>/dev/null | LC_ALL=C tr -dc 'A-Za-z0-9' || true)
      s="${s}${chunk}"
    done
  fi
  printf '%s' "${s:0:16}"
}

pick_port() {
  local p=""
  for _ in $(seq 1 40); do
    p=$(awk 'BEGIN{srand();print int(rand()*20000)+20000}')
    if ! (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q ":${p}\b"; then
      echo "$p"; return 0
    fi
  done
  echo 28650
}

setup_credentials() {
  $SUDO mkdir -p "${INSTALL_DIR}"
  local old_port="" old_token="" old_allow=""
  if [ -f "${ENV_FILE}" ]; then
    old_port=$(grep -E '^MCSYNC_PORT=' "${ENV_FILE}" 2>/dev/null | cut -d= -f2 || true)
    old_token=$(grep -E '^MCSYNC_TOKEN=' "${ENV_FILE}" 2>/dev/null | cut -d= -f2 || true)
    old_allow=$(grep -E '^MCSYNC_ALLOW_IPS=' "${ENV_FILE}" 2>/dev/null | cut -d= -f2 || true)
  fi
  # 注意：本脚本使用 set -e，绝不能写 "[ 条件 ] && 赋值"。
  # 条件为假时整条语句返回 1，脚本会立刻静默退出（历史上正是这么中断的）。
  if [ -z "${PORT}" ];  then PORT="${old_port}"; fi
  if [ -z "${PORT}" ];  then PORT="$(pick_port)"; fi
  if [ -z "${TOKEN}" ]; then TOKEN="${old_token}"; fi
  if [ -n "${TOKEN}" ] && [ ${#TOKEN} -lt 16 ]; then TOKEN=""; fi
  if [ -z "${TOKEN}" ]; then TOKEN="$(gen_token)"; fi
  if [ -z "${ALLOW_IPS}" ]; then ALLOW_IPS="${old_allow}"; fi

  $SUDO sh -c "umask 077; cat > '${ENV_FILE}' <<EOF
MCSYNC_DATA=${DATA_DIR}
MCSYNC_PORT=${PORT}
MCSYNC_HOST=0.0.0.0
MCSYNC_TOKEN=${TOKEN}
MCSYNC_ALLOW_IPS=${ALLOW_IPS}
MCSYNC_AUTH=1
MCSYNC_VERIFY_BLOB=${VERIFY_BLOB}
MCSYNC_KEEP_VERSIONS=${KEEP_VERSIONS}
MCSYNC_MAX_UPLOAD_MB=16
TZ=Asia/Shanghai
PYTHONUNBUFFERED=1
EOF"
  $SUDO chmod 600 "${ENV_FILE}"
}

# ---- 仅查询/维护类动作
if [ "$ACTION" = "show-token" ] || [ "$ACTION" = "rotate-token" ] || [ "$ACTION" = "revoke-clients" ]; then
  [ -f "${ENV_FILE}" ] || die "未找到 ${ENV_FILE}，请先完成部署"
  case "$ACTION" in
    show-token)
      TOKEN=$(grep -E '^MCSYNC_TOKEN=' "${ENV_FILE}" | cut -d= -f2 || true)
      PORT=$(grep -E '^MCSYNC_PORT=' "${ENV_FILE}" | cut -d= -f2 || true)
      printf '\n  服务器端口 : %s\n' "$PORT"
      printf '  配对口令   : %s%s%s\n\n' "$C_GRN" "$TOKEN" "$C_RESET"
      exit 0
      ;;
    rotate-token)
      NEW=$(gen_token)
      $SUDO sed -i "s|^MCSYNC_TOKEN=.*|MCSYNC_TOKEN=${NEW}|" "${ENV_FILE}"
      $SUDO rm -f "${DATA_DIR}/clients.json" 2>/dev/null
      printf '\n  新口令: %s%s%s\n' "$C_GRN" "$NEW" "$C_RESET"
      printf '  已清除全部已配对客户端，需重新配对\n'
      printf '  重启服务: systemctl restart %s\n\n' "$SERVICE_NAME"
      exit 0
      ;;
    revoke-clients)
      $SUDO rm -f "${DATA_DIR}/clients.json" 2>/dev/null
      ok "已撤销全部客户端，客户端需重新配对"
      exit 0
      ;;
  esac
fi

setup_credentials

if [ "$MODE" = "docker" ]; then deploy_docker; else deploy_native; fi

# ============================================================================
#  防火墙与自检
# ============================================================================
step "端口与连通性检查"
sleep 2
if curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  ok "服务健康检查通过"
else
  warn "本地健康检查未通过，查看日志：journalctl -u ${SERVICE_NAME} -f （docker: docker compose logs -f）"
fi

if have_cmd ufw && $SUDO ufw status 2>/dev/null | grep -qi "active"; then
  $SUDO ufw allow "${PORT}/tcp" comment "MCSync" >/dev/null 2>&1 && ok "ufw 已放行 ${PORT}/tcp" || warn "请手动放行端口 ${PORT}"
fi
if have_cmd firewall-cmd && $SUDO firewall-cmd --state >/dev/null 2>&1; then
  $SUDO firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null 2>&1
  $SUDO firewall-cmd --reload >/dev/null 2>&1 && ok "firewalld 已放行 ${PORT}/tcp" || warn "请手动放行端口 ${PORT}"
fi

step "部署完成"

# ---------------------------------------------------------------- 端口实测
# 打印之前先确认服务真的起来了、且端口确实是我们要的那个。
REAL_PORT="$(probe_port)"
if [ -n "${REAL_PORT}" ] && [ "${REAL_PORT}" != "${PORT}" ]; then
  warn "服务实际监听 ${REAL_PORT}，与配置的 ${PORT} 不一致，已按实际端口显示"
  PORT="${REAL_PORT}"
elif [ -z "${REAL_PORT}" ]; then
  warn "未在 ${PORT} 探测到服务，请检查服务状态："
  printf '     systemctl status %s\n     journalctl -u %s -n 50\n' "${SERVICE_NAME}" "${SERVICE_NAME}"
fi

# ---------------------------------------------------------------- 地址检测
# 客户端在另一台机器上，必须填公网 IP；局域网 IP 只在同内网时有效。
# 优先级：手动指定 > 云厂商元数据 > 公网回显服务 > 出网接口 IP

LIP="$(local_ip)"
PIP="$(detect_public_ip)"

printf '%s  管理页面   :%s http://localhost:%s\n' "$C_BOLD" "$C_RESET" "$PORT"
if [ -n "$PIP" ]; then
  printf '%s  %s公网地址   :%s http://%s:%s   %s← 客户端填这个%s\n' \
    "$C_BOLD" "$C_GRN" "$C_RESET" "$PIP" "$PORT" "$C_DIM" "$C_RESET"
  if [ -n "$LIP" ]; then
    printf '%s  局域网地址 :%s http://%s:%s   %s(仅限同一内网/同一路由器下)%s\n' \
      "$C_BOLD" "$C_RESET" "$LIP" "$PORT" "$C_DIM" "$C_RESET"
  fi
  printf '%s  数据目录   :%s %s\n' "$C_BOLD" "$C_RESET" "$DATA_DIR"
  printf '\n%s  ★ 客户端「服务器地址」填： %shttp://%s:%s%s\n' "$C_BOLD" "$C_GRN" "$PIP" "$PORT" "$C_RESET"
  printf '%s    云服务器请在安全组放行 %s/tcp；长期用建议 Nginx 反代 + HTTPS%s\n' "$C_DIM" "$PORT" "$C_RESET"
else
  if [ -n "$LIP" ]; then
    printf '%s  内网地址   :%s http://%s:%s\n' "$C_BOLD" "$C_RESET" "$LIP" "$PORT"
  fi
  printf '%s  数据目录   :%s %s\n' "$C_BOLD" "$C_RESET" "$DATA_DIR"
  printf '\n%s  %s[!] 未能自动获取公网 IP%s\n' "$C_BOLD" "$C_YEL" "$C_RESET"
  printf '%s    客户端与服务器不在同一内网时，请填服务器的公网 IP：%s\n' "$C_DIM" "$C_RESET"
  printf '%s    在服务器上执行 curl ifconfig.me 查看，或到云控制台查看「公网 IP」%s\n' "$C_DIM" "$C_RESET"
  printf '%s    也可用 --public-ip 重新打印：sudo bash %s --status%s\n' "$C_DIM" "${SERVER_DIR}/deploy.sh" "$C_RESET"
fi
# ---- 配对口令：部署完直接展示，不用再去翻文件
printf '\n'
printf '%s%s════════════════════════════════════════════════════════%s\n' "$C_BOLD" "$C_GRN" "$C_RESET"
printf '%s  客户端配对口令（16 位）%s\n' "$C_BOLD" "$C_RESET"
printf '%s%s════════════════════════════════════════════════════════%s\n' "$C_BOLD" "$C_GRN" "$C_RESET"
printf '\n'
printf '        %s%s%s\n' "$C_GRN" "$TOKEN" "$C_RESET"
printf '\n'
printf '  %s这串是"管理员密钥"，只用来创建第一个管理员账号。%s\n' "$C_YEL" "$C_RESET"
printf '\n'
printf '  第一步：浏览器打开管理页，用这串密钥创建管理员账号\n'
printf '          http://%s:%s\n' "${PIP:-$LIP}" "$PORT"
printf '  第二步：在管理页「账号管理」里给其他使用者创建账号\n'
printf '  第三步：Windows 客户端「设置」里填 账号 + 口令 连接（不再用这串密钥）\n'
printf '\n'
printf '  %s请立刻保存。%s遗忘可执行：sudo bash %s --show-token\n' "$C_YEL" "$C_RESET" "${SERVER_DIR}/deploy.sh"
printf '%s════════════════════════════════════════════════════════%s\n' "$C_DIM" "$C_RESET"

printf '\n%s常用命令：%s\n' "$C_DIM" "$C_RESET"
if [ "$MODE" = "docker" ]; then
  printf '  日志: cd %s && docker compose logs -f\n  重启: docker compose restart\n' "$INSTALL_DIR"
else
  printf '  日志: journalctl -u %s -f\n  重启: systemctl restart %s\n' "$SERVICE_NAME" "$SERVICE_NAME"
fi
printf '  卸载: sudo bash %s --uninstall\n' "${SERVER_DIR}/deploy.sh"
  printf '  查口令: sudo bash %s --show-token\n' "${SERVER_DIR}/deploy.sh"
  printf '  换口令: sudo bash %s --rotate-token   (会撤销全部客户端)\n' "${SERVER_DIR}/deploy.sh"
  printf '  撤销客户端: sudo bash %s --revoke-clients\n\n' "${SERVER_DIR}/deploy.sh"
