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
保类型的 NBT 读写器 —— 用于「读 → 删一个键 → 原样写回」这类场景。

为什么不能直接用 core/nbt.py：
  那个解析器把 NBT 拍平成裸 Python 对象（dict / list / int），
  读回来再写会丢失 TAG 类型信息：
    TAG_Int_Array 与 TAG_List<Int> 都变成 list[int]，无法区分；
    TAG_Byte_Array 变成 bytes，还能靠类型猜；
    空 TAG_List 的 item type 直接丢了。
  level.dat 是存档里最要命的文件，写错类型 MC 会直接判存档损坏。

  所以这里用 NBTTag(type, value) 保留每个节点的类型，
  Compound 保持插入顺序（Python 3.7+ dict 有序），
  List 连空列表的 item_type 一起保留 —— 做到语义级完全等价。

支持 gzip / zlib / 未压缩三种容器，写回时沿用原容器。
"""
from __future__ import annotations

import gzip
import os
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

_FIXED_FMT = {
    TAG_BYTE: ">b", TAG_SHORT: ">h", TAG_INT: ">i", TAG_LONG: ">q",
    TAG_FLOAT: ">f", TAG_DOUBLE: ">d",
}


class NBTTag:
    """一个 NBT 节点。value 的形态由 type 决定。"""
    __slots__ = ("type", "value")

    def __init__(self, tag_type: int, value: Any):
        self.type = tag_type
        self.value = value

    def __repr__(self) -> str:
        return f"NBTTag({self.type}, {type(self.value).__name__})"


class NBTError(ValueError):
    pass


# ------------------------------------------------------------------ 读
class TypedReader:
    def __init__(self, data: bytes):
        self.d = data
        self.i = 0

    def _read(self, n: int) -> bytes:
        if n < 0 or self.i + n > len(self.d):
            raise NBTError("NBT 数据意外结束")
        b = self.d[self.i:self.i + n]
        self.i += n
        return b

    def _num(self, tag: int) -> Any:
        return struct.unpack(_FIXED_FMT[tag], self._read(struct.calcsize(_FIXED_FMT[tag])))[0]

    def _string(self) -> str:
        n = struct.unpack(">H", self._read(2))[0]
        return self._read(n).decode("utf-8", errors="replace")

    def payload(self, tag: int) -> Any:
        if tag in _FIXED_FMT:
            return self._num(tag)
        if tag == TAG_BYTE_ARRAY:
            n = self._num(TAG_INT)
            return self._read(n)
        if tag == TAG_STRING:
            return self._string()
        if tag == TAG_LIST:
            item_type = self._num(TAG_BYTE)
            n = self._num(TAG_INT)
            if n < 0:
                raise NBTError("负数长度的 List")
            items = [NBTTag(item_type, self.payload(item_type)) for _ in range(n)]
            # 空列表也要记住声明的元素类型，写回才能一字不差
            return (item_type, items)
        if tag == TAG_COMPOUND:
            out: Dict[str, NBTTag] = {}
            while True:
                t = self._num(TAG_BYTE)
                if t == TAG_END:
                    break
                name = self._string()
                out[name] = NBTTag(t, self.payload(t))
            return out
        if tag == TAG_INT_ARRAY:
            n = self._num(TAG_INT)
            return list(struct.unpack(">%di" % n, self._read(4 * n)))
        if tag == TAG_LONG_ARRAY:
            n = self._num(TAG_INT)
            return list(struct.unpack(">%dq" % n, self._read(8 * n)))
        raise NBTError(f"未知 TAG 类型 {tag}")

    def parse(self) -> Tuple[str, NBTTag]:
        t = self._num(TAG_BYTE)
        if t != TAG_COMPOUND:
            raise NBTError("根标签必须是 Compound")
        name = self._string()
        return name, NBTTag(TAG_COMPOUND, self.payload(TAG_COMPOUND))


# ------------------------------------------------------------------ 写
class TypedWriter:
    def __init__(self):
        self.buf = bytearray()

    def _w(self, b: bytes) -> None:
        self.buf.extend(b)

    def _num(self, tag: int, v: Any) -> None:
        self._w(struct.pack(_FIXED_FMT[tag], v))

    def _string(self, s: str) -> None:
        b = s.encode("utf-8")
        self._w(struct.pack(">H", len(b) & 0xFFFF))
        self._w(b)

    def payload(self, node: NBTTag) -> None:
        t, v = node.type, node.value
        if t in _FIXED_FMT:
            self._num(t, v)
            return
        if t == TAG_BYTE_ARRAY:
            b = bytes(v)
            self._num(TAG_INT, len(b))
            self._w(b)
            return
        if t == TAG_STRING:
            self._string(str(v))
            return
        if t == TAG_LIST:
            item_type, items = v
            self._num(TAG_BYTE, item_type)
            self._num(TAG_INT, len(items))
            for it in items:
                # items 里存的是裸 payload（reader 里 NBTTag(item_type, payload)），
                # 这里按声明的 item_type 写，不再各自带 type
                self.payload(NBTTag(item_type, it.value if isinstance(it, NBTTag) else it))
            return
        if t == TAG_COMPOUND:
            for name, child in v.items():
                self._num(TAG_BYTE, child.type)
                self._string(name)
                self.payload(child)
            self._num(TAG_BYTE, TAG_END)
            return
        if t == TAG_INT_ARRAY:
            arr = list(v)
            self._num(TAG_INT, len(arr))
            self._w(struct.pack(">%di" % len(arr), *arr))
            return
        if t == TAG_LONG_ARRAY:
            arr = list(v)
            self._num(TAG_INT, len(arr))
            self._w(struct.pack(">%dq" % len(arr), *arr))
            return
        raise NBTError(f"无法写入 TAG 类型 {t}")

    def dump(self, name: str, root: NBTTag) -> bytes:
        self._num(TAG_BYTE, TAG_COMPOUND)
        self._string(name)
        self.payload(root)
        return bytes(self.buf)


# ------------------------------------------------------------------ 容器
def detect_container(raw: bytes) -> str:
    if raw[:2] == b"\x1f\x8b":
        return "gzip"
    if raw[:2] in (b"\x78\x01", b"\x78\x9c", b"\x78\xda", b"\x78\x5e"):
        return "zlib"
    return "raw"


def unpack(raw: bytes) -> bytes:
    kind = detect_container(raw)
    if kind == "gzip":
        return gzip.decompress(raw)
    if kind == "zlib":
        return zlib.decompress(raw)
    return raw


def pack(data: bytes, kind: str) -> bytes:
    if kind == "gzip":
        try:
            return gzip.compress(data, compresslevel=9, mtime=0)
        except TypeError:          # 极老 Python 没有 mtime 参数
            return gzip.compress(data, 9)
    if kind == "zlib":
        return zlib.compress(data, 9)
    return data


def read_file(path: str) -> Tuple[str, NBTTag, str]:
    """返回 (根名, 根节点, 容器类型)。"""
    with open(path, "rb") as f:
        raw = f.read()
    kind = detect_container(raw)
    name, root = TypedReader(unpack(raw)).parse()
    return name, root, kind


def write_file(path: str, name: str, root: NBTTag, kind: str = "gzip") -> None:
    """临时文件 + 原子替换：写一半断电也不会留下半截 level.dat。"""
    data = pack(TypedWriter().dump(name, root), kind)
    tmp = path + ".mcsync_tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def compound(node: NBTTag) -> Dict[str, NBTTag]:
    if node.type != TAG_COMPOUND or not isinstance(node.value, dict):
        raise NBTError("不是 Compound")
    return node.value


def child(parent: NBTTag, key: str):
    """取子节点，不存在返回 None。"""
    if parent.type != TAG_COMPOUND:
        return None
    return parent.value.get(key)


def as_ints(node: NBTTag | None) -> List[int]:
    """把 IntArray / LongArray / List<Int> 统一成 int 列表。"""
    if node is None:
        return []
    if node.type in (TAG_INT_ARRAY, TAG_LONG_ARRAY):
        return [int(x) for x in node.value]
    if node.type == TAG_LIST:
        item_type, items = node.value
        if item_type in _FIXED_FMT:
            return [int(x.value if isinstance(x, NBTTag) else x) for x in items]
    return []


def as_bytes(node: NBTTag | None) -> bytes:
    if node is None:
        return b""
    if node.type == TAG_BYTE_ARRAY:
        return bytes(node.value)
    return b""


def as_str(node: NBTTag | None) -> str:
    if node is None:
        return ""
    if node.type == TAG_STRING:
        return str(node.value)
    return ""
