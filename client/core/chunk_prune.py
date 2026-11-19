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
区块清理：按 InhabitedTime 筛选并删除"几乎没待过"的区块。

设计原则（这是危险操作，安全优先于功能 completeness）：

1. 默认 dry-run。scan() 一个字节都不改，只出报告；apply() 必须显式调用。
2. 缺失字段绝不当成 0。InhabitedTime 不存在的老版本存档：
   整个存档都读不到该字段时，capability() 判定为不支持，UI 直接禁用
   清理功能 —— 而不是把"读不到"当成"没人来过"给删了。
3. 无法解析的区块（LZ4 / 自定义压缩 / NBT 损坏 / 数据截断）一律保留，
   计入 skipped 并展示，绝不在存疑时删除。
4. region / entities / poi 三目录按同一全局区块坐标协同删除，
   避免留下孤立实体与 POI 数据。
5. 执行前强制备份，且只允许在游戏关闭时进行。
6. 重写 .mca 时紧凑化数据区，真正释放磁盘空间；空 region 文件直接删除。

InhabitedTime 是"玩家在该区块累计停留的 tick 数"（20 tick = 1 秒），
多人同时在场时增长更快，仅作为"是否有人用过"的参考线索，不是安全判据。
"""
from __future__ import annotations

import os
import shutil
import struct
import time
import zlib
import gzip
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from core.nbt import NBTReader
from core import nbt_scan
from core import nbt_typed

TICKS_PER_SECOND = 20
DEFAULT_MAX_SECONDS = 1          # 默认阈值：停留不足 1 秒
REGION_HEADER = 8192
REGION_SECTOR = 4096
LOCATION_TABLE = 4096
ENTRIES = 1024

# 压缩类型字节，参照 MCA Selector 的 io/mca/CompressionType：
#   NONE 0 / GZIP 1 / ZLIB 2 / UNCOMPRESSED 3 / LZ4 4
#   最高位（>=128，Java 里是负 byte）表示数据放在外部 .mcc 文件，
#   低 7 位仍是上面那几种压缩算法。
COMP_NONE = 0
COMP_GZIP = 1
COMP_ZLIB = 2
COMP_UNCOMPRESSED = 3
COMP_LZ4 = 4
COMP_CUSTOM = 127
COMP_EXTERNAL_BIT = 0x80

# NBT 根里 InhabitedTime 的位置随版本变化：
#   1.18+ 直接在根；1.17 及以前嵌在 Level 下。
#   两处都找不到 => 该存档不写这个字段（老版本），必须禁用清理。
INHABITED_PATHS = (("InhabitedTime",), ("Level", "InhabitedTime"))


@dataclass
class ChunkVerdict:
    """单个区块的判定结果。"""
    index: int
    inhabited: Optional[int] = None     # None = 字段缺失/无法判定
    reason: str = ""                    # keep / prune / skip:xxx
    data: bytes = b""                   # 原始 chunk 数据（含 4 字节长度头）
    data_len: int = 0                   # 数据长度；预览时只记长度，不拷字节
    timestamp: int = 0


@dataclass
class RegionReport:
    path: str
    dimension: str = ""
    kind: str = "region"               # region / entities / poi
    total: int = 0
    prune: int = 0
    keep: int = 0
    skip: int = 0
    missing_field: int = 0             # 明确缺 InhabitedTime 字段
    unreadable: int = 0                # 压缩/NBT 无法解析
    freed_bytes: int = 0
    will_delete_file: bool = False
    verdicts: Dict[int, ChunkVerdict] = field(default_factory=dict)


@dataclass
class PruneReport:
    """一次 dry-run 的完整报告。"""
    save_dir: str = ""
    threshold_seconds: float = DEFAULT_MAX_SECONDS
    threshold_ticks: int = DEFAULT_MAX_SECONDS * TICKS_PER_SECOND
    supported: bool = True
    unsupported_reason: str = ""
    regions: List[RegionReport] = field(default_factory=list)
    total_chunks: int = 0
    prune_chunks: int = 0
    skipped_chunks: int = 0
    affected_files: int = 0
    delete_files: int = 0
    freed_bytes: int = 0
    dimensions: Dict[str, int] = field(default_factory=dict)

    @property
    def safe_to_apply(self) -> bool:
        return self.supported and self.prune_chunks > 0


def _from_typed(root) -> Optional[int]:
    """从保类型 NBT 里取 InhabitedTime：根下，或 Level 下。"""
    def pick(d):
        node = d.get("InhabitedTime")
        if node is not None and getattr(node, "type", None) == nbt_typed.TAG_LONG:
            return int(node.value)
        return None

    d = nbt_typed.compound(root)
    v = pick(d)
    if v is not None:
        return v
    lvl = d.get("Level")
    if lvl is not None and getattr(lvl, "type", None) == nbt_typed.TAG_COMPOUND:
        return pick(nbt_typed.compound(lvl))
    return None


def _inhabited_from_nbt(nbt) -> Optional[int]:
    """从区块 NBT 里取 InhabitedTime；入参可以是原始字节或已解析的 dict。

    三级回退，任一级成功即可。字段确实缺失返回 None（调用方会标
    skip:missing_field 并保留区块），解析失败抛异常（标 skip:nbt）。
    层级设计是为了兼容真实世界里各种版本/模组写出的 NBT：
      1. 轻量扫描器 —— 只跳过 payload，不建树，最快也最抗畸形数据
      2. 保类型解析器 —— 项目自研，12 种 TAG 全部保留
      3. 通用解析器 —— 最后兜底
    """
    # 兼容旧调用：有些辅助函数手里已经是解析好的 dict
    if isinstance(nbt, dict):
        for path in INHABITED_PATHS:
            cur = nbt
            ok = True
            for key in path:
                if not isinstance(cur, dict) or key not in cur:
                    ok = False
                    break
                cur = cur[key]
            if ok and isinstance(cur, (int, float)) and not isinstance(cur, bool):
                return int(cur)
        return None

    errors: List[str] = []
    try:
        # 扫描器能完整走完整个 NBT（跳过所有 payload）就说明数据可信：
        # 返回 None 就是真的没写这个字段，不必再让后面两级重复解一遍树。
        return nbt_scan.extract_inhabited(nbt)
    except Exception as e:
        errors.append(f"scan:{e}")

    try:
        _name, root = nbt_typed.TypedReader(nbt).parse()
        v = _from_typed(root)
        if v is not None:
            return v
        return None
    except Exception as e:
        errors.append(f"typed:{e}")

    try:
        _name, root = NBTReader(nbt).parse()
        d = root or {}
        for path in INHABITED_PATHS:
            cur = d
            ok = True
            for key in path:
                if not isinstance(cur, dict) or key not in cur:
                    ok = False
                    break
                cur = cur[key]
            if ok and isinstance(cur, (int, float)) and not isinstance(cur, bool):
                return int(cur)
        if not errors:
            return None
    except Exception as e:
        errors.append(f"plain:{e}")

    if errors:
        raise ValueError("; ".join(errors))
    return None


def _lz4_block(src: bytes, max_out: int = 64 * 1024 * 1024) -> bytes:
    """纯 Python 的 LZ4 block 解压，不依赖可选包。

    MCA Selector 支持 LZ4 压缩的 chunk（1.20.5+ 服务端可选）。
    Windows 客户端不会预装 lz4 包，所以内置一份实现，避免把这种存档
    整体判成「格式不兼容」。
    """
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                if i >= n:
                    raise ValueError("LZ4 literal 长度截断")
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        if i + lit > n:
            raise ValueError("LZ4 literal 越界")
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        if i + 2 > n:
            raise ValueError("LZ4 offset 截断")
        off = src[i] | (src[i + 1] << 8)
        i += 2
        if off == 0 or off > len(out):
            raise ValueError("LZ4 offset 非法")
        mlen = token & 15
        if mlen == 15:
            while True:
                if i >= n:
                    raise ValueError("LZ4 match 长度截断")
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - off
        for k in range(mlen):
            out.append(out[start + k])
        if len(out) > max_out:
            raise ValueError("LZ4 解压结果过大")
    return bytes(out)


def _decompress_chunk(comp: int, raw: bytes):
    """返回 (是否成功, NBT 字节)。失败时绝不能猜测内容。

    comp 的最高位（Java 里是负 byte）表示数据在外部 .mcc 文件：
    低 7 位仍是真实压缩算法，但 NBT 不在这段字节里，调用方会保守
    保留整份 region。这里主要保证常规四种压缩都能读。
    """
    try:
        if comp == COMP_ZLIB:
            return True, zlib.decompress(raw)
        if comp == COMP_GZIP:
            return True, gzip.decompress(raw)
        if comp in (COMP_NONE, COMP_UNCOMPRESSED):
            return True, raw
        if comp == COMP_LZ4:
            try:
                import lz4.block  # type: ignore
                return True, lz4.block.decompress(raw, uncompressed_size=64 * 1024 * 1024)
            except Exception:
                return True, _lz4_block(raw)
        # 127 自定义压缩 / 未知算法
        return False, b""
    except Exception:
        return False, b""


def _inflate_prefix(comp: int, raw: bytes,
                    limit: int = 64 * 1024):
    """只解压前 limit 字节，返回 (成功, 数据, 是否被截断)。

    InhabitedTime 位于 chunk NBT 根的前部（1.18+）或 Level 的前部（1.17-），
    而真正的开销全在解压后面几百 KB 的 sections / palette / Heightmaps。
    轻量扫描器一找到目标字段就返回，所以绝大多数区块只需解压几 KB。
    若前缀不够（字段靠后或损坏），把 truncated 告诉调用方去全量重试。
    """
    try:
        if comp in (COMP_NONE, COMP_UNCOMPRESSED):
            return True, raw[:limit], len(raw) > limit
        if comp == COMP_ZLIB:
            d = zlib.decompressobj()
            out = d.decompress(raw, limit)
            return True, out, not d.eof
        if comp == COMP_GZIP:
            d = zlib.decompressobj(31)
            out = d.decompress(raw, limit)
            return True, out, not d.eof
    except Exception:
        return False, b"", False
    # LZ4 / 未知算法：走完整解压
    ok, data = _decompress_chunk(comp, raw)
    return ok, data, False


def _read_inhabited(comp: int, payload: bytes):
    """返回 (值, 失败原因)。值为 None 且原因为空 = 字段确实缺失（低版本）。"""
    ok, nbt, truncated = _inflate_prefix(comp, payload)
    if not ok:
        return None, "skip:compression"
    try:
        val = _inhabited_from_nbt(nbt)
        if val is not None or not truncated:
            return val, ""
    except Exception:
        if not truncated:
            return None, "skip:nbt"
    # 前缀不够（字段靠后或数据被截断）：全量解压再判一次，
    # 绝不能把「没解压完」当成「没有该字段」。
    ok2, full = _decompress_chunk(comp, payload)
    if not ok2:
        return None, "skip:compression"
    try:
        return _inhabited_from_nbt(full), ""
    except Exception:
        return None, "skip:nbt"


def parse_region(path: str, threshold_ticks: int,
                 inspect_only: bool = False,
                 max_chunks: Optional[int] = None,
                 keep_data: bool = True) -> RegionReport:
    """解析单个 .mca，给出每个区块的判定。

    inspect_only=True 时只做能力探测（不保留数据、不裁定删留）。
    max_chunks 用于能力探测：只解析前 N 个有数据的区块就提前停。
    keep_data=False 用于 dry-run 预览：只记录每个区块的数据长度用于估算
    释放空间，不把几百 KB 的原始字节拷进内存。apply() 会重新解析一次，
    所以预览完全不需要持有这些数据。
    """
    rep = RegionReport(path=path)
    try:
        size = os.path.getsize(path)
        if size < REGION_HEADER:
            return rep
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return rep

    loc = blob[:LOCATION_TABLE]
    ts_table = blob[LOCATION_TABLE:REGION_HEADER]

    for i in range(ENTRIES):
        if max_chunks is not None and rep.total >= max_chunks:
            break
        b = loc[i * 4:(i * 4) + 4]
        off = (b[0] << 16) | (b[1] << 8) | b[2]
        cnt = b[3]
        if off <= 0 or cnt <= 0:
            continue
        start = off * REGION_SECTOR
        if start < REGION_HEADER or start >= len(blob):
            continue
        rep.total += 1
        stamp = struct.unpack(">i", ts_table[i * 4:(i * 4) + 4])[0] \
            if len(ts_table) >= (i + 1) * 4 else 0

        v = ChunkVerdict(index=i, timestamp=stamp)
        # ── 先把原始扇区数据完整取到手 ──
        # 至关重要：无论 NBT 能否解析、压缩算法是否认识，这些字节都必须保留。
        # 曾经差点写成"解析失败就不赋值 data"，那样重写时会把无法解析的区块
        # 静默丢弃 —— 等于误删玩家数据。宁可不紧凑化，也绝不丢字节。
        # 只有真正要拿着字节去重写时才切片；预览/探测阶段不拷这几十 MB。
        raw_sectors = (blob[start:start + cnt * REGION_SECTOR]
                       if (keep_data and not inspect_only) else b"")
        val: Optional[int] = None          # 必须先初始化：压缩失败分支曾用未定义的 val
        try:
            if start + 4 > len(blob):
                raise ValueError("长度头截断")
            length = struct.unpack(">i", blob[start:start + 4])[0]
            if length <= 1 or start + 4 + length > len(blob):
                raise ValueError("区块数据越界")
            v.data_len = 4 + length
            if not inspect_only and keep_data:
                v.data = blob[start:start + 4 + length]
            comp = blob[start + 4]
            payload = blob[start + 5:start + 4 + length]

            if comp & COMP_EXTERNAL_BIT:
                # 压缩字节带最高位 => 数据在外部 .mcc 文件（MCA Selector 会去读，
                # 我们选择保守：整份 region 不参与清理，但字节原样保留，绝不误删）。
                v.reason = "skip:external"
                rep.unreadable += 1
            else:
                val, why = _read_inhabited(comp, payload)
                if why:
                    v.reason = why
                    rep.unreadable += 1
                elif val is None:
                    # 关键：字段缺失 ≠ 0。老版本存档不写 InhabitedTime，
                    # 这里必须保留，并在报告里累计以便上层禁用功能。
                    v.reason = "skip:missing_field"
                    rep.missing_field += 1
                else:
                    v.inhabited = val
                    v.reason = "prune" if val < threshold_ticks else "keep"
        except Exception:
            v.reason = "skip:truncated"
            rep.unreadable += 1
            # 读不出长度头时退回整个原始扇区，保证原样保留
            if not inspect_only and keep_data and raw_sectors:
                v.data = raw_sectors
                v.data_len = len(raw_sectors)

        if not inspect_only and keep_data and not v.data and raw_sectors:
            v.data = raw_sectors
            v.data_len = len(raw_sectors)

        rep.verdicts[i] = v

    for v in rep.verdicts.values():
        if v.reason == "prune":
            rep.prune += 1
        elif v.reason == "keep":
            rep.keep += 1
        else:
            rep.skip += 1
    return rep


# ------------------------------------------------------------------ 维度发现

def discover_dimensions(save_dir: str) -> List[dict]:
    """找出存档里所有维度的 region/entities/poi 三目录组。

    主世界： <save>/region, <save>/entities, <save>/poi
    下界/末地： <save>/DIM-1/..., <save>/DIM1/...
    自定义维度（1.16+）： <save>/dimensions/<ns>/<name>/...
    """
    out: List[dict] = []
    seen: Set[str] = set()
    for root, dirs, _files in os.walk(save_dir):
        # 不要钻进备份/同步状态目录
        dirs[:] = [d for d in dirs if d not in (".mcsync", "_MCSyncBackups")]
        if "region" not in dirs:
            continue
        rdir = os.path.join(root, "region")
        key = os.path.normcase(os.path.abspath(rdir))
        if key in seen:
            continue
        seen.add(key)
        rel = os.path.relpath(root, save_dir)
        norm = rel.replace("\\", "/")
        low = norm.lower()
        if rel in (".", ""):
            label = "主世界"
        elif rel.upper() == "DIM-1":
            label = "下界"
        elif rel.upper() == "DIM1":
            label = "末地"
        # 26.1 起默认维度搬到 dimensions/<namespace>/<name>/region，
        # 自定义维度也走这个结构。MCA Selector 同样是「找到 region 文件夹即为一维」，
        # 不做白名单，所以这里也一律纳入，只把官方三个维度翻成中文名。
        elif low.endswith("dimensions/minecraft/overworld"):
            label = "主世界 (26.1)"
        elif low.endswith("dimensions/minecraft/the_nether"):
            label = "下界 (26.1)"
        elif low.endswith("dimensions/minecraft/the_end"):
            label = "末地 (26.1)"
        else:
            label = norm
        out.append({
            "label": label,
            "root": root,
            "region": rdir,
            "entities": os.path.join(root, "entities"),
            "poi": os.path.join(root, "poi"),
        })
    return sorted(out, key=lambda d: d["label"])


def _region_coords(fname: str) -> Optional[Tuple[int, int]]:
    if not fname.lower().endswith(".mca"):
        return None
    parts = fname[:-4].split(".")
    if len(parts) != 3 or parts[0] != "r":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _global_chunk(rx: int, rz: int, index: int) -> Tuple[int, int]:
    return rx * 32 + (index % 32), rz * 32 + (index // 32)


def _index_of(rx: int, rz: int, cx: int, cz: int) -> int:
    return (cx - rx * 32) + (cz - rz * 32) * 32


# ------------------------------------------------------------------ 扫描预览

def diagnose(save_dir: str) -> dict:
    """逐维度抽样，给出可读 / 缺字段 / 不可读的具体数字。

    这份诊断会直接显示在页面上。以前只给一句「格式不兼容」，
    用户和开发者都无法判断到底卡在哪，来回猜了好几轮。
    """
    dims = discover_dimensions(save_dir)
    out = {"dims": [], "readable": 0, "missing": 0, "unreadable": 0,
           "external": 0, "regions": 0}
    for d in dims:
        rdir = d["region"]
        try:
            names = sorted(n for n in os.listdir(rdir)
                           if n.lower().endswith(".mca"))
        except OSError:
            names = []
        dr = dm = du = de = 0
        # 每个维度最多看 24 个 region、每个 region 最多 12 个区块。
        # 抽样必须在 parse_region 内部提前停，不能解析完 1024 个再截断。
        for fn in names[:24]:
            rep = parse_region(os.path.join(rdir, fn), 0,
                               inspect_only=True, max_chunks=12)
            out["regions"] += 1
            for v in rep.verdicts.values():
                if v.reason == "skip:missing_field":
                    dm += 1
                elif v.reason == "skip:external":
                    de += 1
                    du += 1
                elif v.reason.startswith("skip:"):
                    du += 1
                elif v.inhabited is not None:
                    dr += 1
            if dr >= 6:
                break
        out["dims"].append({"label": d["label"], "regions": len(names),
                            "readable": dr, "missing": dm,
                            "unreadable": du, "external": de})
        out["readable"] += dr
        out["missing"] += dm
        out["unreadable"] += du
        out["external"] += de
    return out


def capability(save_dir: str) -> Tuple[bool, str]:
    """判断该存档是否支持按 InhabitedTime 清理。

    用户要求：低版本存档不写 InhabitedTime 字段，必须禁用，避免把
    「没有字段」误判成「没人来过」而误删重要内容。

    但「解析失败」≠「低版本」。旧版只要有一个区块读不懂就把整个存档
    判成不支持，正是「MCA Selector 全能操作、本项目只有一个整合包能用」
    的根因（真实 1.18+ 区块 sections[].Y 是负 TAG_Byte，旧解析器会错位）。
    现在：只有 NBT 能完整解析、且确实全部缺字段时才禁用；读不懂的区块
    一律保留不删，但不再牵连整个存档。
    """
    dg = diagnose(save_dir)
    if not dg["dims"]:
        return False, "未找到 region 目录（不是有效存档，或存档里还没有区块）"

    readable, missing, unreadable = dg["readable"], dg["missing"], dg["unreadable"]
    parts = [f"识别 {len(dg['dims'])} 个维度 / {dg['regions']} 个抽样 region",
             f"可读区块 {readable}"]
    if missing:
        parts.append(f"缺字段 {missing}")
    if unreadable:
        parts.append(f"不可读 {unreadable}（自动保留不删）")
    if dg["external"]:
        parts.append(f"外部 .mcc {dg['external']}（整份 region 保留）")
    detail = "；".join(parts)

    if readable == 0:
        # 只有「解析全都成功、但字段确实不存在」才能判定为低版本。
        if unreadable == 0 and missing > 0:
            return False, ("该存档（低版本 MC）不写入 InhabitedTime 字段，"
                           "无法判断玩家停留时间，已禁用区块清理以避免误删。"
                           f"（{detail}）")
        if unreadable == 0:
            return False, ("region 里没有可读取的区块数据。"
                           f"（{detail}）")
        # 读不懂但并非低版本：不禁用。scan 会把这些区块标 skip 保留，
        # 能读的维度照常清理。宁可少删，不可整体罢工。
        return True, (f"部分区块格式无法解析，已自动排除；其余可正常筛选。"
                      f"（{detail}）")

    skipped = sum(1 for d in dg["dims"] if not d["readable"])
    if skipped:
        detail += f"；{skipped} 个维度未读到时间戳，将整体保留"
    return True, detail


def scan(save_dir: str, threshold_seconds: float = DEFAULT_MAX_SECONDS,
         selected_dims: Optional[Set[str]] = None) -> PruneReport:
    """dry-run：一个字节都不修改，只出报告。"""
    ticks = max(0, int(float(threshold_seconds) * TICKS_PER_SECOND))
    supported, why = capability(save_dir)
    rep = PruneReport(save_dir=save_dir, threshold_seconds=float(threshold_seconds),
                      threshold_ticks=ticks, supported=supported,
                      unsupported_reason=why)
    if not supported:
        return rep

    for d in discover_dimensions(save_dir):
        if selected_dims is not None and d["label"] not in selected_dims:
            continue
        rdir = d["region"]
        try:
            names = sorted(n for n in os.listdir(rdir) if n.lower().endswith(".mca"))
        except OSError:
            continue

        # 第一步：只从 region/ 判定哪些全局区块要删。
        # entities/poi 里没有 InhabitedTime 字段，不能自己判，必须跟随 region。
        to_prune: Set[Tuple[int, int]] = set()
        for fn in names:
            coords = _region_coords(fn)
            if not coords:
                continue
            rx, rz = coords
            r = parse_region(os.path.join(rdir, fn), ticks, keep_data=False)
            r.dimension, r.kind = d["label"], "region"
            # 含"外部 .mcc 存储"的 region 结构特殊，重写有不确定性 ——
            # 整个文件不参与清理，宁可少删也不冒险。
            if any(v.reason == "skip:external" for v in r.verdicts.values()):
                for v in r.verdicts.values():
                    if v.reason == "prune":
                        v.reason = "skip:external_region"
            for idx, v in r.verdicts.items():
                if v.reason == "prune":
                    to_prune.add(_global_chunk(rx, rz, idx))
            _finish_region(r)
            rep.regions.append(r)

        # 第二步：三目录协同。entities/poi 按同一坐标集合清掉对应项。
        for kind in ("entities", "poi"):
            sdir = d[kind]
            if not os.path.isdir(sdir):
                continue
            for fn in sorted(os.listdir(sdir)):
                coords = _region_coords(fn)
                if not coords:
                    continue
                rx, rz = coords
                r = parse_region(os.path.join(sdir, fn), ticks, keep_data=False)
                r.dimension, r.kind = d["label"], kind
                for idx, v in list(r.verdicts.items()):
                    g = _global_chunk(rx, rz, idx)
                    if g in to_prune:
                        v.reason = "prune"
                    elif v.reason == "prune":
                        # region 判定保留，则这里也保留（不会走到，保险）
                        v.reason = "keep"
                _finish_region(r)
                rep.regions.append(r)

    _aggregate(rep)
    return rep


def _finish_region(r: RegionReport) -> None:
    r.prune = sum(1 for v in r.verdicts.values() if v.reason == "prune")
    r.keep = sum(1 for v in r.verdicts.values() if v.reason == "keep")
    r.skip = sum(1 for v in r.verdicts.values() if v.reason.startswith("skip:"))
    kept = r.total - r.prune
    r.will_delete_file = (r.total > 0 and kept == 0)
    # 预计释放：被删 chunk 的数据扇区（近似，按 4K 对齐）
    freed = 0
    for v in r.verdicts.values():
        if v.reason == "prune":
            # data_len 已含 4 字节长度头，不要再额外加；
            # 预览时 v.data 为空，必须走 data_len。
            n = v.data_len or len(v.data)
            freed += ((n + REGION_SECTOR - 1) // REGION_SECTOR) * REGION_SECTOR
    r.freed_bytes = freed


def _aggregate(rep: PruneReport) -> None:
    for r in rep.regions:
        # 统计口径：区块数只按 region/ 算。
        # entities/poi 是同一批区块的附属数据，把它们也计进去会变成 3 倍虚高，
        # 报告就失去参考价值了。文件数与释放空间仍要覆盖三个目录。
        if r.kind == "region":
            rep.total_chunks += r.total
            rep.prune_chunks += r.prune
            rep.skipped_chunks += r.skip
            if r.prune:
                rep.dimensions[r.dimension] = rep.dimensions.get(r.dimension, 0) + r.prune
        rep.freed_bytes += r.freed_bytes
        if r.prune or r.will_delete_file:
            rep.affected_files += 1
        if r.will_delete_file and r.prune:
            rep.delete_files += 1


# ------------------------------------------------------------------ 执行

def rewrite_region(path: str, verdicts: Dict[int, ChunkVerdict]) -> Tuple[int, bool]:
    """按判定重写 .mca：清表项 + 紧凑化数据区。返回 (释放字节, 是否删了整个文件)。

    保留区块的原始字节（含压缩类型）原样搬过去，不重新压缩、不改 NBT，
    因此不会引入任何格式兼容风险。
    """
    before = os.path.getsize(path)
    survivors = [(i, v) for i, v in sorted(verdicts.items())
                 if v.reason != "prune" and v.data]
    if not any(v.reason == "prune" for v in verdicts.values()):
        return 0, False
    if not survivors:
        os.remove(path)
        return before, True

    loc = bytearray(LOCATION_TABLE)
    ts = bytearray(LOCATION_TABLE)
    body = bytearray()
    sector = 2
    for i, v in survivors:
        nsec = (len(v.data) + REGION_SECTOR - 1) // REGION_SECTOR
        if nsec > 255:
            nsec = 255      # 理论上限，实际 chunk 远小于 1MB
        loc[i * 4:(i * 4) + 4] = bytes(
            [(sector >> 16) & 0xFF, (sector >> 8) & 0xFF, sector & 0xFF, nsec])
        ts[i * 4:(i * 4) + 4] = struct.pack(">i", int(v.timestamp or 0))
        body.extend(v.data)
        pad = nsec * REGION_SECTOR - len(v.data)
        if pad > 0:
            body.extend(b"\x00" * pad)
        sector += nsec

    tmp = path + ".mcsync_tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(bytes(loc) + bytes(ts) + bytes(body))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    after = os.path.getsize(path)
    return max(0, before - after), False


def backup_save(save_dir: str) -> str:
    """执行前强制备份（硬链接优先，省空间且快）。"""
    root = os.path.join(os.path.dirname(os.path.normpath(save_dir)), "_MCSyncBackups")
    os.makedirs(root, exist_ok=True)
    name = os.path.basename(os.path.normpath(save_dir))
    dst = os.path.join(root, f"{name}-{time.strftime('%Y%m%d-%H%M%S')}-区块清理前")
    os.makedirs(dst, exist_ok=True)
    for r, dirs, files in os.walk(save_dir):
        dirs[:] = [d for d in dirs if d not in (".mcsync", "_MCSyncBackups")]
        rel = os.path.relpath(r, save_dir)
        target = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target, exist_ok=True)
        for fn in files:
            s, d = os.path.join(r, fn), os.path.join(target, fn)
            try:
                os.link(s, d)
            except Exception:
                try:
                    shutil.copy2(s, d)
                except OSError:
                    pass
    return dst


def apply(report: PruneReport) -> dict:
    """真正执行清理。调用方必须已完成：预览确认 + 二次确认 + 游戏关闭检查。"""
    if not report.supported:
        raise RuntimeError(report.unsupported_reason or "该存档不支持区块清理")
    if report.prune_chunks <= 0:
        raise RuntimeError("没有可清理的区块")

    backup = backup_save(report.save_dir)
    freed, deleted_files, touched = 0, 0, 0
    errors: List[str] = []
    for r in report.regions:
        if r.prune <= 0:
            continue
        # 重新解析一次，避免用预览时缓存的数据（预览与执行之间文件可能变了）
        fresh = parse_region(r.path, report.threshold_ticks)
        if fresh.total == 0 and r.will_delete_file:
            continue
        # 以预览的判定为准，但只删"预览判定为 prune 且新解析仍存在"的项
        new_verdicts: Dict[int, ChunkVerdict] = {}
        for idx, v in fresh.verdicts.items():
            old = r.verdicts.get(idx)
            if old and old.reason == "prune":
                v.reason = "prune"
            new_verdicts[idx] = v
        try:
            n, removed = rewrite_region(r.path, new_verdicts)
            freed += n
            touched += 1
            if removed:
                deleted_files += 1
        except Exception as e:
            errors.append(f"{os.path.basename(r.path)}: {e}")

    return {
        "ok": not errors,
        "backup": backup,
        "freed_bytes": freed,
        "touched_files": touched,
        "deleted_files": deleted_files,
        "prune_chunks": report.prune_chunks,
        "errors": errors,
    }
