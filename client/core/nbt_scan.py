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
区块 NBT 的轻量字段提取器 —— 只找 InhabitedTime，不构建整棵 NBT 树。

为什么需要它（这是「MCA Selector 能筛、本工具不能」的根因之一）：
  真实 MC 区块 NBT 动辄几百 KB 未压缩，含 sections / palette / Heightmaps /
  block_entities 等大量嵌套 Compound、List、LongArray。把整棵树反序列化成
  Python 对象既慢又吃内存，且任何一处类型/字节处理瑕疵都会让**整个区块**
  判为「无法解析」，进而把整个存档禁用。

  这里只遍历根 Compound（以及老版本的 Level Compound）的**直接子项**：
    · 命中 InhabitedTime(TAG_Long) 立刻取值
    · 其余子项用 skip_payload 流式跳过，只移动指针，不解码、不建对象
  支持全部 13 种 TAG 的跳过，对损坏数据有严格预算，不会产生死循环或 OOM。

InhabitedTime 位置随版本变化：
    1.18+       根下
    1.17 及以前 Level 下
    字段缺失返回 None（绝不当 0，老版本必须保留）
"""
from __future__ import annotations

import struct
from typing import Optional

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

_FIXED = {TAG_BYTE: 1, TAG_SHORT: 2, TAG_INT: 4, TAG_LONG: 8,
          TAG_FLOAT: 4, TAG_DOUBLE: 8}

# 防御损坏数据：单个 chunk 最多跳过的字节数与最多处理的节点数
MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
MAX_NODES = 4_000_000
MAX_DEPTH = 64


class ScanError(ValueError):
    pass


class _Scanner:
    def __init__(self, data: bytes):
        self.d = data
        self.i = 0
        self.nodes = 0
        self.moved = 0

    def _take(self, n: int) -> bytes:
        if n < 0 or self.i + n > len(self.d):
            raise ScanError("NBT 数据意外结束")
        self.i += n
        self.moved += n
        if self.moved > MAX_PAYLOAD_BYTES:
            raise ScanError("NBT 数据过大")
        return self.d[self.i - n:self.i]

    def _bump(self):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ScanError("NBT 节点数超限")

    def u1(self) -> int:
        return self._take(1)[0]

    def i4(self) -> int:
        return struct.unpack(">i", self._take(4))[0]

    def i8(self) -> int:
        return struct.unpack(">q", self._take(8))[0]

    def name(self) -> str:
        n = struct.unpack(">H", self._take(2))[0]
        return self._take(n).decode("utf-8", errors="replace")

    def skip_payload(self, t: int, depth: int = 0) -> None:
        """跳过一个 TAG 的 payload，不构造任何 Python 对象。"""
        self._bump()
        if depth > MAX_DEPTH:
            raise ScanError("NBT 嵌套过深")
        if t in _FIXED:
            self._take(_FIXED[t])
            return
        if t == TAG_BYTE_ARRAY:
            n = self.i4()
            if n < 0:
                raise ScanError("负长度 ByteArray")
            self._take(n)
            return
        if t == TAG_STRING:
            n = struct.unpack(">H", self._take(2))[0]
            self._take(n)
            return
        if t == TAG_LIST:
            item = self.u1()
            n = self.i4()
            if n < 0:
                raise ScanError("负长度 List")
            if n:
                if item == TAG_END:
                    # 空类型 List 理论上不该有元素，损坏数据直接放弃
                    raise ScanError("TAG_End 类型的非空 List")
                for _ in range(n):
                    self.skip_payload(item, depth + 1)
            return
        if t == TAG_COMPOUND:
            while True:
                ct = self.u1()
                if ct == TAG_END:
                    return
                self.name()
                self.skip_payload(ct, depth + 1)
            return
        if t == TAG_INT_ARRAY:
            n = self.i4()
            if n < 0:
                raise ScanError("负长度 IntArray")
            self._take(4 * n)
            return
        if t == TAG_LONG_ARRAY:
            n = self.i4()
            if n < 0:
                raise ScanError("负长度 LongArray")
            self._take(8 * n)
            return
        raise ScanError(f"未知 TAG 类型 {t}")

    def _scan_compound(self, key: str, depth: int) -> Optional[int]:
        """遍历一个 Compound 的直接子项，找 key(TAG_Long)。

        命中就立刻返回 —— 后面的 sections / palette / Heightmaps 可能几百 KB，
        继续跳过它们既无意义，又会在「只解压了前缀」时误报截断。
        """
        while True:
            self._bump()
            t = self.u1()
            if t == TAG_END:
                return None
            nm = self.name()
            if nm == key and t == TAG_LONG:
                return self.i8()
            self.skip_payload(t, depth + 1)


def extract_inhabited(nbt: bytes) -> Optional[int]:
    """返回 InhabitedTime 的 tick 数；字段缺失返回 None；解析失败抛 ScanError。

    根必须是命名 Compound。依次尝试根下、Level 下两处。
    """
    s = _Scanner(nbt)
    root_type = s.u1()
    if root_type != TAG_COMPOUND:
        raise ScanError("根标签不是 Compound")
    s.name()                      # 根名字（通常为空串）
    val = s._scan_compound("InhabitedTime", 0)
    if val is not None:
        return val
    return _extract_legacy(nbt)


def _extract_legacy(nbt: bytes) -> Optional[int]:
    """1.17 及以前：InhabitedTime 嵌在根下的 Level Compound 里。"""
    s = _Scanner(nbt)
    if s.u1() != TAG_COMPOUND:
        return None
    s.name()
    while True:
        s._bump()
        t = s.u1()
        if t == TAG_END:
            return None
        nm = s.name()
        if nm == "Level" and t == TAG_COMPOUND:
            return s._scan_compound("InhabitedTime", 1)
        s.skip_payload(t, 1)
