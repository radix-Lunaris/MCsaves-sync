#!/usr/bin/env bash
# This file is part of MCSync.
# Copyright (C) 2025 radix-Lunaris
#
# MCSync is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# MCSync is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along
# with MCSync. If not, see <https://www.gnu.org/licenses/>.
#
# 旧服务器「一键导出」：把云端全部数据 + 配置打成一个 tar.gz。
# 用法： sudo bash migrate_backup.sh [--output DIR] [--no-stop]
# 环境变量 INSTALL_DIR / DATA_DIR 可覆盖默认路径。
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/mcsync}"
DATA_DIR="${DATA_DIR:-${INSTALL_DIR}/data}"
ENV_FILE="${INSTALL_DIR}/mcsync.env"
OUT_DIR="${OUT:-$PWD}"
NO_STOP=0
SERVICE="mcsync"

while [ $# -gt 0 ]; do
  case "$1" in
    --output|-o) OUT_DIR="${2:-$OUT_DIR}"; shift 2;;
    --no-stop)   NO_STOP=1; shift;;
    -h|--help)   sed -n '15,17p' "$0"; exit 0;;
    *)           echo "[X] 未知参数: $1" >&2; exit 1;;
  esac
done

echo "==> MCSync 服务端数据导出"
echo "    安装目录: ${INSTALL_DIR}"
echo "    数据目录: ${DATA_DIR}"

if [ ! -d "${DATA_DIR}" ]; then
  echo "[X] 数据目录不存在：${DATA_DIR}" >&2
  echo "    如自定义过路径，请用 DATA_DIR=/your/path sudo -E bash $0" >&2
  exit 1
fi
if [ ! -f "${DATA_DIR}/mcsync.db" ]; then
  echo "[X] ${DATA_DIR}/mcsync.db 不存在，目录不像 MCSync 数据目录" >&2
  exit 1
fi

# ---------------------------------------------------------------- 停服务
# SQLite 开了 WAL，不停机直接拷会得到不一致快照（db-wal 里还有未合并的事务）。
# 停机后 SQLite 会自动 checkpoint，此时整个目录就是一致的。
MODE="native"
if [ -f "${INSTALL_DIR}/.mode" ]; then
  MODE="$(cat "${INSTALL_DIR}/.mode" 2>/dev/null || echo native)"
fi
STOPPED=0
if [ "${NO_STOP}" -eq 0 ]; then
  if [ "${MODE}" = "docker" ] && command -v docker >/dev/null 2>&1; then
    echo "[*] 停止 Docker 容器 ..."
    if ( cd "${INSTALL_DIR}" && docker compose down --remove-orphans ) >/dev/null 2>&1 \
       || docker stop "${SERVICE}" >/dev/null 2>&1; then
      STOPPED=1; echo "[OK] 容器已停止"
    else
      echo "[!] 未能自动停止容器，请手动执行后加 --no-stop 重试" >&2
      exit 1
    fi
  elif command -v systemctl >/dev/null 2>&1 \
       && systemctl is-active "${SERVICE}" >/dev/null 2>&1; then
    echo "[*] 停止 systemd 服务 ..."
    systemctl stop "${SERVICE}"
    STOPPED=1; echo "[OK] 服务已停止"
  else
    echo "[!] 未检测到运行中的服务，按「已停机」处理"
  fi
else
  echo "[!] --no-stop：假定服务已手动停止，直接打包"
fi

# ---------------------------------------------------------------- 组装
STAGE="$(mktemp -d)"
cleanup() { rm -rf "${STAGE}"; }
trap cleanup EXIT

mkdir -p "${STAGE}/data"
# 硬链接优先：几十 GB 的 blobs 不需要复制一份，也不占额外磁盘。
# 跨文件系统时 cp -al 会失败，回退到真实复制。
if ! cp -al "${DATA_DIR}/." "${STAGE}/data/" 2>/dev/null; then
  rm -rf "${STAGE}/data"; mkdir -p "${STAGE}/data"
  echo "[*] 跨文件系统，改用复制（需要约等于数据大小的临时空间）"
  cp -a "${DATA_DIR}/." "${STAGE}/data/"
