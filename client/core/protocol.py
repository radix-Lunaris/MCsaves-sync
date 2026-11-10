# -*- coding: utf-8 -*-
"""
MCSync 公共协议层
服务端与客户端共用，两端各持一份副本（避免部署/打包时的路径依赖）。
"""
from __future__ import annotations

import os
import re
import posixpath
import zlib

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
    "*.spd_backup",          # 共享玩家数据修正时空壳文件的备份，不该反复同步
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
    "players/",               # 26.1+ 玩家数据（data / advancements / stats 都在这里）
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

# ---------------------------------------------------------------- 分片
# 旧版固定 4MiB 一刀切，MC 存档里既有大量几 KB 的小文件（playerdata/ stats/），
# 也有几十 MB 的 region；统一切 4MiB 会让小文件也占一个 blob、大文件改一点就要
# 重传整块。这里按文件大小自适应，并对 .mca 做「区域内部」差分。

PIECE_TINY = 256 * 1024        # <=256KB 的文件：整文件一块
PIECE_SMALL = 1 * 1024 * 1024  # <=8MB
PIECE_MID = 4 * 1024 * 1024    # <=64MB
PIECE_BIG = 8 * 1024 * 1024    # 更大

REGION_HEADER = 8192           # .mca 前 8KB 是 location + timestamp 表
REGION_SECTOR = 4096
REGION_GROUP = 8               # 保留仅为兼容旧常量引用，新算法不再按索引分组
REGION_MIN_PIECES = 32         # 每个 region 至少切成这么多片，保证差分粒度
REGION_PIECE_FLOOR = 64 * 1024  # 单片下限：再小就只剩元数据开销了


def piece_size_for(size: int) -> int:
    """按文件大小自适应分片大小。"""
    if size <= PIECE_TINY:
        return PIECE_TINY
    if size <= 8 * 1024 * 1024:
        return PIECE_SMALL
    if size <= 64 * 1024 * 1024:
        return PIECE_MID
    return PIECE_BIG


def _region_cover_ranges(path: str, size: int):
    """解析 .mca，返回「覆盖式」分片 [(offset, length)]；无法解析返回 None。

    ── 这里踩过一个严重的数据量事故，务必理解为何要这样写 ──

    旧实现按「区块索引」分组（0-7、8-15 …），再取组内 min(起始)/max(结束)。
    但 MC 是按**生成顺序**把区块写进 region 的，空间上完全散乱 ——
    索引 0-7 这 8 个区块往往散布在整个文件里，min/max 一跨就是几十 MB。
    128 个分组彼此严重重叠，同一段字节被重复计入几十次：
    实测 64MB 的 .mca 被切成总计 6195MB 的分片（96.8 倍膨胀），
    400MB 的存档因此上传了 16GB。

    正确做法必须满足三条硬约束：
      1. 有序、不重叠      —— 否则总量会远超文件大小（就是上面这个事故）
      2. 恰好覆盖 [0,size) —— 否则下载回来无法按字节还原
      3. 边界尽量稳定      —— 改一个区块只让少量分片失效

    本实现：解析出所有区块区间 → 按起始排序 → 合并重叠/紧邻的 → 补齐空隙
    → 超长片再按上限切开。空隙多为全 0，内容相同会因哈希去重而几乎不占带宽。
    """
    if size <= REGION_HEADER:
        return None
    try:
        with open(path, "rb") as f:
            header = f.read(REGION_HEADER)
    except Exception:
        return None
    if len(header) < REGION_HEADER:
        return None

    chunks: list = []
    for i in range(1024):
        b = header[i * 4:(i * 4) + 4]
        off = (b[0] << 16) | (b[1] << 8) | b[2]
        cnt = b[3]
        if off <= 0 or cnt <= 0:
            continue
        start = off * REGION_SECTOR
        if start < REGION_HEADER or start >= size:
            continue
        end = min(start + cnt * REGION_SECTOR, size)
        if end <= start:
            continue
        chunks.append((start, end))
    if not chunks:
        return None

    chunks.sort()
    # 合并重叠 / 紧邻（空隙不超过一个扇区就并进去，减少分片数量）
    merged: list = []
    for s, e in chunks:
        if merged and s <= merged[-1][1] + REGION_SECTOR:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])

    out: list = [(0, REGION_HEADER)]
    pos = REGION_HEADER
    for s, e in merged:
        s = max(s, pos)              # 防止与已输出区间重叠
        if s > pos:
            out.append((pos, s - pos))   # 空隙：多为 0 字节，压缩后几乎免费
            pos = s
        cap = _region_piece_cap(size)
        while e - s > cap:               # 超长片切开，避免单片过大
            out.append((s, cap))
            s += cap
        if e > s:
            out.append((s, e - s))
            pos = max(pos, e)
    if pos < size:
        out.append((pos, size - pos))    # 尾部空隙
    return out


