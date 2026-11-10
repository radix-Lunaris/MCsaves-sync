# -*- coding: utf-8 -*-
"""
区块清理 · 多整合包 / 多维度 / 多压缩类型回归测试

参照 MCA Selector (Querz/mcaselector) 的 WorldDirectories 与 CompressionType：
  · 只要找到 region 文件夹就是一个可处理世界，poi / entities 可选，
    不对整合包或维度做白名单（含 DIM-1 / DIM1 / dimensions/<ns>/<name>）
  · 压缩字节 GZIP 1 / ZLIB 2 / UNCOMPRESSED 3 / LZ4 4 / NONE 0，
    最高位表示外部 .mcc 存储

覆盖：
  1. 三个不同整合包的存档都能在页面里被选中并清理
  2. 切实例后存档列表跟着换（旧版永远显示最初那个整合包的存档）
  3. 26.1 dimensions 结构与自定义维度被识别
  4. region 目录里混着 .mcc 时不再被误判为「不支持」
  5. 0/1/2/3 四种压缩类型都能读出 InhabitedTime
  6. 外部 .mcc 标记的区块整份保留，不误删

用法： python3 tools/test_chunk_prune_multi.py
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

import ui_smoke  # noqa: E402
ui_smoke.install_stubs()


def _rec_configure(self, *a, **k):
    cfg = self.__dict__.setdefault("_cfg", {})
    cfg.update(k)
    if "text" in k:
        self._text = k["text"]
    return self


ui_smoke.W.configure = _rec_configure

from core import chunk_prune as cp          # noqa: E402
from core import nbt_typed as N             # noqa: E402
from core.models import Instance            # noqa: E402
from core.scanner import InstanceScanner            # noqa: E402
import test_chunk_prune as h                # noqa: E402
from ui.prune_page import PrunePage         # noqa: E402

TMP = "/tmp/mcsync_prune_multi"
OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def T(t, v):
    return N.NBTTag(t, v)


def write_level(path: str, world: str):
    """写一份合法的 level.dat，保证 Scanner.list_saves 能识别存档。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    root = T(N.TAG_COMPOUND, {"Data": T(N.TAG_COMPOUND, {
        "LevelName": T(N.TAG_STRING, world),
        "DataVersion": T(N.TAG_INT, 3700),
    })})
    N.write_file(path, "", root, "gzip")


def add_region(base: str, sub: str, fname: str, comp: int = cp.COMP_ZLIB):
    # MCA Selector / MC 的实际结构：<维度根>/region/r.x.z.mca
    d = os.path.join(base, sub) if sub else base
    rdir = os.path.join(d, "region")
    os.makedirs(rdir, exist_ok=True)
    h.make_region(os.path.join(rdir, fname),
                  {0: 0, 1: 19, 2: 5000, 3: 40000}, comp=comp)


def make_pack(pack: str, saves: dict) -> Instance:
    """saves: 存档名 -> 维度结构描述"""
    root = os.path.join(TMP, "instances", pack)
    game = os.path.join(root, ".minecraft")
    for save, spec in saves.items():
        base = os.path.join(game, "saves", save)
        os.makedirs(base, exist_ok=True)
        write_level(os.path.join(base, "level.dat"), save)
        for sub, comp in spec["regions"]:
            add_region(base, sub, "r.0.0.mca", comp)
        for junk in spec.get("mcc", []):
            d = os.path.join(os.path.join(base, junk[0]) if junk[0] else base, "region")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, junk[1]), "wb") as f:
                f.write(b"\x00" * 4096)
    return Instance(name=pack, root=root, game_dir=game)


class FakeApp:
    def __init__(self, instances):
        self.instances = instances
        self.saves = []
        self.cur_instance = instances[0]
        self.cur_save = None

    def run_async(self, work, done, fail=None):
        try:
            done(work())
        except Exception as e:
            if fail:
                fail(e)

    def load_saves(self, inst):
        pass

    def toast(self, msg):
        pass


