# -*- coding: utf-8 -*-
"""
区块清理 · 真实区块 NBT 兼容性回归（本次「只有一个整合包能用」的根因）

背景：
  core/nbt.py 的 TAG_BYTE 曾写成条件表达式，负 BYTE 会多读一个字节。
  1.18+ 区块的 sections[].Y 从 -4 开始（字节 0xFC），必然触发，
  导致整份 NBT 解析错位 -> 所有区块标 skip:nbt -> capability 判
  「无法读取任何区块」-> 页面禁用。只有恰好没踩到的那个整合包能用。

覆盖：
  1. 负 TAG_BYTE 的 NBT 往返（nbt.py 回归）
  2. 1.12/1.16 的 Level 嵌套、1.18/1.20/1.21 的根结构都能读出 InhabitedTime
  3. 真实风格大区块（sections / palette / LongArray / 嵌套 List<Compound>）
     -> capability True、scan 命中、apply 后保留区块字节可还原
  4. LZ4 / GZIP / ZLIB / UNCOMPRESSED 四种压缩全部可读
  5. 外部 .mcc 标记仍保守保留
  6. 低版本（确实无字段）依旧禁用，即使 NBT 里含负 BYTE
  7. 前缀解压回退：字段排在巨大 payload 之后仍能读到
  8. 诊断数字准确

用法： python3 tools/test_chunk_prune_real.py
"""
from __future__ import annotations

import gzip
import os
import shutil
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from core import chunk_prune as cp          # noqa: E402
from core import nbt_typed as N             # noqa: E402
from core import nbt_scan                   # noqa: E402
from core.nbt import NBTReader, read_nbt    # noqa: E402
import test_chunk_prune as h                # noqa: E402

TMP = "/tmp/mcsync_prune_real"
OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def T(t, v):
    return N.NBTTag(t, v)


# ------------------------------------------------------------------ NBT 构造
def modern_chunk(inhabited: int, negative_byte: bool = True) -> bytes:
    """1.18+ 风格：根下 InhabitedTime，sections 里 Y 可为 -4（负 TAG_Byte）。"""
    sections = []
    for y in range(4):
        palette = [T(N.TAG_COMPOUND, {
            "Name": T(N.TAG_STRING, "minecraft:stone"),
            "Properties": T(N.TAG_COMPOUND, {"snowy": T(N.TAG_STRING, "false")})})] * 8
        sections.append(T(N.TAG_COMPOUND, {
            "Y": T(N.TAG_BYTE, (y - 4) if negative_byte else y),
            "block_states": T(N.TAG_COMPOUND, {
                "palette": T(N.TAG_LIST, (N.TAG_COMPOUND, palette)),
                "data": T(N.TAG_LONG_ARRAY, [y * 311 + q for q in range(256)])}),
            "BlockLight": T(N.TAG_BYTE_ARRAY, bytes(2048)),
            "SkyLight": T(N.TAG_BYTE_ARRAY, bytes(2048))}))
    root = T(N.TAG_COMPOUND, {
        "DataVersion": T(N.TAG_INT, 3700),
        "xPos": T(N.TAG_INT, 3), "yPos": T(N.TAG_INT, -4), "zPos": T(N.TAG_INT, 7),
        "Status": T(N.TAG_STRING, "full"), "LastUpdate": T(N.TAG_LONG, 999),
        "InhabitedTime": T(N.TAG_LONG, inhabited),
        "sections": T(N.TAG_LIST, (N.TAG_COMPOUND, sections)),
        "Heightmaps": T(N.TAG_COMPOUND, {
            "MOTION_BLOCKING": T(N.TAG_LONG_ARRAY, [y for y in range(37)])}),
        "structures": T(N.TAG_COMPOUND, {"starts": T(N.TAG_COMPOUND, {})})})
    return N.TypedWriter().dump("", root)