def _region_piece_cap(size: int) -> int:
    """region 分片上限：在「差分粒度」与「分片数量」之间取平衡。

    固定 1MB 会让小 region（几百 KB）退化成单片 —— 改一个区块也要整文件重传，
    差分等于失效。这里保证每个文件至少切成约 REGION_MIN_PIECES 片：
      264KB  -> 上限  64KB，约  4 片
      8MB    -> 上限 256KB，约 32 片
      64MB   -> 上限   1MB，约 64 片
      400MB  -> 上限   1MB，约 400 片
    """
    cap = max(1, size // REGION_MIN_PIECES)
    cap = min(cap, PIECE_SMALL)
    return max(cap, REGION_PIECE_FLOOR) if size > REGION_PIECE_FLOOR else max(1, cap)


def _ranges_sane(ranges, size: int) -> bool:
    """分片自检：有序、不重叠、恰好覆盖全文。

    这是防复发的最后一道闸。任何分片算法（尤其是新的差分优化）都必须过这一关，
    否则宁可退回最朴素的定长切分 —— 宁可多传一点，也不能再出现几十倍膨胀。
    """
    if not ranges or size <= 0:
        return False
    pos = 0
    for off, n in ranges:
        if n <= 0 or off != pos or off + n > size:
            return False
        pos += n
    return pos == size


def piece_ranges(path: str, size: int, chunk_size: int = 0):
    """返回该文件的 [(offset, length)] 列表。空文件返回 []。

    铁律：返回值必须有序、不重叠、总长恰好等于 size。
    """
    if size <= 0:
        return []
    if path.lower().endswith(".mca"):
        r = _region_cover_ranges(path, size)
        if r and _ranges_sane(r, size):
            return r
        # 不合规就退回定长切分，绝不带着错误分片继续
    cs = chunk_size or piece_size_for(size)
    out = []
    off = 0
    while off < size:
        n = min(cs, size - off)
        out.append((off, n))
        off += n
    return out


# ---------------------------------------------------------------- 压缩
# 块级压缩：level.dat、playerdata、日志等文本/JSON 可压 50%+；
# region 本身已压缩，压了没收益反而白耗 CPU，所以「先试压，压不动就存原始」。
# 存盘时带 1 字节算法前缀，读取时据此还原。
COMPRESS_MIN = 512          # 小于这个体积不值得压
COMPRESS_LEVEL = 6


def pack_piece(data: bytes) -> bytes:
    if len(data) >= COMPRESS_MIN:
        try:
            c = zlib.compress(data, COMPRESS_LEVEL)
            if len(c) < len(data) * 0.95:
                return b"Z" + c
        except Exception:
            pass
    return b"R" + data


def unpack_piece(blob: bytes) -> bytes:
    if not blob:
        return b""
    if blob[:1] == b"Z":
        try:
            return zlib.decompress(blob[1:])
        except Exception:
            return blob[1:]
    return blob[1:]


def chunks_of(size: int, chunk_size: int = CHUNK_SIZE) -> int:
    if size <= 0:
        return 0
    return (size + chunk_size - 1) // chunk_size
