# -*- coding: utf-8 -*-
"""
极简 NBT 解析器 —— 仅用于读取 level.dat 中的世界信息（无第三方依赖）。
支持 gzip / zlib / 未压缩三种容器，支持全部 13 种 TAG。
"""
from __future__ import annotations

import gzip
import struct
import zlib
from typing import Any, Dict, List, Tuple

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12


class NBTReader:
    def __init__(self, data: bytes):
        self.d = data
        self.i = 0

    def _read(self, n: int) -> bytes:
        if self.i + n > len(self.d):
            raise ValueError("NBT 数据意外结束")
        b = self.d[self.i:self.i + n]
        self.i += n
        return b

    def byte(self) -> int:
        return self._read(1)[0]

    def u1(self) -> int:
        return self._read(1)[0]

    def short(self) -> int:
        return struct.unpack(">h", self._read(2))[0]

    def ushort(self) -> int:
        return struct.unpack(">H", self._read(2))[0]

    def int(self) -> int:
        return struct.unpack(">i", self._read(4))[0]

    def long(self) -> int:
        return struct.unpack(">q", self._read(8))[0]

    def float(self) -> float:
        return struct.unpack(">f", self._read(4))[0]

    def double(self) -> float:
        return struct.unpack(">d", self._read(8))[0]

    def string(self) -> str:
        n = self.ushort()
        return self._read(n).decode("utf-8", errors="replace")

    def payload(self, tag: int) -> Any:
        if tag == TAG_END:
            return None
        if tag == TAG_BYTE:
            return self._read(1)[0] - 256 if self.d[self.i - 1] > 127 else self.d[self.i - 1]
        if tag == TAG_SHORT:
            return self.short()
        if tag == TAG_INT:
            return self.int()
        if tag == TAG_LONG:
            return self.long()
        if tag == TAG_FLOAT:
            return self.float()
        if tag == TAG_DOUBLE:
            return self.double()
        if tag == TAG_BYTE_ARRAY:
            n = self.int()
            return self._read(n)
        if tag == TAG_STRING:
            return self.string()
        if tag == TAG_LIST:
            item_type = self.u1()
            n = self.int()
            out: List[Any] = []
            for _ in range(n):
                out.append(self.payload(item_type))
            return out
        if tag == TAG_COMPOUND:
            out: Dict[str, Any] = {}
            while True:
                t = self.u1()
                if t == TAG_END:
                    break
                name = self.string()
                out[name] = self.payload(t)
            return out
        if tag == TAG_INT_ARRAY:
            n = self.int()
            return list(struct.unpack(">%di" % n, self._read(4 * n)))
        if tag == TAG_LONG_ARRAY:
            n = self.int()
            return list(struct.unpack(">%dq" % n, self._read(8 * n)))
        raise ValueError(f"未知 TAG 类型 {tag}")

    def parse(self) -> Tuple[str, Any]:
        t = self.u1()
        if t != TAG_COMPOUND:
            raise ValueError("根标签必须是 Compound")
        name = self.string()
        return name, self.payload(TAG_COMPOUND)


def decompress(raw: bytes) -> bytes:
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if raw[:2] in (b"\x78\x01", b"\x78\x9c", b"\x78\xda"):
        return zlib.decompress(raw)
    return raw


def read_nbt(path: str) -> Dict[str, Any]:
    with open(path, "rb") as f:
        raw = f.read()
    data = decompress(raw)
    _name, root = NBTReader(data).parse()
    return root or {}


GAMETYPE = {0: "生存", 1: "创造", 2: "冒险", 3: "旁观"}
DIFFICULTY = {0: "和平", 1: "简单", 2: "普通", 3: "困难"}


def level_info(path: str) -> Dict[str, Any]:
    """
    读取 level.dat 摘要。任何异常都被吞掉，返回尽可能多的信息。
    """
    info: Dict[str, Any] = {
        "world_name": "",
        "game_version": "",
        "gametype": "",
        "difficulty": "",
        "hardcore": False,
        "last_played": 0,
        "seed": None,
        "cheats": False,
        "ok": False,
    }
    try:
        root = read_nbt(path)
        data = root.get("Data", root)
        info["world_name"] = str(data.get("LevelName", "") or "")
        ver = data.get("Version")
        if isinstance(ver, dict):
            info["game_version"] = str(ver.get("Name", "") or "")
        elif isinstance(ver, str):
            info["game_version"] = ver
        try:
            info["gametype"] = GAMETYPE.get(int(data.get("GameType", 0)), "")
        except Exception:
            pass
        try:
            info["difficulty"] = DIFFICULTY.get(int(data.get("Difficulty", 2)), "")
        except Exception:
            pass
        info["hardcore"] = bool(data.get("hardcore", False))
        try:
            info["last_played"] = int(data.get("LastPlayed", 0)) / 1000.0
        except Exception:
            info["last_played"] = 0
        try:
            info["seed"] = int(data.get("WorldGenSettings", {}).get("seed", data.get("RandomSeed", 0)))
        except Exception:
            try:
                info["seed"] = int(data.get("RandomSeed", 0))
            except Exception:
                info["seed"] = None
        info["cheats"] = bool(data.get("allowCommands", False))
        info["ok"] = True
    except Exception:
        pass
    return info
