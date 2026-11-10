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
共享玩家数据修正 —— 不用装模组，直接改存档文件。

问题（MC-697）：
  Java 版 26.1 之前，单人世界把「世界所有者」的完整玩家数据额外存一份在
  level.dat → Data → Player。单人模式打开世界时**无条件优先读这份**，
  完全不看当前玩家的 UUID。所以房主把存档发给别人，接手者打开会变成
  房主的背包 / 位置 / 经验 / 进度，甚至覆盖掉自己原本的角色。

  26.1 起 Player 复合标签换成 singleplayer_uuid 指针，真正的数据统一放
  players/data/<UUID>.dat，按 UUID 读 —— 但那个指针仍可能指向房主，
  接手者照样读到房主的数据。

做法（与 shared-playerdata 模组同一思路，但由同步工具在客户端侧完成）：
  1. 把 Data.Player 那份数据**归档**成 playerdata/<原主人UUID>.dat
     —— 原主人再打开时按自己 UUID 就能读回来，数据不丢。
  2. 从 level.dat 摘掉 Data.Player（26.1+ 则摘掉 singleplayer_uuid）。
  3. 游戏退回按当前玩家 UUID 读各自的数据文件；没有就以新手身份进世界。
     于是 A、B 两个人各拿各的，互不覆盖。

  这个方案最大的好处：**不需要知道当前玩家是谁**。模组要在游戏进程里
  拿登录 UUID，MCSync 是独立程序拿不到；而「归档 + 摘除」对房主和
  接手者都成立，因此零配置即可工作。

安全原则（宁可不动，也不能丢）：
  · 改前把原始 level.dat 备份到 <存档>/.mcsync/（该目录默认不同步）
  · 临时文件 + 原子替换写回，不会写坏 level.dat
  · 读不出原主人 UUID 时，把整份 Player 数据另存备份后才摘除，绝不丢数据
  · 目标 .dat 已存在且是真实数据时保留它；只有「空壳」才备份后覆盖
  · 游戏运行中拒绝修改
  · 完全幂等：第二次运行检测不到内嵌数据，直接跳过
