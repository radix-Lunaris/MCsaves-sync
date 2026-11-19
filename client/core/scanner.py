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
import threading
import time
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from core.models import Instance, SaveInfo, SaveState
from core.nbt import level_info
from core.protocol import make_ignore, norm_rel
from concurrent.futures import ThreadPoolExecutor

IS_WIN = sys.platform.startswith("win")

# 实例扫描深度上限。旧版硬编码 6，且每个候选根只给 4000 目录预算，
# 深度调到 6 时预算早在第 2~3 层就耗尽，等于「调了也没用」。
# 现在上限 12，预算改为全局共享并随深度放大（见 InstanceScanner）。
MAX_SCAN_DEPTH = 12
DEFAULT_SCAN_DEPTH = 6

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


def _drives() -> List[str]:
    if IS_WIN:
        return [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.isdir(f"{c}:\\")]
    # 非 Windows（含测试沙盒）：用家目录当"盘符根"，避免扫整个 /
    return [os.path.expanduser("~")]


def _listdirs(path: str) -> List[str]:
    """一层子目录（零多余 stat）。"""
    try:
        with os.scandir(path) as it:
            return [e.path for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def sniff_roots(max_depth: int = 2) -> List[str]:
    """盘符级轻量嗅探：名字像启动器的目录，最多向下 max_depth 层。

    旧版写死了两层、参数 max_depth 根本没生效。现在参数真正参与遍历，
    让「E:\\Games\\Modpacks\\PCL2」这种稍深的启动器位置也能进入候选。
    """
    max_depth = max(1, min(6, int(max_depth)))
    found: List[str] = []
    for drv in _drives():
        frontier = [drv]
        for _d in range(max_depth):
            nxt: List[str] = []
            for p in frontier:
                for sub in _listdirs(p):
                    low = os.path.basename(sub).lower()
                    if low in _SKIP_LOWER:
                        continue
                    if any(k in low for k in LAUNCHER_KEYWORDS) or low == ".minecraft":
                        found.append(sub)
                    nxt.append(sub)
            # 广谱遍历会指数膨胀，这里只保留"像启动器"的分支继续深入
            frontier = [p for p in nxt
                        if any(k in os.path.basename(p).lower() for k in LAUNCHER_KEYWORDS)]
            if not frontier:
                break
    return found


def broad_roots(limit: int = 80) -> List[str]:
    """盘符根下第一层目录，**不论名字**。

    这是「导入新整合包后全盘重扫也扫不到」的关键修复：
    旧版只把名字含 pcl/hmcl/minecraft/mc 之类关键词的目录当候选根，
    用户把整合包放在「E:\\整合包\\ATM9」或「D:\\Games\\MyPack」这种
    不含关键词的目录里，就永远不在候选列表，全盘重扫当然找不到。
    现在把所有第一层目录都纳入，交给 _walk 的深度 + 全局预算去控制代价。
    """
    out: List[str] = []
    for drv in _drives():
        for p in _listdirs(drv):
            low = os.path.basename(p).lower()
            if low in _SKIP_LOWER or low.startswith("$"):
                continue
            out.append(p)
            if len(out) >= limit:
                return out
    return out


# 遍历时直接跳过的目录：这些位置不可能有 MC 实例，
# 却能轻松吃掉几万个目录的遍历预算（尤其 Program Files / Windows / node_modules）。
SKIP_DIR_NAMES = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "perflogs", "recovery",
    "node_modules", ".git", ".svn", ".hg", ".idea", ".vscode",
    "__pycache__", "site-packages", "dist-info", "venv", ".venv",
    "cache", "cached", "temp", "tmp",
    # 根目录快照时额外排除（广谱第一层扫描）
    "bin", "boot", "dev", "etc", "lib", "lib64", "proc", "run", "sbin",
    "sys", "usr", "var", "opt", "srv", "mnt", "media", "snap",
}
_SKIP_LOWER = set(SKIP_DIR_NAMES)

# .minecraft 目录内的标志性子目录（用于零 stat 判断）
GAME_DIR_MARKERS = {"versions", "mods", "resourcepacks", "config", "logs", "screenshots"}


