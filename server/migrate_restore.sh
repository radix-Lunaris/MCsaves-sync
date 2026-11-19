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
# 新服务器「一键导入」：解开 migrate_backup.sh 产出的迁移包。
# 用法： sudo bash migrate_restore.sh --archive mcsync-migrate-XXX.tar.gz [--force]
# 它只负责把数据放到位；装依赖 / 建服务由随后执行的 deploy.sh 完成。
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/mcsync}"
DATA_DIR="${DATA_DIR:-${INSTALL_DIR}/data}"
ENV_FILE="${INSTALL_DIR}/mcsync.env"
ARCHIVE=""
FORCE=0
SERVICE="mcsync"

while [ $# -gt 0 ]; do
  case "$1" in
    --archive|-a) ARCHIVE="${2:-}"; shift 2;;
    --force|-f)   FORCE=1; shift;;
    -h|--help)    sed -n '15,18p' "$0"; exit 0;;
    *)            echo "[X] 未知参数: $1" >&2; exit 1;;
  esac
done

echo "==> MCSync 服务端数据导入"
echo "    安装目录: ${INSTALL_DIR}"
echo "    数据目录: ${DATA_DIR}"

# --force 会清空 DATA_DIR，绝不能让自定义路径指向根目录或安装目录本身
case "${DATA_DIR}" in
  /|/opt|/opt/mcsync|/home|/root|/usr|/var|"${INSTALL_DIR}")
    echo "[X] 拒绝在危险路径上操作：${DATA_DIR}" >&2
    exit 1;;
esac

if [ -z "${ARCHIVE}" ] || [ ! -f "${ARCHIVE}" ]; then
  echo "[X] 请指定迁移包：--archive /path/to/mcsync-migrate-XXX.tar.gz" >&2
  exit 1
fi

# ---------------------------------------------------------------- 校验
SHA_FILE="${ARCHIVE}.sha256"
if [ -f "${SHA_FILE}" ] && command -v sha256sum >/dev/null 2>&1; then
  echo "[*] 校验文件完整性 ..."
  EXPECT="$(awk '{print $1}' "${SHA_FILE}" 2>/dev/null || true)"
  ACTUAL="$(sha256sum "${ARCHIVE}" 2>/dev/null | awk '{print $1}' || true)"
  if [ -n "${EXPECT}" ] && [ -n "${ACTUAL}" ] && [ "${EXPECT}" != "${ACTUAL}" ]; then
    echo "[X] 校验和不匹配，文件传输可能损坏，已中止" >&2
    echo "    期望 ${EXPECT}" >&2
    echo "    实际 ${ACTUAL}" >&2
    exit 1
  fi
  echo "[OK] 校验通过"
fi

STAGE="$(mktemp -d)"
cleanup() { rm -rf "${STAGE}"; }
trap cleanup EXIT

echo "[*] 解包 ..."
if ! tar -xzf "${ARCHIVE}" -C "${STAGE}"; then
  echo "[X] 解包失败，文件可能损坏或不是 MCSync 迁移包" >&2
  exit 1
fi
for need in MIGRATE_INFO.txt data/mcsync.db; do
  if [ ! -e "${STAGE}/${need}" ]; then
    echo "[X] 迁移包缺少 ${need}，不是有效的 MCSync 导出" >&2
    exit 1
  fi
done
cat "${STAGE}/MIGRATE_INFO.txt"
echo ""

# ---------------------------------------------------------------- 保护已有数据
if [ -f "${DATA_DIR}/mcsync.db" ] && [ "${FORCE}" -eq 0 ]; then
  echo "[X] ${DATA_DIR} 里已有数据库，为避免覆盖，已中止" >&2
  echo "    · 若这是刚部署完的空环境，通常直接覆盖是安全的：" >&2
  echo "      sudo bash $0 --archive ${ARCHIVE} --force" >&2
  echo "    · 若里面已有真实存档，请先备份再决定是否覆盖" >&2
  exit 1
fi

# 若目标服务正在跑，先停掉，避免它一边写一边被覆盖
if command -v systemctl >/dev/null 2>&1 \
   && systemctl is-active "${SERVICE}" >/dev/null 2>&1; then
  echo "[*] 停止现有服务 ..."
  systemctl stop "${SERVICE}"
fi
if [ -f "${INSTALL_DIR}/.mode" ] \
   && [ "$(cat "${INSTALL_DIR}/.mode" 2>/dev/null || echo '')" = "docker" ] \
   && command -v docker >/dev/null 2>&1; then
  ( cd "${INSTALL_DIR}" && docker compose down --remove-orphans ) >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------- 放置
mkdir -p "${INSTALL_DIR}" "${DATA_DIR}"
if [ "${FORCE}" -eq 1 ]; then
  # 只清数据内容，不动 venv / 源码 / static
  rm -rf "${DATA_DIR:?}"/*
else
  mkdir -p "${DATA_DIR}"
fi
cp -a "${STAGE}/data/." "${DATA_DIR}/"

HAS_ENV=0
if [ -f "${STAGE}/mcsync.env" ]; then
  # deploy.sh 会读旧 env 里的 MCSYNC_PORT / MCSYNC_TOKEN / ALLOW_IPS，
  # 只要这个文件在位，重新部署后端口与管理员密钥保持不变。
  cp -a "${STAGE}/mcsync.env" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  HAS_ENV=1
  echo "[OK] 已恢复 mcsync.env（端口与管理员密钥保持不变）"
else
  echo "[!] 迁移包不含 mcsync.env，部署时会生成新端口与新管理员密钥"
  echo "    所有客户端需要重新填写地址并重新登录"
fi

# 属主：服务端以独立低权限用户运行，写不进 root 的文件会直接 500
if id -u "${SERVICE}" >/dev/null 2>&1; then
  chown -R "${SERVICE}:${SERVICE}" "${INSTALL_DIR}" 2>/dev/null \
    || chown -R "${SERVICE}" "${INSTALL_DIR}" 2>/dev/null || true
  echo "[OK] 属主已设为 ${SERVICE}"
else
  echo "[!] 尚无 ${SERVICE} 用户，deploy.sh 创建后会自动 chown"
fi

PORT="$(grep -E '^MCSYNC_PORT=' "${ENV_FILE}" 2>/dev/null | cut -d= -f2 || true)"
BLOBS="$(find "${DATA_DIR}/blobs" -type f 2>/dev/null | wc -l | tr -d ' ' || echo 0)"
echo ""
echo "[OK] 数据已就位"
echo "    数据块 : ${BLOBS} 个"
echo "    端口   : ${PORT:-由 deploy.sh 分配}"
echo ""
echo "  下一步（务必执行，用来装依赖 / 建 systemd 服务 / 放行端口）："
echo ""
echo "    sudo bash deploy.sh"
echo ""
echo "  部署完成后："
echo "    · sudo bash deploy.sh --status     查看真实公网地址与端口"
echo "    · 云服务器安全组放行该端口"
echo "    · 客户端「设置 → 服务器地址」改成新服务器地址"
if [ "${HAS_ENV}" -eq 1 ]; then
  echo "    · accounts.json / clients.json 已保留，客户端无需重新登录"
else
  echo "    · 因 env 未随迁，端口与管理员密钥已变，客户端需重新登录"
fi