def legacy_chunk(inhabited) -> bytes:
    """1.12 / 1.16 风格：InhabitedTime 嵌在 Level 下，含负 BYTE 的 Light 数据。"""
    inner = {} if inhabited is None else {"InhabitedTime": T(N.TAG_LONG, inhabited)}
    inner.update({
        "xPos": T(N.TAG_INT, 1), "zPos": T(N.TAG_INT, 2),
        "Status": T(N.TAG_STRING, "full"),
        "Sections": T(N.TAG_LIST, (N.TAG_COMPOUND, [
            T(N.TAG_COMPOUND, {"Y": T(N.TAG_BYTE, -1),
                               "BlockLight": T(N.TAG_BYTE_ARRAY, bytes(2048))})])),
        "Entities": T(N.TAG_LIST, (N.TAG_COMPOUND, [])),
        "TileEntities": T(N.TAG_LIST, (N.TAG_COMPOUND, [])),
        "Biomes": T(N.TAG_INT_ARRAY, [1] * 256)})
    root = T(N.TAG_COMPOUND, {
        "DataVersion": T(N.TAG_INT, 1343),
        "Level": T(N.TAG_COMPOUND, inner)})
    return N.TypedWriter().dump("", root)


def lz4_store(data: bytes) -> bytes:
    """纯 literal 的合法 LZ4 block（只用扩展长度，不做 match 压缩）。"""
    n = len(data)
    out = bytearray([0xF0])          # lit=15(扩展), match=0
    rem = n - 15
    while rem >= 255:
        out.append(255)
        rem -= 255
    out.append(rem)
    out += data
    return bytes(out)


def packed_nbt(nbt: bytes, comp: int) -> bytes:
    if comp == cp.COMP_ZLIB:
        return zlib.compress(nbt)
    if comp == cp.COMP_GZIP:
        return gzip.compress(nbt)
    if comp in (cp.COMP_NONE, cp.COMP_UNCOMPRESSED):
        return nbt
    if comp == cp.COMP_LZ4:
        return lz4_store(nbt)
    return nbt


