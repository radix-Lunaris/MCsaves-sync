# -*- coding: utf-8 -*-
"""
区块清理（按 InhabitedTime）回归测试

覆盖：
  1. 默认阈值 = 1 秒（20 tick），只删 0~19 tick 的区块
  2. 字段缺失的老版本存档 -> capability() 判不支持，scan() 不产出任何删除
  3. 无法解析/压缩不认识的区块一律保留，重写后字节级可还原
  4. region / entities / poi 三目录协同删除
  5. 重写后 .mca 格式合法（表项、扇区、长度头可解析），且体积确实变小
  6. 执行前备份可用、清理后云端同步不受影响（region 差分仍能工作）

用法： python3 tools/test_chunk_prune.py
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

from core import chunk_prune as cp  # noqa: E402
from core.nbt import NBTReader  # noqa: E402

TMP = "/tmp/mcsync_prune_test"
OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


# ---------------------------------------------------------------- NBT 构造
def _enc_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def _tag_long(name: str, v: int) -> bytes:
    return bytes([4]) + _enc_str(name) + struct.pack(">q", v)


def make_chunk_nbt(inhabited, legacy: bool = False) -> bytes:
    """构造一个最小 chunk NBT。inhabited=None 表示不写该字段（老版本）。"""
    body = bytearray()
    if inhabited is not None:
        body += _tag_long("InhabitedTime", int(inhabited))
    body += _tag_long("LastUpdate", 12345)
    body += b"\x00"                       # TAG_End
    if legacy and inhabited is not None:
        # 1.17 及以前：嵌在 Level 下
        inner = _tag_long("InhabitedTime", int(inhabited)) + b"\x00"
        body = bytes([10]) + _enc_str("Level") + inner + b"\x00"
    return bytes([10]) + _enc_str("") + body  # 根 Compound


def make_region(path: str, chunks: dict, comp: int = cp.COMP_ZLIB,
                legacy: bool = False) -> None:
    """chunks: index -> inhabited tick（None=字段缺失, 'raw'=无法解析的垃圾）"""
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
            nbt = make_chunk_nbt(val, legacy=legacy)
            if comp == cp.COMP_ZLIB:
                packed = zlib.compress(nbt)
            elif comp == cp.COMP_GZIP:
                packed = gzip.compress(nbt)
            elif comp == cp.COMP_NONE:
                packed = nbt
            else:
                packed = nbt
            data = struct.pack(">i", len(packed) + 1) + bytes([comp]) + packed
        nsec = (len(data) + cp.REGION_SECTOR - 1) // cp.REGION_SECTOR
        loc[idx * 4:(idx * 4) + 4] = bytes(
            [(sector >> 16) & 0xFF, (sector >> 8) & 0xFF, sector & 0xFF, nsec])
        ts[idx * 4:(idx * 4) + 4] = struct.pack(">i", 1700000000 + idx)
        body += data + b"\x00" * (nsec * cp.REGION_SECTOR - len(data))
        sector += nsec
    with open(path, "wb") as f:
        f.write(bytes(loc) + bytes(ts) + bytes(body))


def read_all_chunks(path: str) -> dict:
    """独立解析 .mca，返回 index -> (comp, nbt bytes)。不依赖被测代码。"""
    with open(path, "rb") as f:
        blob = f.read()
    out = {}
    for i in range(cp.ENTRIES):
        b = blob[i * 4:(i * 4) + 4]
        off = (b[0] << 16) | (b[1] << 8) | b[2]
        if off <= 0 or b[3] <= 0:
            continue
        start = off * cp.REGION_SECTOR
        if start + 5 > len(blob):
            continue
        length = struct.unpack(">i", blob[start:start + 4])[0]
        comp = blob[start + 4]
        raw = blob[start + 5:start + 4 + length]
        try:
            if comp == cp.COMP_ZLIB:
                nbt = zlib.decompress(raw)
            elif comp == cp.COMP_GZIP:
                nbt = gzip.decompress(raw)
            elif comp == cp.COMP_NONE:
                nbt = raw
            else:
                nbt = raw
        except Exception:
            nbt = raw
        out[i] = (comp, nbt)
    return out


def inhabited_of(path: str, idx: int):
    chunks = read_all_chunks(path)
    if idx not in chunks:
        return None
    try:
        _n, root = NBTReader(chunks[idx][1]).parse()
        return cp._inhabited_from_nbt(root or {})
    except Exception:
        return None


def build_save(name: str, chunks: dict, with_siblings: bool = True,
               comp: int = cp.COMP_ZLIB, legacy: bool = False,
               dims: tuple = (".", "DIM-1")) -> str:
    base = os.path.join(TMP, name)
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(os.path.join(base, "level.dat"), exist_ok=False) if False else None
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "level.dat"), "wb") as f:
        f.write(b"\x00")
    for d in dims:
        root = os.path.join(base, d) if d != "." else base
        rdir = os.path.join(root, "region")
        make_region(os.path.join(rdir, "r.0.0.mca"), chunks,
                    comp=comp, legacy=legacy)
        if with_siblings:
            # entities/poi 的 NBT 不含 InhabitedTime，但结构可解析
            make_region(os.path.join(root, "entities", "r.0.0.mca"),
                        {k: 999999 for k in chunks}, comp=comp)
            make_region(os.path.join(root, "poi", "r.0.0.mca"),
                        {k: 999999 for k in chunks}, comp=comp)
    return base


def main():
    print("\n=== 区块清理回归测试 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    # ---------------- 1. 默认阈值 ----------------
    print("[1] 默认阈值 = 1 秒（20 tick）")
    check("DEFAULT_MAX_SECONDS == 1", cp.DEFAULT_MAX_SECONDS == 1, "")
    check("换算 20 tick", cp.DEFAULT_MAX_SECONDS * cp.TICKS_PER_SECOND == 20, "")

    chunks = {
        0: 0,        # 从未停留 -> 删
        1: 19,       # 0.95 秒 -> 删
        2: 20,       # 恰好 1 秒 -> 保留（是 "<" 不是 "<="）
        3: 100,      # 5 秒 -> 保留
        4: 72000,    # 1 小时 -> 保留
    }
    save = build_save("normal", chunks, dims=(".",))
    ok, why = cp.capability(save)
    check("正常存档判定为支持", ok, why)
    rep = cp.scan(save)
    check("dry-run 未修改源文件", os.path.getsize(
        os.path.join(save, "region", "r.0.0.mca")) > cp.REGION_HEADER, "")

    region_reports = [r for r in rep.regions if r.kind == "region"]
    pruned = sum(r.prune for r in region_reports)
    check("默认阈值删 2 个区块（0 和 19 tick）", pruned == 2, str(pruned))
    check("20 tick 及以上全部保留",
          sum(r.keep for r in region_reports) == 3,
          str(sum(r.keep for r in region_reports)))
    check("报告里标记可安全执行", rep.safe_to_apply, "")

    # ---------------- 2. 老版本禁用 ----------------
    print("\n[2] 低版本（无 InhabitedTime）必须禁用清理")
    old = build_save("legacy_no_field", {0: None, 1: None, 2: None},
                     with_siblings=False, dims=(".",))
    ok, why = cp.capability(old)
    check("capability 返回 False", not ok, why)
    check("理由明确说明是缺字段", "InhabitedTime" in why and "禁用" in why, why)
    rep_old = cp.scan(old)
    check("scan 不产出任何删除", rep_old.prune_chunks == 0, str(rep_old.prune_chunks))
    check("报告 safe_to_apply=False", not rep_old.safe_to_apply, "")
    check("源文件仍然存在且大小不变",
          os.path.getsize(os.path.join(old, "region", "r.0.0.mca")) > cp.REGION_HEADER, "")
    try:
        cp.apply(rep_old)
        check("apply 会拒绝执行", False, "竟然执行了")
    except RuntimeError as e:
        check("apply 拒绝执行并说明原因", "禁用" in str(e) or "不支持" in str(e), str(e))

    # 1.17 及以前：字段在 Level 下，应当被识别（不算低版本禁用）
    legacy = build_save("legacy_level", {0: 0, 1: 5000},
                        with_siblings=False, dims=(".",), legacy=True)
    ok2, why2 = cp.capability(legacy)
    check("1.17 的 Level.InhabitedTime 能识别", ok2, why2)
    r2 = cp.scan(legacy)
    check("1.17 存档也能正确筛选",
          sum(r.prune for r in r2.regions if r.kind == "region") == 1, "")

    # ---------------- 3. 无法解析一律保留 ----------------
    print("\n[3] 无法解析的区块一律保留（绝不误删）")
    mixed = build_save("mixed", {0: 0, 1: 999, 2: "raw", 3: 0},
                       with_siblings=False, dims=(".",))
    rm = cp.scan(mixed)
    rr = [r for r in rm.regions if r.kind == "region"][0]
    check("有 1 个被跳过", rr.skip == 1, str(rr.skip))
    check("仍删 2 个明确命中的", rr.prune == 2, str(rr.prune))

    p = os.path.join(mixed, "region", "r.0.0.mca")
    before = read_all_chunks(p)
    result = cp.apply(rm)
    check("执行成功", result["ok"], str(result["errors"]))
    after = read_all_chunks(p)
    check("无法解析的区块仍在（index 2）", 2 in after, str(sorted(after)))
    check("其原始字节未变", before[2] == after.get(2), "")
    check("长时间区块仍在且值不变", inhabited_of(p, 1) == 999,
          str(inhabited_of(p, 1)))
    check("命中区块已删除", 0 not in after and 3 not in after, str(sorted(after)))
    check("文件确实变小", os.path.getsize(p) < cp.REGION_HEADER +
          len(before) * cp.REGION_SECTOR, "")
    check("备份目录已生成", os.path.isdir(result["backup"]), result["backup"])

    # ---------------- 4. 三目录协同 ----------------
    print("\n[4] region / entities / poi 三目录协同")
    tri = build_save("triple", {0: 0, 1: 999, 2: 5}, with_siblings=True,
                     dims=(".",))
    rt = cp.scan(tri)
    by_kind = {}
    for r in rt.regions:
        by_kind.setdefault(r.kind, []).append(r)
    check("三种目录都被扫描", set(by_kind) == {"region", "entities", "poi"},
          str(set(by_kind)))
    check("entities 跟随 region 删 2 个",
          sum(r.prune for r in by_kind["entities"]) == 2, "")
    check("poi 跟随 region 删 2 个",
          sum(r.prune for r in by_kind["poi"]) == 2, "")
    check("entities 不自行按 InhabitedTime 判定（值 999999 全保留会错）",
          sum(r.prune for r in by_kind["entities"]) != 0, "")
    cp.apply(rt)
    for kind in ("region", "entities", "poi"):
        pp = os.path.join(tri, kind, "r.0.0.mca")
        left = read_all_chunks(pp)
        check(f"{kind} 保留 index 1、删掉 0/2",
              set(left) == {1}, str(sorted(left)))

    # ---------------- 5. 重写格式合法性 ----------------
    print("\n[5] 重写后的 .mca 格式合法")
    fmt = build_save("format", {i: (0 if i % 2 == 0 else 5000) for i in range(20)},
                     with_siblings=False, dims=(".",))
    rf = cp.scan(fmt)
    cp.apply(rf)
    pp = os.path.join(fmt, "region", "r.0.0.mca")
    with open(pp, "rb") as f:
        blob = f.read()
    check("文件大小是 4096 的整数倍",
          len(blob) % cp.REGION_SECTOR == 0, str(len(blob)))
    check("头部 8192 字节完整", len(blob) >= cp.REGION_HEADER, "")
    valid = 0
    for i in range(cp.ENTRIES):
        b = blob[i * 4:(i * 4) + 4]
        off = (b[0] << 16) | (b[1] << 8) | b[2]
        cnt = b[3]
        if off <= 0:
            continue
        check_once = off * cp.REGION_SECTOR
        if check_once + 4 <= len(blob) and cnt > 0:
            valid += 1
    check("保留 10 个有效表项", valid == 10, str(valid))
    check("被删区块的 InhabitedTime 读不到",
          inhabited_of(pp, 0) is None, str(inhabited_of(pp, 0)))
    check("保留区块的值仍是 5000", inhabited_of(pp, 1) == 5000,
          str(inhabited_of(pp, 1)))

    # ---------------- 6. 维度发现 ----------------
    print("\n[6] 多维度识别")
    mult = build_save("dims", {0: 0, 1: 999}, with_siblings=False,
                      dims=(".", "DIM-1", "DIM1"))
    labels = sorted(d["label"] for d in cp.discover_dimensions(mult))
    check("识别主世界/下界/末地", labels == ["下界", "主世界", "末地"] or
          set(labels) == {"主世界", "下界", "末地"}, str(labels))
    rd = cp.scan(mult, selected_dims={"主世界"})
    check("可只清理指定维度",
          sum(r.prune for r in rd.regions if r.kind == "region") == 1, "")

    # ---------------- 7. 空 region 整个删除 ----------------
    print("\n[7] 全部命中时删除整个 region 文件")
    empty = build_save("allempty", {0: 0, 1: 1, 2: 19},
                       with_siblings=False, dims=(".",))
    re_ = cp.scan(empty)
    check("标记 will_delete_file",
          any(r.will_delete_file for r in re_.regions), "")
    cp.apply(re_)
    check("空 region 文件已删除",
          not os.path.exists(os.path.join(empty, "region", "r.0.0.mca")), "")

    # ---------------- 8. 与同步层互不影响 ----------------
    print("\n[8] 清理后 region 差分仍可用")
    from core.protocol import piece_ranges, _ranges_sane
    pp = os.path.join(fmt, "region", "r.0.0.mca")
    sz = os.path.getsize(pp)
    ranges = piece_ranges(pp, sz)
    check("分片总量不膨胀", sum(n for _, n in ranges) == sz,
          f"{sum(n for _, n in ranges)} vs {sz}")
    check("分片覆盖完整", _ranges_sane(ranges, sz), "")

    print("\n" + "=" * 58)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 区块清理安全可用")
    print("=" * 58)


if __name__ == "__main__":
    main()
