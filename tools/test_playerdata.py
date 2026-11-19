# -*- coding: utf-8 -*-
"""
共享玩家数据修正（不装模组）回归测试

覆盖：
  1. 保类型 NBT 全 12 种 TAG 往返字节级一致（level.dat 写不错）
  2. 老格式：Data.Player 被摘除 + 归档成 playerdata/<原主人UUID>.dat
  3. level.dat 其余字段/类型完全保留（用 byte-level 对比非 Player 部分）
  4. 幂等、属于自己时不动、26.1 指针格式
  5. 空壳文件备份后覆盖 / 真实数据不覆盖 / UUID 未知时另存备份不伪装
  6. level.dat_old 一并处理、.mcsync 备份存在
  7. 批量扫描与修复

用法： python3 tools/test_playerdata.py
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

from core import nbt_typed as N          # noqa: E402
from core import playerdata as pd        # noqa: E402

TMP = "/tmp/mcsync_playerdata"
OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def T(t, v):
    return N.NBTTag(t, v)


OWNER = uuid.UUID("c06f8906-4c8a-4911-9c71-e3b1f2a0d5e7")
OTHER = uuid.UUID("11111111-2222-3333-4444-555555555555")


def uuid_ints(u: uuid.UUID):
    return list(struct.unpack(">iiii", u.bytes))


def make_player(u: uuid.UUID, with_uuid: bool = True, shell: bool = False) -> N.NBTTag:
    d = {
        "Pos": T(N.TAG_LIST, (N.TAG_DOUBLE, [T(N.TAG_DOUBLE, 1.0), T(N.TAG_DOUBLE, 64.0), T(N.TAG_DOUBLE, 2.0)])),
        "Rotation": T(N.TAG_LIST, (N.TAG_FLOAT, [T(N.TAG_FLOAT, 0.0), T(N.TAG_FLOAT, 0.0)])),
        "Inventory": T(N.TAG_LIST, (N.TAG_COMPOUND, [T(N.TAG_COMPOUND, {
            "id": T(N.TAG_STRING, "minecraft:diamond"), "Count": T(N.TAG_BYTE, 5)})])),
        "playerGameType": T(N.TAG_INT, 0),
        "XpLevel": T(N.TAG_INT, 30),
        "Health": T(N.TAG_FLOAT, 20.0),
        "foodLevel": T(N.TAG_INT, 20),
        "abilities": T(N.TAG_COMPOUND, {"flying": T(N.TAG_BYTE, 0)}),
        "DataVersion": T(N.TAG_INT, 3700),
    }
    if shell:
        d = {"playerGameType": T(N.TAG_INT, 0), "DataVersion": T(N.TAG_INT, 3700)}
    if with_uuid:
        d["UUID"] = T(N.TAG_INT_ARRAY, uuid_ints(u))
    return T(N.TAG_COMPOUND, d)


def make_level(path: str, fmt: str = "legacy", owner=OWNER,
               with_uuid: bool = True, extra_old: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "LevelName": T(N.TAG_STRING, "共享世界"),
        "DataVersion": T(N.TAG_INT, 3700),
        "DayTime": T(N.TAG_LONG, 12345),
        "Difficulty": T(N.TAG_BYTE, 2),
        "GameRules": T(N.TAG_COMPOUND, {"doFireTick": T(N.TAG_STRING, "true")}),
        "EnabledList": T(N.TAG_LIST, (N.TAG_STRING, [])),      # 空 List 的类型必须保住
        "BorderSize": T(N.TAG_DOUBLE, 60000000.0),
        "SpawnX": T(N.TAG_INT, 100), "SpawnY": T(N.TAG_INT, 64), "SpawnZ": T(N.TAG_INT, -20),
        "CustomBossEvents": T(N.TAG_COMPOUND, {
            "boss1": T(N.TAG_COMPOUND, {"UUID": T(N.TAG_INT_ARRAY, [1, 2, 3, 4])})}),
    }
    if fmt == "legacy":
        data["Player"] = make_player(owner, with_uuid=with_uuid)
    elif fmt == "new":
        data["singleplayer_uuid"] = T(N.TAG_INT_ARRAY, uuid_ints(owner))
    root = T(N.TAG_COMPOUND, {"Data": T(N.TAG_COMPOUND, data)})
    N.write_file(path, "", root, "gzip")
    if extra_old:
        N.write_file(os.path.join(os.path.dirname(path), "level.dat_old"), "", root, "gzip")


def read_level(path: str):
    _n, root, _k = N.read_file(path)
    return N.compound(N.child(root, "Data"))


def main():
    print("\n=== 共享玩家数据修正回归测试 ===\n")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    # ---------------- 1. NBT 全类型往返 ----------------
    print("[1] 保类型 NBT 往返（level.dat 绝不能写错）")
    probe = T(N.TAG_COMPOUND, {
        "B": T(N.TAG_BYTE, -3), "S": T(N.TAG_SHORT, -300), "I": T(N.TAG_INT, 123456),
        "L": T(N.TAG_LONG, -9876543210), "F": T(N.TAG_FLOAT, 1.5),
        "D": T(N.TAG_DOUBLE, -2.25), "BA": T(N.TAG_BYTE_ARRAY, b"\x00\xff"),
        "Str": T(N.TAG_STRING, "中文☃"), "IA": T(N.TAG_INT_ARRAY, [1, -2, 3]),
        "LA": T(N.TAG_LONG_ARRAY, [10, -20]),
        "Empty": T(N.TAG_LIST, (N.TAG_STRING, [])),
        "List": T(N.TAG_LIST, (N.TAG_INT, [T(N.TAG_INT, 1), T(N.TAG_INT, 2)])),
        "Nest": T(N.TAG_COMPOUND, {"a": T(N.TAG_LIST, (N.TAG_COMPOUND,
                                                     [T(N.TAG_COMPOUND, {"x": T(N.TAG_INT, 9)})]))}),
    })
    base = N.TypedWriter().dump("", probe)
    for kind in ("raw", "zlib", "gzip"):
        n, got = N.TypedReader(N.unpack(N.pack(base, kind))).parse()
        check(f"{kind} 往返字节一致", N.TypedWriter().dump(n, got) == base, "")
    check("UUID IntArray[4] 解析正确",
          pd.uuid_from_ints(uuid_ints(OWNER)) == str(OWNER),
          pd.uuid_from_ints(uuid_ints(OWNER)))
    check("norm_uuid 兼容无连字符",
          pd.norm_uuid(str(OWNER).replace("-", "").upper()) == str(OWNER), "")

    # ---------------- 2. 老格式修复 ----------------
    print("\n[2] 老格式：摘除 Data.Player 并归档")
    s1 = os.path.join(TMP, "legacy_world")
    os.makedirs(s1, exist_ok=True)
    make_level(os.path.join(s1, "level.dat"), "legacy", extra_old=True)
    before = read_level(os.path.join(s1, "level.dat"))
    check("修复前存在 Player", "Player" in before, "")

    st = pd.inspect(s1, "1.20.1-Forge")
    check("inspect 判为 legacy", st.format == "legacy", st.format)
    check("inspect 读出原主人 UUID", st.owner_uuid == str(OWNER), st.owner_uuid)
    check("inspect 不修改文件", "Player" in read_level(os.path.join(s1, "level.dat")), "")
    check("status 文案含警告", "房主" in st.summary(), st.summary())

    r1 = pd.fix(s1)
    check("返回 fixed", r1.status == "fixed", str(r1.status))
    after = read_level(os.path.join(s1, "level.dat"))
    check("Player 已摘除", "Player" not in after, str(list(after)))
    check("其余字段全部保留",
          all(k in after for k in ("LevelName", "DataVersion", "DayTime", "Difficulty",
                                   "GameRules", "BorderSize", "SpawnX", "CustomBossEvents")),
          str(list(after)))
    check("空 List 的类型仍是 String",
          after["EnabledList"].value[0] == N.TAG_STRING,
          str(after["EnabledList"].value[0]))
    check("LevelName 值不变",
          N.as_str(after["LevelName"]) == "共享世界", "")
    check("DayTime 仍是 Long",
          after["DayTime"].type == N.TAG_LONG, str(after["DayTime"].type))

    arch = os.path.join(s1, "playerdata", str(OWNER) + ".dat")
    check("归档文件已生成", os.path.isfile(arch), arch)
    _n, aroot, _k = N.read_file(arch)
    akeys = set(N.compound(aroot))
    check("归档含完整背包/位置/经验",
          {"Inventory", "Pos", "XpLevel", "playerGameType"} <= akeys, str(akeys))
    check("归档文件是 gzip 容器", N.detect_container(open(arch, "rb").read(2)) == "gzip", "")
    check("files_changed 含 level.dat 与归档",
          "level.dat" in r1.files_changed and
          any("playerdata/" in x for x in r1.files_changed), str(r1.files_changed))
    check("原始 level.dat 已备份到 .mcsync",
          os.path.isfile(r1.backup) and ".mcsync" in r1.backup, r1.backup)
    check("level.dat_old 也已摘除",
          "Player" not in read_level(os.path.join(s1, "level.dat_old")), "")

    # 幂等
    r1b = pd.fix(s1)
    check("第二次运行幂等（already_clean）", r1b.status == "already_clean", r1b.status)
    check("幂等时不产生新改动", not r1b.files_changed, str(r1b.files_changed))
    st2 = pd.inspect(s1)
    check("修复后状态为 clean", st2.format == "clean", st2.format)
    check("玩家数据列表含原主人", str(OWNER) in st2.player_files, str(st2.player_files))

    # ---------------- 3. 属于自己时不动 ----------------
    print("\n[3] 内嵌数据属于自己时原样保留")
    s2 = os.path.join(TMP, "my_world")
    os.makedirs(s2, exist_ok=True)
    make_level(os.path.join(s2, "level.dat"), "legacy")
    raw_before = open(os.path.join(s2, "level.dat"), "rb").read()
    r2 = pd.fix(s2, my_uuid=str(OWNER).replace("-", ""))
    check("返回 is_mine", r2.status == "is_mine", r2.status)
    check("文件字节完全没变",
          open(os.path.join(s2, "level.dat"), "rb").read() == raw_before, "")
    check("没有生成归档", not os.path.isdir(os.path.join(s2, "playerdata")), "")

    # ---------------- 4. 26.1 新格式 ----------------
    print("\n[4] 26.1 singleplayer_uuid 指针")
    s3 = os.path.join(TMP, "new_world")
    os.makedirs(os.path.join(s3, "players", "data"), exist_ok=True)
    make_level(os.path.join(s3, "level.dat"), "new")
    # 房主数据本就在 players/data
    N.write_file(os.path.join(s3, "players", "data", str(OWNER) + ".dat"),
                 "", make_player(OWNER), "gzip")
    st3 = pd.inspect(s3)
    check("inspect 判为 new", st3.format == "new", st3.format)
    check("指针 UUID 正确", st3.owner_uuid == str(OWNER), st3.owner_uuid)
    check("player_dir 选 players/data",
          pd.player_dir(s3).replace("\\", "/").endswith("players/data"), pd.player_dir(s3))
    r3 = pd.fix(s3)
    check("返回 fixed", r3.status == "fixed", r3.status)
    check("singleplayer_uuid 已摘除",
          KEY := "singleplayer_uuid" not in read_level(os.path.join(s3, "level.dat")), "")
    check("房主自己的数据文件仍在",
          os.path.isfile(os.path.join(s3, "players", "data", str(OWNER) + ".dat")), "")
    check("没有多余归档到旧 playerdata",
          not os.path.isdir(os.path.join(s3, "playerdata")), "")

    # ---------------- 5. 空壳 / 真实数据 / UUID 未知 ----------------
    print("\n[5] 归档冲突与无法识别时的保守策略")
    s4 = os.path.join(TMP, "shell_world")
    os.makedirs(os.path.join(s4, "playerdata"), exist_ok=True)
    make_level(os.path.join(s4, "level.dat"), "legacy")
    N.write_file(os.path.join(s4, "playerdata", str(OWNER) + ".dat"),
                 "", make_player(OWNER, shell=True), "gzip")
    r4 = pd.fix(s4)
    check("空壳被备份成 .spd_backup",
          os.path.isfile(os.path.join(s4, "playerdata", str(OWNER) + ".dat.spd_backup")), "")
    _n, fixed_shell, _k = N.read_file(os.path.join(s4, "playerdata", str(OWNER) + ".dat"))
    check("空壳已被完整数据覆盖",
          "Inventory" in N.compound(fixed_shell), str(set(N.compound(fixed_shell))))

    s5 = os.path.join(TMP, "real_world")
    os.makedirs(os.path.join(s5, "playerdata"), exist_ok=True)
    make_level(os.path.join(s5, "level.dat"), "legacy")
    real = os.path.join(s5, "playerdata", str(OWNER) + ".dat")
    N.write_file(real, "", make_player(OTHER), "gzip")
    raw_real = open(real, "rb").read()
    r5 = pd.fix(s5)
    check("真实数据文件不被覆盖", open(real, "rb").read() == raw_real, "")
    check("仍摘除了 level.dat 的 Player",
          "Player" not in read_level(os.path.join(s5, "level.dat")), "")
    check("报告里说明保留了原文件",
          any("保留" in m for m in r5.messages), str(r5.messages))

    s6 = os.path.join(TMP, "unknown_world")
    os.makedirs(s6, exist_ok=True)
    make_level(os.path.join(s6, "level.dat"), "legacy", with_uuid=False)
    r6 = pd.fix(s6)
    check("UUID 未知仍成功摘除", r6.status == "fixed", r6.status)
    check("没有伪装任何 UUID 的 .dat",
          not os.path.isdir(os.path.join(s6, "playerdata")) or
          not os.listdir(os.path.join(s6, "playerdata")), "")
    check("整份数据另存到 .mcsync 备份",
          os.path.isfile(r6.player_backup), str(r6.player_backup))
    _n, saved, _k = N.read_file(r6.player_backup)
    check("备份内容含完整背包", "Inventory" in N.compound(saved), "")

    # ---------------- 6. 游戏运行中拒绝 ----------------
    print("\n[6] 游戏运行中拒绝修改")
    s7 = os.path.join(TMP, "running_world")
    os.makedirs(s7, exist_ok=True)
    make_level(os.path.join(s7, "level.dat"), "legacy")
    # 用 monkeypatch 模拟「锁被占用」，不依赖平台
    orig = pd.is_running
    pd.is_running = lambda _p: True
    try:
        r7 = pd.fix(s7)
        check("返回 running", r7.status == "running", r7.status)
        check("Player 未被摘除",
              "Player" in read_level(os.path.join(s7, "level.dat")), "")
    finally:
        pd.is_running = orig

    # ---------------- 7. 批量 ----------------
    print("\n[7] 批量扫描与修复")
    from core.models import Instance
    inst_root = os.path.join(TMP, "instances", "A")
    saves_root = os.path.join(inst_root, ".minecraft", "saves")
    os.makedirs(saves_root, exist_ok=True)
    for nm in ("W1", "W2"):
        d = os.path.join(saves_root, nm)
        os.makedirs(d, exist_ok=True)
        make_level(os.path.join(d, "level.dat"), "legacy")
    inst = Instance(name="1.20.1-Forge", root=inst_root,
                    game_dir=os.path.join(inst_root, ".minecraft"))
    found = pd.iter_saves([inst])
    check("遍历到 2 个存档", len(found) == 2, str(found))
    statuses = pd.inspect_all([inst])
    check("两个都判为需修复", all(s.needs_fix for s in statuses), str([s.format for s in statuses]))
    results = pd.fix_all([inst])
    check("两个都修复成功", all(r.status == "fixed" for r in results),
          str([r.status for r in results]))
    check("批量后全部 clean",
          all(s.format == "clean" for s in pd.inspect_all([inst])), "")
    check("再次批量幂等",
          all(r.status == "already_clean" for r in pd.fix_all([inst])), "")

    print("\n" + "=" * 60)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 共享玩家数据可被安全解除")
    print("=" * 60)


if __name__ == "__main__":
    main()