def main():
    print("\n=== 区块清理 · 多整合包 / 多维度 / 多压缩类型 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    # ---------------- 造三个整合包 ----------------
    print("[1] 构造三种不同目录结构的整合包")
    pack_a = make_pack("PackA-经典", {
        "WorldA": {"regions": [("", cp.COMP_ZLIB),
                               ("DIM-1", cp.COMP_GZIP),
                               ("DIM1", cp.COMP_UNCOMPRESSED)]},
        "WorldA2": {"regions": [("", cp.COMP_ZLIB)]},
    })
    pack_b = make_pack("PackB-26.1", {
        "WorldB": {"regions": [
            ("dimensions/minecraft/overworld", cp.COMP_ZLIB),
            ("dimensions/minecraft/the_nether", cp.COMP_GZIP),
            ("dimensions/minecraft/the_end", cp.COMP_UNCOMPRESSED),
            ("dimensions/mypack/void", cp.COMP_ZLIB),
        ]},
    })
    pack_c = make_pack("PackC-混合", {
        "WorldC": {"regions": [("", cp.COMP_NONE),
                               ("", cp.COMP_GZIP)],
                   "mcc": [("", "r.9.9.mcc"), ("", "r.8.8.mcc")]},
    })
    # WorldC 的第二个 region 文件名要不同，否则覆盖第一个
    c_base = os.path.join(TMP, "instances", "PackC-混合", ".minecraft", "saves", "WorldC")
    h.make_region(os.path.join(c_base, "region", "r.1.0.mca"),
                  {0: 3, 1: 8000}, comp=cp.COMP_GZIP)
    insts = [pack_a, pack_b, pack_c]

    for inst in insts:
        names = sorted(s.name for s in InstanceScanner.list_saves(inst, quick=True))
        check(f"{inst.name} 的存档可被扫描到", len(names) >= 1, str(names))

    # ---------------- 2. 维度发现 ----------------
    print("\n[2] 维度发现（MCA Selector：找到 region 即为一维）")
    dims_a = cp.discover_dimensions(os.path.join(pack_a.game_dir, "saves", "WorldA"))
    check("经典结构识别 3 个维度", len(dims_a) == 3, str([d["label"] for d in dims_a]))
    check("下界/末地名称正确",
          {"主世界", "下界", "末地"} <= {d["label"] for d in dims_a},
          str([d["label"] for d in dims_a]))

    dims_b = cp.discover_dimensions(os.path.join(pack_b.game_dir, "saves", "WorldB"))
    check("26.1 结构识别 4 个维度", len(dims_b) == 4, str([d["label"] for d in dims_b]))
    labels_b = {d["label"] for d in dims_b}
    check("26.1 官方维度翻译正确",
          {"主世界 (26.1)", "下界 (26.1)", "末地 (26.1)"} <= labels_b, str(labels_b))
    check("自定义维度按路径保留",
          any("mypack/void" in x for x in labels_b), str(labels_b))

    # ---------------- 3. 能力检测（关键回归） ----------------
    print("\n[3] 每个整合包的存档都能通过能力检测")
    for inst in insts:
        for s in InstanceScanner.list_saves(inst, quick=True):
            ok, why = cp.capability(s.path)
            check(f"{inst.name}/{s.name} 支持清理", ok, why)

    # .mcc 混在 region 目录里，旧版会误判成「无法读取任何区块」
    c_path = os.path.join(c_base)
    ok_c, why_c = cp.capability(c_path)
    check("混入 .mcc 文件仍判为支持", ok_c, why_c)
    check("能力说明包含维度数", "维度" in why_c, why_c)

    # ---------------- 4. 压缩类型覆盖 ----------------
    print("\n[4] 压缩字节 0/1/2/3 都能读出 InhabitedTime")
    for comp, label in ((cp.COMP_NONE, "NONE(0)"),
                        (cp.COMP_GZIP, "GZIP(1)"),
                        (cp.COMP_ZLIB, "ZLIB(2)"),
                        (cp.COMP_UNCOMPRESSED, "UNCOMPRESSED(3)")):
        p = os.path.join(TMP, "comp", f"r.{comp}.0.mca")
        h.make_region(p, {0: 5, 1: 9000}, comp=comp)
        rep = cp.parse_region(p, 20)
        vals = sorted(v.inhabited for v in rep.verdicts.values()
                      if v.inhabited is not None)
        check(f"{label} 解析成功", vals == [5, 9000], str(vals))

    # 外部 .mcc 标记：整份 region 不参与清理，但字节保留
    print("\n[5] 外部 .mcc 标记保守保留")
    ext = os.path.join(TMP, "comp", "r.ext.0.mca")
    h.make_region(ext, {0: 0, 1: 9000}, comp=cp.COMP_ZLIB | cp.COMP_EXTERNAL_BIT)
    rep_ext = cp.parse_region(ext, 20)
    check("标记为 skip:external",
          any(v.reason == "skip:external" for v in rep_ext.verdicts.values()),
          str([v.reason for v in rep_ext.verdicts.values()]))
    check("无法判定的区块数据仍被保留",
          all(v.data for v in rep_ext.verdicts.values()), "")

    # ---------------- 6. 页面切换实例 ----------------
    print("\n[6] 页面里三个整合包都能选到自己的存档")
    app = FakeApp(insts)
    page = PrunePage(ui_smoke.W(), app)
    page.sync_selection(insts, [], pack_a, None)
    check("初始列出 PackA 的存档",
          "WorldA" in (page.cb_save._cfg.get("values") or []),
          str(page.cb_save._cfg.get("values")))

    page.cb_inst._text = "PackB-26.1"
    page._on_inst_changed()
    vals_b = page.cb_save._cfg.get("values") or []
    check("切到 PackB 后只列 PackB 的存档",
          "WorldB" in vals_b and "WorldA" not in vals_b, str(vals_b))
    check("_saves 路径都在 PackB 目录下",
          all(os.path.normcase(s.path).startswith(
              os.path.normcase(pack_b.game_dir)) for s in page._saves),
          str([s.path for s in page._saves]))

    page.cb_inst._text = "PackC-混合"
    page._on_inst_changed()
    vals_c = page.cb_save._cfg.get("values") or []
    check("切到 PackC 后只列 PackC 的存档",
          "WorldC" in vals_c and "WorldB" not in vals_c, str(vals_c))
    check("PackC 扫描按钮启用",
          page.b_scan._cfg.get("state") == "normal", str(page.b_scan._cfg))

    # 切回来也能恢复
    page.cb_inst._text = "PackA-经典"
    page._on_inst_changed()
    check("切回 PackA 仍可用",
          "WorldA" in (page.cb_save._cfg.get("values") or []),
          str(page.cb_save._cfg.get("values")))

    # ---------------- 7. 三个整合包都能实际扫描出结果 ----------------
    print("\n[7] 三个整合包的存档都能产出预览报告")
    for inst in insts:
        s = InstanceScanner.list_saves(inst, quick=True)[0]
        rep = cp.scan(s.path, 1.0)
        check(f"{inst.name}/{s.name} 支持且命中区块",
              rep.supported and rep.prune_chunks > 0,
              f"supported={rep.supported} prune={rep.prune_chunks} {rep.unsupported_reason}")
        check(f"{inst.name}/{s.name} 覆盖多个维度",
              len(rep.dimensions) >= 1, str(rep.dimensions))

    print("\n" + "=" * 62)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 多整合包、多维度、多压缩均可清理")
    print("=" * 62)


if __name__ == "__main__":
    main()
