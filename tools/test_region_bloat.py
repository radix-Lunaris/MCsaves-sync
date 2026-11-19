# -*- coding: utf-8 -*-
"""
region 分片「数据量膨胀」回归测试

真实事故：
    400MB 的存档上传时变成了 16GB（约 40 倍）。

根因：
    旧算法按 MC 区块「索引」分组（0-7、8-15 …），再取组内 min(起始)/max(结束)。
    但 MC 是按生成顺序把区块写进 .mca 的，空间上完全散乱 ——
    同一索引组的 8 个区块往往散布在整个文件里，min/max 一跨就是几十 MB。
    128 个分组彼此严重重叠，同一段字节被重复计入几十次。
    实测 64MB 的 .mca 被切成总计 6195MB（96.8 倍）。

本测试用合成的 .mca（连续 / 散乱 / 带空洞 / 超大区块 等多种形态）断言：
    1. 分片总量 == 文件大小（绝不允许膨胀）
    2. 有序、不重叠、恰好覆盖 [0, size)
    3. 下载的重组结果与原文逐字节一致
    4. 小改动只让少量分片失效（差分仍然有效）
    5. 非 .mca 文件走定长切分，行为不受影响

用法： python3 tools/test_region_bloat.py
"""
from __future__ import annotations

import os
import random
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

from core.protocol import (  # noqa: E402
    REGION_HEADER, REGION_SECTOR, piece_ranges, _ranges_sane,
)

TMP = "/tmp/mcsync_region_bloat"
OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def build_mca(path: str, size_mb: int = 64, seed: int = 42,
              contiguous: bool = True, holes: bool = False,
              nchunks: int = 1024) -> int:
    """合成一个 .mca。

    contiguous=True  -> 区块按索引顺序写入（刚生成的新存档）
    contiguous=False -> 区块写入顺序打乱（多次游玩/编辑后的真实形态）
    holes=True       -> 索引表里留空洞（某些区块从未生成）
    """
    rnd = random.Random(seed)
    size = size_mb * 1024 * 1024
    header = bytearray(REGION_HEADER)
    body = bytearray(size - REGION_HEADER)
    idxs = [i for i in range(nchunks)]
    if holes:
        idxs = [i for i in idxs if i % 7 != 0]      # 约 1/7 的区块不存在
    if not contiguous:
        rnd.shuffle(idxs)
    sector = 2                                       # 前 2 个扇区给 header
    for i in idxs:
        cnt = rnd.randint(1, 30)
        off = sector
        header[i * 4:(i * 4) + 4] = bytes(
            [(off >> 16) & 0xFF, (off >> 8) & 0xFF, off & 0xFF, cnt])
        sector += cnt
        if sector * REGION_SECTOR >= size:
            break
    with open(path, "wb") as f:
        f.write(bytes(header))
        f.write(bytes(body))
    return os.path.getsize(path)


def rebuild(ranges, blob_of) -> bytes:
    """按分片重组，模拟下载端的还原过程。"""
    out = bytearray()
    for off, n in ranges:
        chunk = blob_of(off, n)
        assert len(chunk) == n, f"分片长度不符 {len(chunk)} != {n}"
        out.extend(chunk)
    return bytes(out)


