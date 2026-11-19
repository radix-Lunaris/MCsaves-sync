# -*- coding: utf-8 -*-
"""
哈希工具：xxh128 优先（极快），无 xxhash 时降级 blake2b-128。
两级指纹：
  1) 廉价指纹 (size, mtime_int) 用于预筛，避免重复读盘
  2) 内容哈希：整文件 + 分块（4MiB）双哈希，分块用于断点续传/增量
"""
from __future__ import annotations

import hashlib
import os
from typing import Dict, List, Optional, Tuple

try:
    import xxhash  # type: ignore

    _HAS_XXH = True

    def _new_hasher():
        return xxhash.xxh128()

    def hash_bytes(data: bytes) -> str:
        return xxhash.xxh128(data).hexdigest()

    ALGO = "xxh128"
except Exception:  # pragma: no cover
    _HAS_XXH = False

    def _new_hasher():
        return hashlib.blake2b(digest_size=16)

    def hash_bytes(data: bytes) -> str:
        return hashlib.blake2b(data, digest_size=16).hexdigest()

    ALGO = "blake2b128"


READ_BUF = 1024 * 1024


def cheap_fingerprint(size: int, mtime: float) -> Tuple[int, int]:
    """廉价指纹：大小 + 修改时间（毫秒级，避免同一秒内的改动被漏掉）。"""
    return (int(size), int(round(float(mtime) * 1000)))


def file_hash(path: str) -> str:
    h = _new_hasher()
    with open(path, "rb") as f:
        while True:
            buf = f.read(READ_BUF)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def chunk_hashes(path: str, chunk_size: int, size: Optional[int] = None) -> List[str]:
    """按块顺序返回每个块的哈希；空文件返回 []。"""
    if size is None:
        size = os.path.getsize(path)
    if size <= 0:
        return []
    out: List[str] = []
    with open(path, "rb") as f:
        remaining = size
        while remaining > 0:
            n = min(chunk_size, remaining)
            h = _new_hasher()
            left = n
            while left > 0:
                buf = f.read(min(READ_BUF, left))
                if not buf:
                    break
                h.update(buf)
                left -= len(buf)
            out.append(h.hexdigest())
            remaining -= n
    return out


def hash_pieces(path: str, ranges) -> List[str]:
    """对 [(offset, length)] 逐段求哈希，只开一次文件句柄。"""
    out: List[str] = []
    if not ranges:
        return out
    with open(path, "rb") as f:
        for off, length in ranges:
            if length <= 0:
                out.append(hash_bytes(b""))
                continue
            h = _new_hasher()
            f.seek(off)
            left = length
            while left > 0:
                buf = f.read(min(READ_BUF, left))
                if not buf:
                    break
                h.update(buf)
                left -= len(buf)
            out.append(h.hexdigest())
    return out


class ScanResult:
    """一次本地扫描的结果缓存，避免重复 IO。"""

    def __init__(self) -> None:
        self.files: Dict[str, dict] = {}   # rel -> {size, mtime, hash, chunks}
        self.skipped: int = 0
        self.total_size: int = 0


def fingerprint_map(files: Dict[str, dict]) -> Dict[str, Tuple[int, int]]:
    return {k: cheap_fingerprint(v["size"], v["mtime"]) for k, v in files.items()}