def make_region(path: str, chunks: dict, comp: int = cp.COMP_ZLIB,
                legacy: bool = False, negative: bool = True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    loc = bytearray(cp.LOCATION_TABLE)
    ts = bytearray(cp.LOCATION_TABLE)
    body = bytearray()
    sector = 2
    for idx, val in chunks.items():
        if val == "raw":
            raw = b"\xde\xad\xbe\xef" * 40
            data = struct.pack(">i", len(raw) + 1) + bytes([99]) + raw
        else:
            nbt = (legacy_chunk(val) if legacy else modern_chunk(val, negative))
            packed = packed_nbt(nbt, comp)
            data = struct.pack(">i", len(packed) + 1) + bytes([comp]) + packed
        nsec = (len(data) + cp.REGION_SECTOR - 1) // cp.REGION_SECTOR
        loc[idx * 4:(idx * 4) + 4] = bytes(
            [(sector >> 16) & 255, (sector >> 8) & 255, sector & 255, min(nsec, 255)])
        ts[idx * 4:(idx * 4) + 4] = struct.pack(">i", 1700000000 + idx)
        body += data + b"\x00" * (nsec * cp.REGION_SECTOR - len(data))
        sector += nsec
    open(path, "wb").write(bytes(loc) + bytes(ts) + bytes(body))


def make_save(name: str, chunks: dict, comp=cp.COMP_ZLIB,
              legacy=False, negative=True) -> str:
    base = os.path.join(TMP, name)
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(os.path.join(base, "region"), exist_ok=True)
    make_region(os.path.join(base, "region", "r.0.0.mca"),
                chunks, comp, legacy, negative)
    root = T(N.TAG_COMPOUND, {"Data": T(N.TAG_COMPOUND, {
        "LevelName": T(N.TAG_STRING, name), "DataVersion": T(N.TAG_INT, 3700)})})
    N.write_file(os.path.join(base, "level.dat"), "", root, "gzip")
    return base


def main():
    print("\n=== 区块清理 · 真实 NBT 兼容性（根治「只有一个整合包能用」） ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    # ---------------- 1. nbt.py 负 BYTE 回归 ----------------
    print("[1] core/nbt.py 的 TAG_BYTE 不再多吞字节")
    nbt = modern_chunk(123, negative_byte=True)
    try:
        _n, root = NBTReader(nbt).parse()
        check("NBTReader 能解析含负 BYTE 的真实风格 NBT",
              root.get("InhabitedTime") == 123, str(root.get("InhabitedTime")))
        check("负 Y 值读出来是 -4",
              root["sections"][0]["Y"] == -4, str(root["sections"][0]["Y"]))
    except Exception as e:
        check("NBTReader 能解析含负 BYTE 的真实风格 NBT", False, str(e))
    check("轻量扫描器能提取 InhabitedTime",
          nbt_scan.extract_inhabited(nbt) == 123,
          str(nbt_scan.extract_inhabited(nbt)))
    check("保类型解析器也能提取", cp._from_typed(
        N.TypedReader(nbt).parse()[1]) == 123)

    # ---------------- 2. 各版本结构 ----------------
    print("\n[2] 1.12 / 1.16（Level 嵌套）与 1.18+（根）都能读到")
    lg = legacy_chunk(7)
    check("legacy Level.InhabitedTime", nbt_scan.extract_inhabited(lg) == 7,
          str(nbt_scan.extract_inhabited(lg)))
    check("modern 根 InhabitedTime", nbt_scan.extract_inhabited(nbt) == 123)
    check("legacy 缺字段返回 None",
          nbt_scan.extract_inhabited(legacy_chunk(None)) is None)

    # ---------------- 3. 各版本整合包都能通过能力检测并筛出区块 ----------------
    print("\n[3] 模拟多个不同 MC 版本的整合包，全部可用")
    versions = {
        "MC-1.12.2-Forge": dict(legacy=True, comp=cp.COMP_ZLIB, negative=True),
        "MC-1.16.5-Forge": dict(legacy=True, comp=cp.COMP_GZIP, negative=True),
        "MC-1.20.1-Forge": dict(legacy=False, comp=cp.COMP_ZLIB, negative=True),
        "MC-1.21.1-Fabric": dict(legacy=False, comp=cp.COMP_UNCOMPRESSED, negative=True),
        "MC-26.1-LZ4": dict(legacy=False, comp=cp.COMP_LZ4, negative=True),
    }
    chunks = {0: 0, 1: 19, 2: 20, 3: 5000, 4: 90000, 5: "raw"}
    for label, kw in versions.items():
        path = make_save(label, chunks, **kw)
        ok, why = cp.capability(path)
        check(f"{label} 能力检测通过", ok, why)
        rep = cp.scan(path, 1.0)
        check(f"{label} 能筛出低停留区块", rep.prune_chunks == 2,
              f"prune={rep.prune_chunks} supported={rep.supported} {rep.unsupported_reason}")
        check(f"{label} 读不懂的区块被保留", rep.skipped_chunks >= 1,
              f"skip={rep.skipped_chunks}")

    # ---------------- 4. 压缩类型逐个体检 ----------------
    print("\n[4] 四种压缩字节都能读出 InhabitedTime")
    for comp, label in ((cp.COMP_NONE, "NONE 0"), (cp.COMP_GZIP, "GZIP 1"),
                        (cp.COMP_ZLIB, "ZLIB 2"),
                        (cp.COMP_UNCOMPRESSED, "UNCOMPRESSED 3"),
                        (cp.COMP_LZ4, "LZ4 4")):
        p = os.path.join(TMP, "comp", f"r.{comp}.0.mca")
        make_region(p, {0: 5, 1: 9000}, comp=comp)
        rep = cp.parse_region(p, 20)
        vals = sorted(v.inhabited for v in rep.verdicts.values()
                      if v.inhabited is not None)
        check(f"{label} 解析成功", vals == [5, 9000], str(vals))
    check("纯 Python LZ4 解压正确",
          cp._lz4_block(lz4_store(nbt)) == nbt)

    # ---------------- 5. 外部 .mcc 仍保守保留 ----------------
    print("\n[5] 外部 .mcc 标记保守保留")
    ext = os.path.join(TMP, "comp", "r.ext.0.mca")
    make_region(ext, {0: 0, 1: 9000}, comp=cp.COMP_ZLIB)
    blob = bytearray(open(ext, "rb").read())
    start = 2 * cp.REGION_SECTOR
    blob[start + 4] |= cp.COMP_EXTERNAL_BIT
    open(ext, "wb").write(bytes(blob))
    rep_ext = cp.parse_region(ext, 20)
    check("标记 skip:external",
          any(v.reason == "skip:external" for v in rep_ext.verdicts.values()),
          str([v.reason for v in rep_ext.verdicts.values()]))
    check("原始字节仍被保留", all(v.data for v in rep_ext.verdicts.values()))

    # ---------------- 6. 低版本仍禁用 ----------------
    print("\n[6] 确实不写 InhabitedTime 的低版本依旧禁用")
    low = make_save("LowVersion", {0: None, 1: None},
                    legacy=True, negative=True, comp=cp.COMP_ZLIB)
    ok, why = cp.capability(low)
    check("低版本被禁用", not ok, why)
    check("禁用理由说明是缺字段", "不写入 InhabitedTime" in why, why)
    check("scan 不产出删除", cp.scan(low, 1.0).prune_chunks == 0)

    # ---------------- 7. 前缀解压回退 ----------------
    print("\n[7] 字段排在巨大 payload 之后时能全量回退")
    # 把 InhabitedTime 放到最后一个键，前面塞 200KB 的 ByteArray，
    # 64KB 前缀必然不够，验证不会误判成「没有字段」。
    root = T(N.TAG_COMPOUND, {
        "BigData": T(N.TAG_BYTE_ARRAY, bytes(220 * 1024)),
        "InhabitedTime": T(N.TAG_LONG, 3)})
    late = N.TypedWriter().dump("", root)
    p = os.path.join(TMP, "late", "r.0.0.mca")
    packed = zlib.compress(late)
    data = struct.pack(">i", len(packed) + 1) + bytes([cp.COMP_ZLIB]) + packed
    nsec = (len(data) + cp.REGION_SECTOR - 1) // cp.REGION_SECTOR
    loc = bytearray(cp.LOCATION_TABLE); ts = bytearray(cp.LOCATION_TABLE)
    loc[0:4] = bytes([0, 0, 2, min(nsec, 255)])
    ts[0:4] = struct.pack(">i", 1700000000)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(bytes(loc) + bytes(ts) + data +
                        b"\x00" * (nsec * cp.REGION_SECTOR - len(data)))
    rep_late = cp.parse_region(p, 20)
    check("仍读到 InhabitedTime=3",
          any(v.inhabited == 3 for v in rep_late.verdicts.values()),
          str([(v.reason, v.inhabited) for v in rep_late.verdicts.values()]))
    check("被判定为可清理", rep_late.prune == 1, str(rep_late.prune))

    # ---------------- 8. 诊断数字 ----------------
    print("\n[8] 诊断信息具体可核对")
    path = make_save("DiagWorld", chunks, comp=cp.COMP_ZLIB)
    dg = cp.diagnose(path)
    check("诊断含维度列表", len(dg["dims"]) >= 1, str(dg["dims"]))
    check("诊断统计了可读区块", dg["readable"] > 0, str(dg))
    ok, why = cp.capability(path)
    check("可用说明里带可读区块数", "可读区块" in why, why)

    # ---------------- 9. 执行后字节可还原 ----------------
    print("\n[9] 真实风格区块执行清理后，保留区块字节完好")
    path = make_save("ApplyWorld", {0: 0, 1: 5000, 2: 19, 3: 90000},
                     comp=cp.COMP_ZLIB)
    before = h.read_all_chunks(os.path.join(path, "region", "r.0.0.mca"))
    rep = cp.scan(path, 1.0)
    r = cp.apply(rep)
    check("执行成功", r.get("ok") is True, str(r))
    after = h.read_all_chunks(os.path.join(path, "region", "r.0.0.mca"))
    check("只删了 2 个低停留区块", len(after) == 2, str(sorted(after)))
    check("保留区块的原始字节未变",
          all(after[i] == before[i] for i in after), "")

    print("\n" + "=" * 64)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 各版本真实区块均可筛选")
    print("=" * 64)


if __name__ == "__main__":
    main()