def looks_like_game_dir(dir_names: Set[str], file_names: Set[str],
                        base_name: str = "") -> bool:
    """仅凭一次 scandir 的名字判断「这本身就是游戏目录」。

    重要：刚导入、还没进过游戏的整合包实例通常**没有 saves 目录**
    （只有 mods/ + versions/ + config/）。旧版强制要求 saves 存在，
    这类新实例会被直接丢弃 —— 这正是「导入新整合包后怎么扫都扫不到」
    的另一个根因。

    判定分层，尽量不误判：
      · 目录名就是 .minecraft：有任一标志性子目录即可
      · 有 saves + 任一标志：典型已游玩过的游戏目录
      · 有 >=2 个标志性子目录：新整合包（mods + versions 等）
      · options.txt + 任一标志：官方启动器常见形态
    """
    low = (base_name or "").lower()
    if ".minecraft" in dir_names:
        return False          # 这是「实例根」，游戏目录在它的 .minecraft 里
    markers = dir_names & GAME_DIR_MARKERS
    has_opts = "options.txt" in file_names
    if low == ".minecraft":
        return bool(markers) or has_opts
    if "saves" in dir_names and (markers or has_opts or "level.dat" in file_names):
        return True
    if len(markers) >= 2:
        return True
    if has_opts and markers:
        return True
    return False


def looks_like_instance_dir(dir_names: Set[str], file_names: Set[str],
                            base_name: str = "") -> bool:
    """判断某个目录是「实例根」：内含 .minecraft，或它本身就是游戏目录。"""
    if ".minecraft" in dir_names:
        return True
    return looks_like_game_dir(dir_names, file_names, base_name)