fi

# tmp/ 是下载 zip 等临时产物，不带走；目录本身保留，服务端启动会自建。
if [ -d "${STAGE}/data/tmp" ]; then
  rm -rf "${STAGE}/data/tmp"/* 2>/dev/null || true
fi

HAS_ENV=0
if [ -f "${ENV_FILE}" ]; then
  cp -a "${ENV_FILE}" "${STAGE}/mcsync.env"
  HAS_ENV=1
else
  echo "[!] 未找到 ${ENV_FILE}（Docker 模式或自定义部署常见）"
  echo "    新服务器会生成新端口/新管理员密钥，客户端需要重新配置"
fi

PORT="$(grep -E '^MCSYNC_PORT=' "${ENV_FILE}" 2>/dev/null | cut -d= -f2 || true)"
BLOBS="$(find "${DATA_DIR}/blobs" -type f 2>/dev/null | wc -l | tr -d ' ' || echo 0)"
SIZE="$(du -sh "${DATA_DIR}" 2>/dev/null | cut -f1 || echo '?')"

cat > "${STAGE}/MIGRATE_INFO.txt" <<EOF
MCSync 数据迁移包
导出时间 : $(date '+%Y-%m-%d %H:%M:%S %Z')
主机名   : $(hostname)
模式     : ${MODE}
安装目录 : ${INSTALL_DIR}
数据目录 : ${DATA_DIR}
端口     : ${PORT:-未记录}
数据大小 : ${SIZE}
数据块数 : ${BLOBS}
含 env   : ${HAS_ENV}

内容：
  data/mcsync.db          存档/版本/审计/回收站的 SQLite 库
  data/mcsync.db-wal|-shm WAL 日志（停机后通常已合并，存在则一并带上）
  data/blobs/             内容寻址数据块，全部存档的真实内容
  data/manifests/         每个版本的清单 JSON
  data/trash/             软删除的回收站
  data/accounts.json      账号与口令哈希
  data/clients.json       已配对客户端记录（保留它，客户端才不用重新登录）
  data/token.txt          管理员密钥
  mcsync.env              端口 / 管理员密钥 / IP 白名单 / 鉴权开关

恢复： sudo bash migrate_restore.sh --archive <本文件>
EOF

TS="$(date '+%Y%m%d-%H%M%S')"
mkdir -p "${OUT_DIR}"
ARCHIVE="${OUT_DIR}/mcsync-migrate-${TS}.tar.gz"
tar -czf "${ARCHIVE}" -C "${STAGE}" data mcsync.env MIGRATE_INFO.txt 2>/dev/null \
  || tar -czf "${ARCHIVE}" -C "${STAGE}" data MIGRATE_INFO.txt
chmod 600 "${ARCHIVE}"

# ---------------------------------------------------------------- 恢复服务
if [ "${STOPPED}" -eq 1 ]; then
  if [ "${MODE}" = "docker" ] && command -v docker >/dev/null 2>&1; then
    ( cd "${INSTALL_DIR}" && docker compose up -d ) >/dev/null 2>&1 || true
  else
    systemctl start "${SERVICE}" 2>/dev/null || true
  fi
  echo "[OK] 旧服务已恢复运行"
fi

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "${ARCHIVE}" > "${ARCHIVE}.sha256"
  CHECK_LINE="    校验   : ${ARCHIVE}.sha256"
else
  CHECK_LINE="    校验   : (系统无 sha256sum，请自行核对文件完整性)"
fi
echo ""
echo "[OK] 导出完成"
echo "    文件   : ${ARCHIVE}"
echo "    大小   : $(du -sh "${ARCHIVE}" 2>/dev/null | cut -f1 || echo '?')"
echo "${CHECK_LINE}"
echo ""
echo "  下一步：把上面两个文件传到新服务器，然后执行"
echo "    sudo bash migrate_restore.sh --archive $(basename "${ARCHIVE}")"
echo "    sudo bash deploy.sh"