"""
from __future__ import annotations

import os
import shutil
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from core import nbt_typed as N

# level.dat 可能与玩家数据同名的两个键（不同 MC 版本二选一）
KEY_PLAYER = "Player"
KEY_SP_UUID = "singleplayer_uuid"

# 判断一个 playerdata/<uuid>.dat 是不是「空壳」（只有几个键、没有实质状态）
CORE_PLAYER_KEYS = {
    "Inventory", "EnderItems", "Pos", "Rotation", "Motion", "playerGameType",
    "XpLevel", "XpTotal", "Health", "foodLevel", "abilities", "Attributes",
    "Dimension", "SpawnX", "SpawnY", "SpawnZ", "respawn", "recipeBook",
}

BACKUP_SUBDIR = "playerdata_backup"     # 放在 .mcsync/ 下
SPD_SUFFIX = ".spd_backup"               # 被覆盖的空壳文件备份后缀


# ------------------------------------------------------------------ UUID
def norm_uuid(s: str) -> str:
    """归一化 UUID：接受带/不带连字符、大小写，失败返回空串。"""
    s = (s or "").strip().replace("-", "").lower()
    if len(s) != 32:
        return ""
    try:
        return str(uuid.UUID(hex=s))
    except (ValueError, AttributeError):
        return ""


def uuid_from_ints(vals: List[int]) -> str:
    """IntArray[4] 或 LongArray[2] -> 标准 UUID 字符串。

    MC 的 UUID IntArray 按「由高到低」存 4 个 32 位有符号整数，
    直接 pack 成 16 字节即为 UUID 的 bytes 表示。
    """
    try:
        if len(vals) == 4:
            return str(uuid.UUID(bytes=struct.pack(">iiii", *[int(v) for v in vals])))
        if len(vals) == 2:
            return str(uuid.UUID(bytes=struct.pack(">qq", *[int(v) for v in vals])))
    except (struct.error, ValueError, OverflowError):
        pass
    return ""


def uuid_of_player(player: N.NBTTag) -> str:
    """从玩家 Compound 里尽力提取 UUID，兼容各版本写法。"""
    d = N.compound(player)
    # 1.16+：UUID 是 IntArray[4]
    node = d.get("UUID")
    if node is not None:
        got = uuid_from_ints(N.as_ints(node))
        if got:
            return got
        got = norm_uuid(N.as_str(node))
        if got:
            return got
    # 1.15 及以前：两个 Long
    most, least = d.get("UUIDMost"), d.get("UUIDLeast")
    if most is not None and least is not None:
        try:
            return uuid_from_ints([int(most.value), int(least.value)])
        except (TypeError, ValueError):
            pass
    return ""


def uuid_of_pointer(node: N.NBTTag) -> str:
    """26.1 的 singleplayer_uuid（IntArray[4]，也可能是字符串）。"""
    got = uuid_from_ints(N.as_ints(node))
    if got:
        return got
    return norm_uuid(N.as_str(node))


# ------------------------------------------------------------------ 目录
def player_dir(save_dir: str, create: bool = False) -> str:
    """玩家数据目录：26.1+ 是 players/data，旧版是 playerdata。"""
    new_dir = os.path.join(save_dir, "players", "data")
    if os.path.isdir(new_dir):
        return new_dir
    old_dir = os.path.join(save_dir, "playerdata")
    if create:
        os.makedirs(old_dir, exist_ok=True)
    return old_dir


def is_new_format(save_dir: str) -> bool:
    return os.path.isdir(os.path.join(save_dir, "players", "data"))


def list_player_files(save_dir: str) -> List[str]:
    """列出玩家数据文件里的 UUID（文件名去掉 .dat）。"""
    d = player_dir(save_dir)
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if fn.lower().endswith(".dat"):
            u = norm_uuid(fn[:-4])
            if u:
                out.append(u)
    return out


def is_running(save_dir: str) -> bool:
    """游戏开着时绝不改 level.dat（延迟导入避免与 sync 循环依赖）。"""
    try:
        from core.sync import SyncEngine
        return bool(SyncEngine.is_running(save_dir))
    except Exception:
        return False


def _backup_dir(save_dir: str) -> str:
    return os.path.join(save_dir, ".mcsync", BACKUP_SUBDIR)


MAX_BACKUPS = 8


def _raw_backup(path: str, save_dir: str, tag: str) -> str:
    """把原始文件原样备份到 .mcsync/playerdata_backup/。

    去重：云端同一版本的 level.dat 每次下载回来字节完全相同，
    若不复用，反复下载会在存档里堆几十份一模一样的备份。
    另外限制总份数，避免长期使用时无限增长。
    """
    d = _backup_dir(save_dir)
    os.makedirs(d, exist_ok=True)
    base = os.path.basename(path)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        raw = b""

    if raw:
        for fn in sorted(os.listdir(d)):
            # 只比同源同用途的备份：level.dat_old 的前缀不同，不会误命中
            if not fn.startswith(base + "-") or (f"-{tag}") not in fn:
                continue
            fp = os.path.join(d, fn)
            try:
                if os.path.getsize(fp) == len(raw):
                    with open(fp, "rb") as f:
                        if f.read() == raw:
                            return fp            # 内容一致，复用旧备份
            except OSError:
                pass

    dst = os.path.join(d, f"{base}-{time.strftime('%Y%m%d-%H%M%S')}-{tag}")
    n = 1
    while os.path.exists(dst):
        dst = os.path.join(d, f"{base}-{time.strftime('%Y%m%d-%H%M%S')}-{tag}-{n}")
        n += 1
    shutil.copy2(path, dst)

    # 上限清理：按时间删最旧的，永远保留最新那份
    try:
        subs = sorted((os.path.join(d, x) for x in os.listdir(d)
                       if os.path.isfile(os.path.join(d, x))),
                      key=lambda p: os.path.getmtime(p))
        for old in subs[:-MAX_BACKUPS]:
            os.remove(old)
    except OSError:
        pass
    return dst


def _is_shell(root: N.NBTTag) -> bool:
    """已有的 .dat 是不是「空号」：键太少且没有任何实质状态字段。"""
    try:
        keys = set(N.compound(root).keys())
    except Exception:
        return False        # 解析不了就当它是真实数据，绝不能覆盖
    if len(keys) >= 8 and (keys & CORE_PLAYER_KEYS):
        return False
    return True


# ------------------------------------------------------------------ 诊断
@dataclass
class SaveStatus:
    save_dir: str
    name: str = ""
    instance: str = ""
    ok: bool = True
    error: str = ""
    format: str = "none"           # legacy(Player) / new(singleplayer_uuid) / clean
    owner_uuid: str = ""
    player_files: List[str] = field(default_factory=list)
    has_level_dat: bool = False
    level_dat_old: bool = False
    needs_fix: bool = False
    running: bool = False

    def summary(self) -> str:
        if self.error:
            return f"❌ {self.error}"
        if not self.has_level_dat:
            return "— 没有 level.dat"
        if self.running:
            return "⏸ 游戏正在运行，暂不处理"
        if self.format == "clean":
            return f"✅ 已是共享友好（{len(self.player_files)} 份玩家数据）"
        if self.format == "legacy":
            who = self.owner_uuid[:8] + "…" if self.owner_uuid else "未知"
            return f"⚠ 内嵌房主玩家数据（{who}）→ 别人打开会变成房主"
        who = self.owner_uuid[:8] + "…" if self.owner_uuid else "未知"
        return f"⚠ singleplayer_uuid 指向房主（{who}）→ 别人打开会变成房主"


def inspect(save_dir: str, instance: str = "") -> SaveStatus:
    """只读诊断，一个字节都不改。"""
    lvl = os.path.join(save_dir, "level.dat")
    st = SaveStatus(save_dir=save_dir, name=os.path.basename(os.path.normpath(save_dir)),
                    instance=instance)
    st.has_level_dat = os.path.isfile(lvl)
    st.level_dat_old = os.path.isfile(os.path.join(save_dir, "level.dat_old"))
    st.player_files = list_player_files(save_dir)
    st.running = is_running(save_dir)
    if not st.has_level_dat:
        st.format = "none"
        return st
    try:
        _name, root, _kind = N.read_file(lvl)
        data = N.child(root, "Data")
        if data is None or data.type != N.TAG_COMPOUND:
            st.format = "clean"
            return st
        player = N.child(data, KEY_PLAYER)
        pointer = N.child(data, KEY_SP_UUID)
        if player is not None and player.type == N.TAG_COMPOUND:
            st.format = "legacy"
            st.owner_uuid = uuid_of_player(player)
            st.needs_fix = True
        elif pointer is not None:
            st.format = "new"
            st.owner_uuid = uuid_of_pointer(pointer)
            st.needs_fix = True
        else:
            st.format = "clean"
    except Exception as e:
        st.ok = False
        st.error = f"level.dat 解析失败：{e}"
    return st


# ------------------------------------------------------------------ 修正
@dataclass
class FixResult:
    save_dir: str = ""
    status: str = ""               # fixed / already_clean / is_mine / running / error / no_level_dat
    format: str = ""
    owner_uuid: str = ""
    archived_to: str = ""
    backup: str = ""
    player_backup: str = ""
    files_changed: List[str] = field(default_factory=list)   # 相对存档根的路径
    messages: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.status == "fixed"

    def summary(self) -> str:
        if self.status == "fixed":
            return (f"✅ 已解除共享：{self.format} 格式，原主人 "
                    f"{self.owner_uuid[:8] if self.owner_uuid else '未知'}…")
        return {
            "already_clean": "✅ 无需处理（已是共享友好）",
            "is_mine": "✅ 内嵌数据属于你自己，原样保留",
            "running": "⏸ 游戏正在运行，已跳过",
            "no_level_dat": "— 没有 level.dat",
            "error": "❌ 处理失败",
        }.get(self.status, self.status)


def fix(save_dir: str, my_uuid: str = "") -> FixResult:
    """解除一个存档的共享玩家数据。幂等；失败时尽量不动任何文件。"""
    r = FixResult(save_dir=save_dir)
    lvl = os.path.join(save_dir, "level.dat")
    if not os.path.isfile(lvl):
        r.status = "no_level_dat"
        return r
    if is_running(save_dir):
        r.status = "running"
        return r

    mine = norm_uuid(my_uuid)
    try:
        name, root, kind = N.read_file(lvl)
        data = N.child(root, "Data")
        if data is None or data.type != N.TAG_COMPOUND:
            r.status = "already_clean"
            return r
        d = N.compound(data)

        player = d.get(KEY_PLAYER)
        pointer = d.get(KEY_SP_UUID)
        if player is None and pointer is None:
            r.status = "already_clean"
            return r

        # ---- 判定原主人
        if player is not None and player.type == N.TAG_COMPOUND:
            r.format = "legacy"
            r.owner_uuid = uuid_of_player(player)
        elif pointer is not None:
            r.format = "new"
            r.owner_uuid = uuid_of_pointer(pointer)
        else:
            r.status = "already_clean"
            return r

        if mine and r.owner_uuid and mine == r.owner_uuid:
            r.status = "is_mine"
            return r

        # ---- 先备份原始 level.dat（备份到 .mcsync/，默认不同步）
        r.backup = _raw_backup(lvl, save_dir, "修改前")

        # ---- 老格式：把内嵌 Player 归档成独立 .dat
        if r.format == "legacy" and player is not None:
            archived = _archive_player(save_dir, player, r.owner_uuid, r)
            if archived:
                r.archived_to = os.path.relpath(archived, save_dir).replace("\\", "/")

        # ---- 摘除指针 / 内嵌数据
        removed_player = d.pop(KEY_PLAYER, None)
        removed_pointer = d.pop(KEY_SP_UUID, None)
        if removed_player is None and removed_pointer is None:
            r.status = "already_clean"
            return r

        # ---- 原子写回
        N.write_file(lvl, name, root, kind)
        r.files_changed.append("level.dat")

        # ---- level.dat_old 也一并处理：避免某些恢复流程把它当 level.dat 读回去
        old = os.path.join(save_dir, "level.dat_old")
        if os.path.isfile(old):
            try:
                n2, root2, k2 = N.read_file(old)
                data2 = N.child(root2, "Data")
                if data2 is not None and data2.type == N.TAG_COMPOUND:
                    d2 = N.compound(data2)
                    if KEY_PLAYER in d2 or KEY_SP_UUID in d2:
                        _raw_backup(old, save_dir, "修改前-old")
                        d2.pop(KEY_PLAYER, None)
                        d2.pop(KEY_SP_UUID, None)
                        N.write_file(old, n2, root2, k2)
                        r.files_changed.append("level.dat_old")
                        r.messages.append("level.dat_old 里的内嵌数据也已摘除")
            except Exception as e:
                r.messages.append(f"level.dat_old 未改动（{e}）；MC 正常不会读它")

        r.status = "fixed"
        if r.archived_to:
            r.messages.append(f"原主人数据已归档到 {r.archived_to}，房主仍可按自己 UUID 读回")
        else:
            r.messages.append("游戏将按各自 UUID 读取玩家数据，没有则以新角色进入")
        if not r.owner_uuid:
            r.messages.append("未能识别原主人 UUID，整份内嵌数据已另存备份，未丢弃")
        return r

    except Exception as e:
        r.status = "error"
        r.messages.append(f"{type(e).__name__}: {e}")
        # 若已经写了 level.dat 但后面出错，尝试用备份还原
        if r.backup and r.files_changed:
            try:
                shutil.copy2(r.backup, lvl)
                r.messages.append("已用备份还原 level.dat")
                r.files_changed.clear()
            except OSError:
                pass
        return r


def _archive_player(save_dir: str, player: N.NBTTag, owner: str, r: FixResult) -> str:
    """把 Data.Player 写成独立的玩家数据 .dat。

    读不出 UUID 时不猜名字 —— 存到 .mcsync 备份区，数据不丢，
    但不伪装成某个玩家的数据文件（伪装错了会顶掉别人的角色）。
    """
    if not owner:
        try:
            d = _backup_dir(save_dir)
            os.makedirs(d, exist_ok=True)
            dst = os.path.join(d, f"Player-{time.strftime('%Y%m%d-%H%M%S')}.dat")
            N.write_file(dst, "", N.NBTTag(N.TAG_COMPOUND, dict(N.compound(player))), "gzip")
            r.player_backup = dst
            r.messages.append("原主人 UUID 未知，内嵌数据已另存到 .mcsync 备份区")
        except Exception as e:
            r.messages.append(f"备份内嵌数据失败：{e}")
        return ""

    pdir = player_dir(save_dir, create=True)
    dst = os.path.join(pdir, owner + ".dat")
    # 写成一个根 Compound 的独立 NBT 文档（与 playerdata/*.dat 格式一致）
    doc = N.NBTTag(N.TAG_COMPOUND, dict(N.compound(player)))

    if os.path.isfile(dst):
        try:
            _n, existing, _k = N.read_file(dst)
            if not _is_shell(existing):
                r.messages.append(f"{owner[:8]}… 的数据文件已存在且是真实数据，保留原文件")
                return ""
            bak = dst + SPD_SUFFIX
            n = 1
            while os.path.exists(bak):
                bak = f"{dst}.{n}{SPD_SUFFIX}"
                n += 1
            shutil.copy2(dst, bak)
            r.messages.append(f"已有空壳数据文件，备份为 {os.path.basename(bak)}")
        except Exception as e:
            # 解析不了就当真实数据，绝不覆盖
            r.messages.append(f"已有数据文件无法判定，保留不覆盖（{e}）")
            return ""

    N.write_file(dst, "", doc, "gzip")
    r.files_changed.append(os.path.relpath(dst, save_dir).replace("\\", "/"))
    return dst


# ------------------------------------------------------------------ 批量
def iter_saves(instances) -> List[Tuple[str, str, str]]:
    """遍历所有实例的存档目录，返回 (实例名, 存档名, 路径)。"""
    out: List[Tuple[str, str, str]] = []
    seen = set()
    for inst in (instances or []):
        sd = getattr(inst, "saves_dir", "") or ""
        if not sd or not os.path.isdir(sd):
            continue
        try:
            names = sorted(os.listdir(sd))
        except OSError:
            continue
        for nm in names:
            p = os.path.join(sd, nm)
            if not os.path.isdir(p):
                continue
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                continue
            seen.add(key)
            if os.path.isfile(os.path.join(p, "level.dat")):
                out.append((inst.name, nm, p))
    return out


def inspect_all(instances) -> List[SaveStatus]:
    return [inspect(p, iname) for iname, _nm, p in iter_saves(instances)]


def fix_all(instances, my_uuid: str = "") -> List[FixResult]:
    results = []
    for _iname, _nm, p in iter_saves(instances):
        st = inspect(p)
        if not st.needs_fix or st.running or not st.ok:
            results.append(FixResult(
                save_dir=p,
                status=("running" if st.running else
                        "error" if not st.ok else
                        "no_level_dat" if not st.has_level_dat else "already_clean"),
                format=st.format, owner_uuid=st.owner_uuid,
                messages=[st.error] if st.error else []))
            continue
        results.append(fix(p, my_uuid))
    return results