def is_game_dir(path: str) -> bool:
    """判断是否为 .minecraft 游戏目录（基于真实磁盘，供 probe / 复核）。"""
    try:
        with os.scandir(path) as it:
            dirs: Set[str] = set()
            files: Set[str] = set()
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        dirs.add(e.name.lower())
                    else:
                        files.add(e.name.lower())
                except OSError:
                    continue
    except OSError:
        return False
    return looks_like_game_dir(dirs, files, os.path.basename(os.path.normpath(path)))


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
        # APPDATA\Roaming\.minecraft 的父目录叫 "Roaming"，拿它当实例名
        # 毫无意义。官方启动器统一给个可识别的名字。
        if parent.lower() in ("roaming", "local", "appdata", "", "."):
            return "官方启动器 (.minecraft)"
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
    def __init__(self, roots: Optional[Sequence[str]] = None,
                 depth: int = DEFAULT_SCAN_DEPTH):
        self.extra_roots: List[str] = list(roots or [])
        self.depth = max(1, min(MAX_SCAN_DEPTH, int(depth)))
        # 全局目录预算：所有候选根共享。旧版是每个根 4000 且 break 退出，
        # 深度调到 6 时第 2 层就把预算吃光，后面的目录根本没遍历。
        # 现在随深度线性放大，并允许调用方按机器性能覆盖。
        self.max_dirs = max(8000, 4000 * self.depth)
        self._budget_lock = threading.Lock()
        self._seen_dirs = 0

    # ------------------------------------------------------------ 快速重扫
    @staticmethod
    def fast_known_scan(known: Sequence[dict], extra_roots: Sequence[str] = (),
                        max_parents: int = 24, max_entries: int = 600) -> List[Instance]:
        """极快的「已知目录」重扫。

        为什么不用普通 scan()：known_roots 里可能混进盘符根（如 D:\\），
        对它做深遍历等于扫大半个盘 —— 用户看到的就是"一直在重扫"。

        旧实现有两个致命缺陷，导致「手动添加目录后重扫扫不到」：
          1) 只检查子目录里有没有 `.minecraft` 文件夹，用户添加的若是
             **实例目录本身**（或游戏目录本身），单层 listdir 永远查不到；
          2) 旧的隐藏目录过滤把点号开头的目录一律跳过，
             连 `.minecraft` 自己也被排除了。
        现在：先直接 probe 每个自定义根自身，再单层扫父目录；判定统一走
        is_instance_root()，兼容「内含 .minecraft」与「自身即游戏目录」两种形态，
        也兼容尚无 saves 的新整合包。
        """
        parents: List[str] = []
        seen = set()

        def add(p):
            try:
                k = os.path.normcase(os.path.abspath(p))
            except Exception:
                return
            if os.path.dirname(k) == k:      # 盘符根不扫
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

        out: Dict[str, Instance] = {}

        def collect(path: str):
            try:
                if not is_instance_root(path):
                    return
                inst = probe_instance(path)
                out[os.path.normcase(os.path.abspath(inst.root))] = inst
            except Exception:
                pass

        # ① 自定义根自身就是要找的实例 —— 旧版完全漏掉这一支
        for r in extra_roots or []:
            collect(r)

        # ② 已知实例的父目录 + 自定义根下的一层子目录
        for p in parents[:max_parents]:
            # 若这个父目录自己就是实例，别再往里扫一层：
            # 否则会把它的 `.minecraft` 当成第二个独立实例，
            # 列表里出现两个同名条目（旧版没有 ① 分支时不会暴露）。
            try:
                if is_instance_root(p):
                    continue
            except Exception:
                pass
            try:
                names = os.listdir(p)
            except OSError:
                continue
            for name in names[:max_entries]:
                low = name.lower()
                if low in _SKIP_LOWER and low != ".minecraft":
                    continue
                collect(os.path.join(p, name))

        return list(out.values())

    # ------------------------------------------------------------ 扫描
    def candidate_roots(self, sniff: bool = True, broad: bool = True) -> List[str]:
        """候选根 = 固定启动器路径 + 手动指定 + 关键词嗅探 + 盘符第一层广谱目录。

        broad=True 是「新整合包扫不到」的主修：不再要求目录名含关键词。
        """
        roots = default_roots() + list(self.extra_roots)
        if sniff:
            roots += sniff_roots(max_depth=min(4, max(2, self.depth - 2)))
        broad_list = broad_roots() if broad else []
        self._broad_set = {os.path.normcase(os.path.abspath(p)) for p in broad_list}
        roots += broad_list
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

    def _walk(self, root: str, budget: Optional[list] = None,
              depth_limit: Optional[int] = None,
              limit: Optional[int] = None) -> List[str]:
        """有限深度遍历，返回所有实例根。

        三处关键修复：
          1) **root 自身也要判定**。旧版在命中判断前加了一句
             "当前目录不等于根才判定"，直接跳过了 root，
             用户手动添加、或 default_roots() 里的「实例目录本身」
             （如 APPDATA\\Roaming\\.minecraft）永远扫不出来。
          2) **预算全局共享**，不是每根 4000。旧版预算在浅层就 break，
             把 scan_depth 调到 6 也走不到第 4 层。
          3) 判定改用 looks_like_instance_dir()，兼容尚无 saves 的新整合包。
        """
        hits: List[str] = []
        maxd = self.depth if depth_limit is None else min(self.depth, depth_limit)
        cap = self.max_dirs if limit is None else limit
        stack: List[Tuple[str, int]] = [(root, 0)]
        while stack:
            cur, d = stack.pop()
            if budget is not None:
                with self._budget_lock:
                    budget[0] += 1
                    over = budget[0] > cap
                if over:
                    break

            try:
                with os.scandir(cur) as it:
                    dir_names: Set[str] = set()
                    file_names: Set[str] = set()
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

            # 命中判断：只用本次 scandir 拿到的名字，不再做额外 stat。
            # 命中后不再深入，避免把存档目录误判为实例。
            if looks_like_instance_dir(dir_names, file_names,
                                       os.path.basename(os.path.normpath(cur))):
                hits.append(cur)
                continue

            if d >= maxd:
                continue
            for sp in subs:
                stack.append((sp, d + 1))
        return hits

    def scan(self, sniff: bool = True, broad: bool = True,
             progress=None) -> List[Instance]:
        self._seen_dirs = 0
        roots = self.candidate_roots(sniff=sniff, broad=broad)
        if not roots:
            return []
        # 两批各用独立预算：明确候选不会把广谱根的额度吃光，
        # 否则「E:\\整合包」这类新位置又会因为预算耗尽而扫不到。
        prio_budget = [0]
        broad_budget = [0]
        broad_cap = max(4000, self.max_dirs // 2)

        # 分两批：明确候选（启动器固定路径 / 手动指定 / 关键词命中）先扫，
        # 广谱第一层目录后扫。否则 C:\\Users 这种巨型目录会先吃光全局预算，
        # 放在 E:\\整合包 的新实例反而没额度遍历 —— 又变成「扫不到」。
        broad_set = getattr(self, "_broad_set", set())
        priority = [r for r in roots
                    if os.path.normcase(os.path.abspath(r)) not in broad_set]
        secondary = [r for r in roots
                     if os.path.normcase(os.path.abspath(r)) in broad_set]

        found: Dict[str, Instance] = {}
        # 目录遍历是 I/O 密集（os.scandir 会释放 GIL），多线程能显著加速。
        workers = max(1, min(8, max(len(priority), len(secondary)), (os.cpu_count() or 4)))
        total = max(1, len(roots))
        done = 0

        def absorb(got):
            for inst in got:
                found.setdefault(inst.key, inst)

        def work_one(root: str, depth_limit=None, broad: bool = False) -> List[Instance]:
            """只在子线程做磁盘遍历；UI 进度回调一律留在主线程。"""
            budget = broad_budget if broad else prio_budget
            cap = broad_cap if broad else None
            try:
                hits = self._walk(root, budget, depth_limit=depth_limit, limit=cap)
            except Exception:
                hits = []
            got: List[Instance] = []
            for hit in hits:
                gd = os.path.join(hit, ".minecraft")
                if not os.path.isdir(gd):
                    gd = hit
                try:
                    if not is_game_dir(gd):
                        continue
                    got.append(Instance(
                        name=_read_instance_name(hit, gd), root=hit, game_dir=gd,
                        source=detect_source(hit), game_version=_guess_version(gd),
                        modded=_guess_modded(gd),
                    ))
                except Exception:
                    continue
            return got

        def tick(n: int = 1):
            nonlocal done
            done += n
            if progress:
                try:
                    progress(done / total, "")
                except Exception:
                    pass

        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan") as ex:
                for got in ex.map(lambda r: work_one(r), priority):
                    absorb(got)
                    tick()
                for got in ex.map(lambda r: work_one(r, min(self.depth, 4), broad=True),
                                  secondary):
                    absorb(got)
                    tick()
        except Exception:
            # 线程池不可用时退回串行，保证功能不丢
            for r in priority:
                absorb(work_one(r))
                tick()
            for r in secondary:
                absorb(work_one(r, min(self.depth, 4), broad=True))
                tick()

        self._seen_dirs = prio_budget[0] + broad_budget[0]
        insts = sorted(found.values(), key=lambda x: (x.source != "PCL2", x.name.lower()))
        return insts

    @staticmethod
    def scan_one_root(root: str, depth: int = DEFAULT_SCAN_DEPTH) -> List[Instance]:
        """只扫用户刚手动添加的**那一个**目录，深度按设置，立即返回结果。

        用于「➕ 添加目录后马上能看到实例」，不必等全盘重扫、更不必重启。
        不走 candidate_roots()，所以不会被盘符第一层广谱目录拖慢。
        """
        if not os.path.isdir(root):
            return []
        sc = InstanceScanner(depth=depth)
        try:
            hits = sc._walk(root, [0])
        except Exception:
            hits = []
        out: Dict[str, Instance] = {}
        for hit in hits:
            gd = os.path.join(hit, ".minecraft")
            if not os.path.isdir(gd):
                gd = hit
            try:
                if not is_game_dir(gd):
                    continue
                inst = probe_instance(hit)
                out[os.path.normcase(os.path.abspath(inst.root))] = inst
            except Exception:
                continue
        return list(out.values())

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