def main():
    print("\n=== region 分片膨胀回归测试 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    cases = [
        ("连续写入（新存档）", dict(contiguous=True)),
        ("散乱写入（多次编辑后）", dict(contiguous=False)),
        ("散乱 + 索引空洞", dict(contiguous=False, holes=True)),
        ("散乱 128MB 大文件", dict(contiguous=False, size_mb=128)),
        ("只有少量区块", dict(contiguous=False, nchunks=16, size_mb=8)),
    ]

    print("[1] 分片总量必须等于文件大小（核心断言）")
    for name, kw in cases:
        p = os.path.join(TMP, "t.mca")
        sz = build_mca(p, **kw)
        rs = piece_ranges(p, sz)
        total = sum(n for _, n in rs)
        ratio = total / sz if sz else 0
        check(f"{name}：膨胀 {ratio:.3f}x", abs(ratio - 1.0) < 1e-9,
              f"{total/1048576:.1f}MB / {sz/1048576:.1f}MB")

    print("\n[2] 有序、不重叠、恰好覆盖（保证可字节级还原）")
    for name, kw in cases:
        p = os.path.join(TMP, "t2.mca")
        sz = build_mca(p, **kw)
        rs = piece_ranges(p, sz)
        check(f"{name}：_ranges_sane", _ranges_sane(rs, sz), "")

    print("\n[3] 重组结果与原文逐字节一致")
    for name, kw in cases:
        p = os.path.join(TMP, "t3.mca")
        sz = build_mca(p, **kw)
        rs = piece_ranges(p, sz)
        with open(p, "rb") as f:
            data = f.read()
        got = rebuild(rs, lambda o, n: data[o:o + n])
        check(f"{name}：重组一致", got == data,
              f"{len(got)} vs {len(data)}")

    print("\n[4] 差分仍然有效：改一个区块只影响少量分片")
    p = os.path.join(TMP, "t4.mca")
    sz = build_mca(p, contiguous=False, size_mb=64)
    r1 = piece_ranges(p, sz)
    with open(p, "rb") as f:
        d1 = f.read()
    p1 = [d1[o:o + n] for o, n in r1]
    # 改动索引 500 号区块中间 4 字节
    off500 = ((d1[500 * 4] << 16) | (d1[500 * 4 + 1] << 8) | d1[500 * 4 + 2]) * REGION_SECTOR
    d2 = bytearray(d1)
    d2[off500 + 100:off500 + 104] = b"\xde\xad\xbe\xef"
    with open(p, "wb") as f:
        f.write(bytes(d2))
    r2 = piece_ranges(p, sz)
    p2 = [bytes(d2[o:o + n]) for o, n in r2]
    if r1 == r2:
        changed = sum(n for (o, n), a, b in zip(r1, p1, p2) if a != b)
    else:
        changed = sum(n for _, n in r2)      # 布局变了就按最坏情况计
    pct = changed / sz * 100
    check("改 1 个区块的重传量 < 5%", pct < 5.0, f"{changed/1048576:.2f}MB ({pct:.2f}%)")
    print(f"      重传 {changed/1048576:.2f}MB / 共 {sz/1048576:.1f}MB（{pct:.2f}%）")

    print("\n[5] 非 .mca 文件走定长切分，不受影响")
    for sz_mb in (0.5, 5, 40):
        p = os.path.join(TMP, f"plain_{sz_mb}.dat")
        with open(p, "wb") as f:
            f.write(os.urandom(int(sz_mb * 1024 * 1024)))
        sz = os.path.getsize(p)
        rs = piece_ranges(p, sz)
        total = sum(n for _, n in rs)
        check(f"普通文件 {sz_mb}MB：总量一致", total == sz, f"{total} vs {sz}")
        check(f"普通文件 {sz_mb}MB：覆盖完整", _ranges_sane(rs, sz), "")

    print("\n[6] 边界情况")
    # 空文件
    check("空文件返回空列表", piece_ranges("/tmp/x.mca", 0) == [], "")
    # 小于 header
    p = os.path.join(TMP, "tiny.mca")
    with open(p, "wb") as f:
        f.write(b"\x00" * 100)
    rs = piece_ranges(p, 100)
    check("小于 header 的文件覆盖完整", _ranges_sane(rs, 100), "")
    # 全零 header（无有效区块）应退回定长
    p2 = os.path.join(TMP, "zero.mca")
    with open(p2, "wb") as f:
        f.write(b"\x00" * (2 * 1024 * 1024))
    rs2 = piece_ranges(p2, 2 * 1024 * 1024)
    check("全零 header 退回定长切分", _ranges_sane(rs2, 2 * 1024 * 1024), "")

    print("\n[7] 事故复现：旧算法必须被判定为不合格")
    # 用旧逻辑（按索引取 min/max）生成分片，验证 _ranges_sane 会拒绝它
    p = os.path.join(TMP, "old.mca")
    sz = build_mca(p, contiguous=False, size_mb=64)
    with open(p, "rb") as f:
        header = f.read(REGION_HEADER)
    groups: dict = {}
    for i in range(1024):
        b = header[i * 4:(i * 4) + 4]
        off = (b[0] << 16) | (b[1] << 8) | b[2]
        cnt = b[3]
        if off <= 0 or cnt <= 0:
            continue
        start = off * REGION_SECTOR
        end = min(start + cnt * REGION_SECTOR, sz)
        if start < REGION_HEADER or start >= sz:
            continue
        g = i // 8
        cur = groups.get(g)
        if cur is None:
            groups[g] = [start, end]
        else:
            cur[0] = min(cur[0], start)
            cur[1] = max(cur[1], end)
    old = [(0, REGION_HEADER)]
    for _g, v in sorted(groups.items()):
        s, e = v[0], min(v[1], sz)
        if e > s:
            old.append((s, e - s))
    old_total = sum(n for _, n in old)
    print(f"      旧算法总长 {old_total/1048576:.1f}MB vs 文件 {sz/1048576:.1f}MB"
          f"（{old_total/sz:.1f}x）")
    check("旧算法确实会膨胀（确认事故真实）", old_total > sz * 10, "")
    check("旧算法被 _ranges_sane 拒绝", not _ranges_sane(old, sz), "")

    print("\n[8] 服务端必须拒掉膨胀清单（防止磁盘被撑爆）")
    appsrc_path = os.path.join(ROOT, "server", "app.py")
    try:
        appsrc = open(appsrc_path, encoding="utf-8").read()
    except OSError:
        appsrc = ""
    check("服务端有清单膨胀拦截", "清单异常" in appsrc, "")
    check("拦截用 HTTP 400 而非静默接受", "400" in appsrc, "")
    # 拦截必须发生在 commit 函数内、且早于任何 blob 写入/引用
    _ci = appsrc.find("def commit(")
    _fn = appsrc[_ci:_ci + 3000] if _ci >= 0 else appsrc
    check("拦截位于 commit 函数内且早于存盘",
          0 <= _fn.find("清单异常") < _fn.find("store.put_blob")
          or (0 <= _fn.find("清单异常") < _fn.find("create_version")),
          f"guard={_fn.find('清单异常')} put={_fn.find('store.put_blob')}")

    print("\n[9] 客户端同步层有第二道兜底")
    syncsrc_path = os.path.join(ROOT, "client", "core", "sync.py")
    syncsrc = open(syncsrc_path, encoding="utf-8").read()
    check("同步层校验分片总长", "sum(n for _, n in ranges) > size" in syncsrc, "")
    check("不合规时退回定长切分", "piece_size_for" in syncsrc, "")

    print("\n" + "=" * 58)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 分片不再膨胀")
    print("=" * 58)


if __name__ == "__main__":
    main()
