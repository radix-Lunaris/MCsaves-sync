# -*- coding: utf-8 -*-
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

"""
MCSync 公共协议层
服务端与客户端共用，两端各持一份副本（避免部署/打包时的路径依赖）。
"""
from __future__ import annotations

import os
import re
import posixpath

# ---------------------------------------------------------------- 基础常量

APP_NAME = "MCSync"
API_VERSION = 1

CHUNK_SIZE = 4 * 1024 * 1024          # 4 MiB 分块
HASH_DIGEST_SIZE = 16                  # xxh128 -> 32 位 16 进制字符串
STATE_DIR = ".mcsync"                  # 存档本地状态目录
STATE_FILE = "state.json"
MANIFEST_VERSION = 2

# 哈希算法标识：xxh128（首选）/ blake2b（无 xxhash 时的降级）
HASH_ALGO_XXH = "xxh128"
HASH_ALGO_B2 = "blake2b"

# ---------------------------------------------------------------- 忽略规则

# 默认忽略：这些文件对存档同步毫无意义，且会频繁变化 / 体积巨大 / 含锁。
DEFAULT_IGNORE_RULES: list[str] = [
    # 锁与临时
    "session.lock",
    "*.lock",
    "*.tmp",
    "*.temp",
    "*.part",
    "*.bak",
    # 日志与崩溃报告
    "logs/",
    "crash-reports/",
    "*.log",
    "*.log.gz",
    "*.txt.gz",
    # 启动器/同步工具自身
    ".mcsync/",
    ".ds_store",
    "thumbs.db",
    "desktop.ini",
    # 常见的纯缓存
    "advancements_cache/",
    "download_cache/",
    "screenshots_cache/",
    "**/cached_assets/",
]

# 只同步这些目录/文件时开启「核心模式」
CORE_ONLY_INCLUDE: list[str] = [
    "level.dat",
    "level.dat_old",
    "session.lock",          # 会在忽略中剔除，这里仅为语义完整
    "region/",
    "poi/",
    "entities/",
    "DIM*/",
    "dimensions/",
    "data/",
    "datapacks/",
    "playerdata/",
    "stats/",
    "advancements/",
    "icon.png",
    "resources.zip",
    "generated/",
    "maps/",
]


def _rule_to_regex(rule: str) -> re.Pattern:
    """把 glob 风格的忽略规则编译成匹配相对路径的正则。"""
    rule = rule.strip().replace("\\", "/")
    if not rule:
        return re.compile(r"(?!x)x")  # 永不匹配

    anchored = rule.startswith("/")

    # 目录规则：以 / 结尾 -> 该目录及其内部所有内容
    if rule.endswith("/"):
        body = rule.rstrip("/").lstrip("/")
        pat = _glob_body(body)
        prefix = "" if anchored else "(?:.*/)?"
        return re.compile(r"^%s%s(?:/.*)?$" % (prefix, pat))

    body = rule.lstrip("/")
    pat = _glob_body(body)
    prefix = "" if anchored else "(?:.*/)?"
    return re.compile(r"^%s%s$" % (prefix, pat))


def _glob_body(body: str) -> str:
    out = []
    for ch in body:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


class IgnoreMatcher:
    """相对路径 -> 是否忽略。支持 glob、目录规则、以及取反规则（以 ! 开头）。"""

    def __init__(self, rules: list[str] | None = None):
        self.rules: list[str] = []
        self._pos: list[re.Pattern] = []
        self._neg: list[re.Pattern] = []
        for r in (rules if rules is not None else DEFAULT_IGNORE_RULES):
            self.add(r)

    def add(self, rule: str) -> None:
        rule = rule.strip()
        if not rule or rule.startswith("#"):
            return
        negate = rule.startswith("!")
        if negate:
            rule = rule[1:]
        self.rules.append(("!" if negate else "") + rule)
        (self._neg if negate else self._pos).append(_rule_to_regex(rule))

    def match(self, rel_path: str) -> bool:
        rel = norm_rel(rel_path)
        for p in self._neg:
            if p.match(rel):
                return False
        for p in self._pos:
            if p.match(rel):
                return True
        return False


def make_ignore(extra_rules: list[str] | None = None, core_only: bool = False) -> IgnoreMatcher:
    rules = list(DEFAULT_IGNORE_RULES)
    if extra_rules:
        rules.extend(extra_rules)
    m = IgnoreMatcher(rules)
    m._core_only = core_only  # type: ignore[attr-defined]
    return m


def is_core_path(rel_path: str) -> bool:
    rel = norm_rel(rel_path)
    for inc in CORE_ONLY_INCLUDE:
        if _rule_to_regex(inc).match(rel):
            return True
    return False


# ---------------------------------------------------------------- 路径工具

def norm_rel(rel_path: str) -> str:
    """统一为 posix 风格的相对路径，作为服务端与客户端的共同主键。"""
    p = rel_path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return posixpath.normpath(p.lstrip("/"))


def safe_join(root: str, rel_path: str) -> str:
    """防止 ../ 穿越，返回本地实际路径。"""
    rel = norm_rel(rel_path)
    if rel in (".", "", "/"):
        return root
    parts = [p for p in rel.split("/") if p not in ("..", "")]
    return os.path.join(root, *parts)


def pack_piece(data: bytes) -> bytes:
    import zlib
    if len(data) >= 512:
        try:
            c = zlib.compress(data, 6)
            if len(c) < len(data) * 0.95:
                return b"Z" + c
        except Exception:
            pass
    return b"R" + data


def unpack_piece(blob: bytes) -> bytes:
    import zlib
    if not blob:
        return b""
    if blob[:1] == b"Z":
        try:
            return zlib.decompress(blob[1:])
        except Exception:
            return blob[1:]
    return blob[1:]


def normalize_save_name(name: str) -> str:
    """云端存档键名：去掉 Windows 非法字符与路径分隔符。"""
    name = name.strip().replace("\\", "_").replace("/", "_")
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    name = name.strip(" .")
    return name or "world"


# ---------------------------------------------------------------- 数据结构

def file_entry(path: str, size: int, mtime: float, fhash: str, chunks: list[str] | None = None) -> dict:
    return {
        "path": norm_rel(path),
        "size": int(size),
        "mtime": float(mtime),
        "hash": fhash,
        "chunks": chunks or [],
    }


def human_size(num: float) -> str:
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} PB"


def human_speed(bps: float) -> str:
    return human_size(bps) + "/s"


def human_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds != seconds:  # NaN
        return "--"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds // 60 % 60:02d}m"


# ---------------------------------------------------------------- 版本兼容

def chunks_of(size: int, chunk_size: int = CHUNK_SIZE) -> int:
    if size <= 0:
        return 0
    return (size + chunk_size - 1) // chunk_size
