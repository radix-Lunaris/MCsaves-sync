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
实例与存档识别
兼容：PCL2 / HMCL / 官方启动器 / Prism Launcher / MultiMC / Modrinth / 手动指定
策略：环境变量候选根 + 盘符级轻量嗅探 + 有限深度目录检测（不做全盘暴力扫描）
"""
from __future__ import annotations

import os
import sys
import time
from typing import Dict, Iterable, List, Optional, Sequence

from core.models import Instance, SaveInfo, SaveState
from core.nbt import level_info
from core.protocol import make_ignore, norm_rel
from concurrent.futures import ThreadPoolExecutor

IS_WIN = sys.platform.startswith("win")

LAUNCHER_KEYWORDS = (
    "pcl", "hmcl", "minecraft", "mc", "prism", "multimc", "modrinth",
    "xl", "bakaxl", "helio", "fabric", "forge", "mclauncher",
)


def _env(*names: str) -> List[str]:
    out = []
    for n in names:
        v = os.environ.get(n)
        if v:
            out.append(v)
    return out


def _first_existing(paths: Iterable[str]) -> List[str]:
    return [p for p in paths if p and os.path.isdir(p)]


def default_roots() -> List[str]:
    """已知启动器的默认实例根目录候选"""
    home = os.path.expanduser("~")
    appdata = _env("APPDATA") or [os.path.join(home, "AppData", "Roaming")]
    local = _env("LOCALAPPDATA") or [os.path.join(home, "AppData", "Local")]
    cands: List[str] = []

    for base in appdata + local:
        cands += [
            os.path.join(base, ".minecraft"),
            os.path.join(base, ".hmcl"),
            os.path.join(base, "hmcl"),
            os.path.join(base, "HMCL"),
            os.path.join(base, "PrismLauncher"),
            os.path.join(base, "PrismLauncher", "instances"),
            os.path.join(base, "MultiMC"),
            os.path.join(base, "MultiMC", "instances"),
            os.path.join(base, "ModrinthApp"),
            os.path.join(base, "ModrinthApp", "instances"),
            os.path.join(base, "com.modrinth.theseus"),
            os.path.join(base, "X Minecraft Launcher"),
            os.path.join(base, "PCL"),
        ]
    for base in [home] + _env("USERPROFILE"):
        cands += [
            os.path.join(base, ".minecraft"),
            os.path.join(base, "PCL"),
            os.path.join(base, "HMCL"),
            os.path.join(base, "Minecraft"),
            os.path.join(base, "Games", "Minecraft"),
            os.path.join(base, "Desktop", "PCL"),
            os.path.join(base, "Documents", "PCL"),
        ]
    # 绿色版常见落点
    for drive in ("C:\\", "D:\\", "E:\\", "F:\\"):
        if os.path.isdir(drive):
            cands += [
                os.path.join(drive, "PCL"),
                os.path.join(drive, "HMCL"),
                os.path.join(drive, "Minecraft"),
                os.path.join(drive, "Games", "Minecraft"),
                os.path.join(drive, "Program Files", "PCL"),
            ]
    seen, out = set(), []
    for c in cands:
        k = os.path.normcase(os.path.abspath(c))
        if k not in seen and os.path.isdir(c):
            seen.add(k)
            out.append(c)
    return out


def sniff_roots(max_depth: int = 2) -> List[str]:
    """盘符级轻量嗅探：只检查盘符根下 1~max_depth 层、名字像启动器的目录"""
    found: List[str] = []
    drives: List[str] = []
    if IS_WIN:
        drives = [f"{c}:\\" for c in "CDEFGHIJ" if os.path.isdir(f"{c}:\\")]
    else:
        drives = ["/"]
    for drv in drives:
        try:
            entries = [os.path.join(drv, e) for e in os.listdir(drv)]
        except Exception:
            continue
        for p in entries:
            if not os.path.isdir(p):
                continue
            low = os.path.basename(p).lower()
            if any(k in low for k in LAUNCHER_KEYWORDS):
                found.append(p)
                try:
                    sub = [os.path.join(p, e) for e in os.listdir(p)]
                except Exception:
                    sub = []
                for sp in sub:
                    if os.path.isdir(sp):
                        sl = os.path.basename(sp).lower()
                        if any(k in sl for k in LAUNCHER_KEYWORDS) or sl == ".minecraft":
                            found.append(sp)
    return found


# 遍历时直接跳过的目录：这些位置不可能有 MC 实例，
# 却能轻松吃掉几万个目录的遍历预算（尤其 Program Files / Windows / node_modules）。
SKIP_DIR_NAMES = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "perflogs", "recovery",
    "node_modules", ".git", ".svn", ".hg", ".idea", ".vscode",
    "__pycache__", "site-packages", "dist-info", "venv", ".venv",
    "cache", "cached", "temp", "tmp",
}

# .minecraft 目录内的标志性子目录（用于零 stat 判断）
GAME_DIR_MARKERS = {"versions", "mods", "resourcepacks", "config", "logs", "screenshots"}


def is_game_dir(path: str) -> bool:
    """判断是否为 .minecraft 游戏目录"""
    try:
        if not os.path.isdir(os.path.join(path, "saves")):
            return False
    except Exception:
        return False
    for marker in ("versions", "options.txt", "mods", "resourcepacks", "config", "logs", "screenshots"):
        if os.path.exists(os.path.join(path, marker)):
            return True
    return False


def is_instance_root(path: str) -> bool:
    """实例根：要么内含 .minecraft，要么本身就是游戏目录"""
    if os.path.isdir(os.path.join(path, ".minecraft")):
        return True
    return is_game_dir(path)


def detect_source(path: str) -> str:
    low = os.path.normcase(path)
    if "prismlauncher" in low:
        return "Prism Launcher"
    if "multimc" in low:
        return "MultiMC"
    if "modrinth" in low or "theseus" in low:
        return "Modrinth"
    if ".hmcl" in low or "hmcl" in low:
        return "HMCL"
    if "pcl" in low:
        return "PCL2"
    if os.path.normcase(os.path.expanduser("~")) in low and low.rstrip("\\/").endswith(".minecraft"):
        return "官方启动器"
    if "x minecraft launcher" in low or "bakaxl" in low:
        return "BakaXL / XMC"
    return "自定义"


def _read_instance_name(root: str, game_dir: str) -> str:
    cfg = os.path.join(root, "instance.cfg")
    if os.path.exists(cfg):
        try:
            with open(cfg, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("name="):
                        return line.split("=", 1)[1].strip()
        except Exception:
            pass
    base = os.path.basename(os.path.normpath(root))
    if base.lower() in (".minecraft", "minecraft", "instances", ""):
        parent = os.path.basename(os.path.dirname(os.path.normpath(root)))
        base = parent or "Minecraft"
    return base


def _guess_version(game_dir: str) -> str:
    vdir = os.path.join(game_dir, "versions")
    try:
        if os.path.isdir(vdir):
            names = [d for d in os.listdir(vdir) if os.path.isdir(os.path.join(vdir, d))]
            names.sort()
            return names[0] if names else ""
    except Exception:
        pass
    return ""


def _guess_modded(game_dir: str) -> bool:
    for marker in ("mods", "mods.txt"):
        p = os.path.join(game_dir, marker)
        if os.path.exists(p):
            if os.path.isdir(p):
                try:
                    return len(os.listdir(p)) > 0
                except Exception:
                    return False
            return True
    return False


def probe_instance(root: str) -> "Instance":
    """由实例根目录构造 Instance（供快速重扫使用）。

    与 InstanceScanner._make 走同一套命名/版本/来源推断，
    保证快速重扫和全盘扫描得到的实例信息完全一致，
    不会出现"重扫后名字变了"这种前后不一致。
    """
    gd = root if is_game_dir(root) else os.path.join(root, ".minecraft")
    return Instance(
        name=_read_instance_name(root, gd),
        root=root,
        game_dir=gd,
        source=detect_source(root),
        game_version=_guess_version(gd),
        modded=_guess_modded(gd),
    )


class InstanceScanner:
    def __init__(self, roots: Optional[Sequence[str]] = None, depth: int = 3):
        self.extra_roots: List[str] = list(roots or [])
        self.depth = max(1, min(6, int(depth)))
        self.max_dirs = 4000
        self._seen_dirs = 0

    # ------------------------------------------------------------ 快速重扫
    @staticmethod
    def fast_known_scan(known: Sequence[dict], extra_roots: Sequence[str] = ()) -> List[Instance]:
        """极快的「已知目录」重扫。

        为什么不用普通 scan()：
          known_roots 里可能混进盘符根（如 D:\\），对它做 depth=2 遍历等于把整个盘
          扫两层 —— 用户看到的就是"一直在重扫，半天不完"。
          而且旧实现每扫一次就往 known_roots 里追加新父目录（上限 40），
          目录越攒越多、每次越扫越慢，形成恶性循环。

        这里的做法：只对每个已知实例的**父目录做单层 listdir**。
        实例几乎总是平铺在同一个父目录下（如 PCL\\versions\\），
        单层检查足以发现新增/移除，耗时通常在几十毫秒。
        """
        parents: list = []
        seen = set()

        def add(p):
            try:
                k = os.path.normcase(os.path.abspath(p))
            except Exception:
                return
            # 盘符根不扫：遍历它没有意义，代价却极高
            if os.path.dirname(k) == k:
                return
            if k in seen or not os.path.isdir(p):
                return
            seen.add(k)
            parents.append(p)

        for d in known or []:
            try:
                inst = Instance.from_dict(d)
            except Exception:
                continue
            add(os.path.dirname(os.path.abspath(inst.root)))
        for r in extra_roots or []:
            add(r)

        # 工作量硬上限：
        # 哪怕用户把某个巨大的目录（甚至是网络盘）加成了自定义目录，
        # 单次重扫也绝不无限进行 —— 这正是"一直重扫停不下来"的根源之一。
        MAX_PARENTS = 12        # 最多扫 12 个父目录
        MAX_ENTRIES = 400       # 每个父目录最多看 400 个子项

        out: List[Instance] = []
        for p in parents[:MAX_PARENTS]:
            try:
                names = os.listdir(p)
            except OSError:
                continue
            for name in names[:MAX_ENTRIES]:
                if name.startswith("."):
                    continue
                full = os.path.join(p, name)
                gd = os.path.join(full, ".minecraft")
                try:
                    if not os.path.isdir(gd):
                        continue
                except OSError:
                    continue
                try:
                    out.append(probe_instance(full))
                except Exception:
                    continue
        return out

    # ------------------------------------------------------------ 扫描
    def candidate_roots(self, sniff: bool = True) -> List[str]:
        roots = default_roots() + list(self.extra_roots)
        if sniff:
            roots += sniff_roots()
        out, seen = [], set()
        for r in roots:
            try:
                k = os.path.normcase(os.path.abspath(r))
            except Exception:
                continue
            if k in seen or not os.path.isdir(r):
                continue
            seen.add(k)
            out.append(r)
        return out

    def _walk(self, root: str, budget: Optional[list] = None) -> List[str]:
        """有限深度遍历，返回所有实例根。

        性能要点（这是扫描提速的关键）：
          1) os.scandir 一次拿到子项及其类型，省掉逐个 os.path.isdir 的额外 stat 调用
          2) 跳过系统/工程类无关目录，大幅减少遍历量
          3) 用本次 scandir 已拿到的名字判断实例特征，命中判断零额外 stat

        budget: 单元素列表做本地计数，供并行调用时使用，
                避免多线程共享 self._seen_dirs 造成统计错乱。
        """
        hits: List[str] = []
        stack: List[tuple] = [(root, 0)]
        seen = 0
        while stack:
            cur, d = stack.pop()
            seen += 1
            if seen > self.max_dirs:
                break
            if budget is not None:
                budget[0] += 1

            try:
                with os.scandir(cur) as it:
                    dir_names = set()
                    file_names = set()
                    subs: List[str] = []
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                ln = e.name.lower()
                                dir_names.add(ln)
                                if ln in SKIP_DIR_NAMES:
                                    continue
                                subs.append(e.path)
                            else:
                                file_names.add(e.name.lower())
                        except OSError:
                            continue
            except (OSError, PermissionError):
                continue

            # 命中判断：只用本次 scandir 拿到的名字，不再做 stat
            if cur != root:
                if ".minecraft" in dir_names:
                    hits.append(cur)
                    continue          # 命中后不再深入，避免把存档目录误判为实例
                if "saves" in dir_names and (
                    (dir_names & GAME_DIR_MARKERS) or "options.txt" in file_names
                ):
                    hits.append(cur)
                    continue

            if d >= self.depth:
                continue
            for sp in subs:
                stack.append((sp, d + 1))
        return hits

    def scan(self, sniff: bool = True, progress=None) -> List[Instance]:
        self._seen_dirs = 0
        roots = self.candidate_roots(sniff=sniff)
        if not roots:
            return []

        def work(root: str) -> List[Instance]:
            budget = [0]
            got: List[Instance] = []
            try:
                hits = self._walk(root, budget)
            except Exception:
                hits = []
            for hit in hits:
                gd = os.path.join(hit, ".minecraft") if os.path.isdir(os.path.join(hit, ".minecraft")) else hit
                try:
                    if not is_game_dir(gd):
                        continue
                    got.append(Instance(
                        name=_read_instance_name(hit, gd),
                        root=hit,
                        game_dir=gd,
                        source=detect_source(hit),
                        game_version=_guess_version(gd),
                        modded=_guess_modded(gd),
                    ))
                except Exception:
                    continue
            return got

        found: Dict[str, Instance] = {}
        # 目录遍历是 I/O 密集（os.listdir 会释放 GIL），多线程能显著加速：
        # 多个根目录并行枚举，机械硬盘/网络盘上差异尤其明显。
        workers = max(1, min(8, len(roots), (os.cpu_count() or 4)))
        done = 0
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan") as ex:
                for got in ex.map(work, roots):
                    done += 1
                    if progress:
                        try:
                            progress(done / max(1, len(roots)), "")
                        except Exception:
                            pass
                    for inst in got:
                        found.setdefault(inst.key, inst)
        except Exception:
            # 线程池不可用时退回串行，保证功能不丢
            for root in roots:
                for inst in work(root):
                    found.setdefault(inst.key, inst)

        insts = sorted(found.values(), key=lambda x: (x.source != "PCL2", x.name.lower()))
        return insts

    # ------------------------------------------------------------ 存档
    @staticmethod
    def list_saves(inst: Instance, quick: bool = False) -> List[SaveInfo]:
        saves_dir = inst.saves_dir
        out: List[SaveInfo] = []
        if not os.path.isdir(saves_dir):
            return out
        try:
            entries = sorted(os.listdir(saves_dir))
        except Exception:
            return out
        for name in entries:
            p = os.path.join(saves_dir, name)
            if not os.path.isdir(p):
                continue
            if not os.path.exists(os.path.join(p, "level.dat")):
                continue
            info = level_info(os.path.join(p, "level.dat"))
            icon = os.path.join(p, "icon.png")
            si = SaveInfo(
                name=name,
                path=p,
                world_name=info.get("world_name", ""),
                game_version=info.get("game_version", "") or inst.game_version,
                gametype=info.get("gametype", ""),
                difficulty=info.get("difficulty", ""),
                hardcore=bool(info.get("hardcore")),
                last_played=float(info.get("last_played", 0) or 0),
                seed=info.get("seed"),
                icon=icon if os.path.exists(icon) else "",
                state=SaveState.load(p),
                instance=inst,
            )
            out.append(si)
        if not quick and out:
            # 统计每个存档大小要遍历全部文件，是最慢的一步；并行处理多个存档。
            def _stat(si: SaveInfo):
                try:
                    size, count = InstanceScanner.stat_save(si.path)
                    si.size, si.file_count = size, count
                except Exception:
                    si.size, si.file_count = 0, 0
            workers = max(1, min(8, len(out), (os.cpu_count() or 4)))
            try:
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="stat") as ex:
                    list(ex.map(_stat, out))
            except Exception:
                for si in out:
                    _stat(si)
        out.sort(key=lambda s: s.last_played or 0, reverse=True)
        return out

    @staticmethod
    def stat_save(path: str, ignore_rules: Optional[List[str]] = None, core_only: bool = False):
        """统计存档大小与文件数（遵循忽略规则）"""
        matcher = make_ignore(ignore_rules, core_only)
        total, count = 0, 0
        for root, dirs, files in os.walk(path):
            rel_root = norm_rel(os.path.relpath(root, path))
            if rel_root == ".":
                rel_root = ""
            keep = []
            for d in dirs:
                rel = (rel_root + "/" + d) if rel_root else d
                if matcher.match(rel):
                    continue
                keep.append(d)
            dirs[:] = keep
            for f in files:
                rel = (rel_root + "/" + f) if rel_root else f
                if matcher.match(rel):
                    continue
                try:
                    total += os.path.getsize(os.path.join(root, f))
                    count += 1
                except OSError:
                    pass
        return total, count


def human_time_ago(ts: float) -> str:
    if not ts:
        return "未知"
    delta = time.time() - ts
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} 天前"
    return time.strftime("%Y-%m-%d", time.localtime(ts))
