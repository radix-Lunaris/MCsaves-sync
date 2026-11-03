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
分块传输层：4MiB 内容寻址块 + 多线程并发 + 断点续传 + 实时速率统计
"""
from __future__ import annotations

import os
from core.protocol import pack_piece
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

from core.api import Api, MCSyncError


class RateMeter:
    """滑动窗口速率统计"""

    def __init__(self, window: float = 5.0):
        self.window = window
        self._lock = threading.Lock()
        self._samples: List[tuple] = []
        self.total = 0

    def add(self, n: int) -> None:
        with self._lock:
            self.total += n
            self._samples.append((time.time(), n))

    def speed(self) -> float:
        with self._lock:
            now = time.time()
            self._samples = [(t, n) for (t, n) in self._samples if now - t <= self.window]
            if not self._samples:
                return 0.0
            span = max(0.5, now - self._samples[0][0])
            return sum(n for _, n in self._samples) / span

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self.total = 0


class CancelToken:
    def __init__(self):
        self._e = threading.Event()

    def cancel(self):
        self._e.set()

    @property
    def cancelled(self) -> bool:
        return self._e.is_set()

    def raise_if(self):
        if self._e.is_set():
            raise MCSyncError("已取消")


class Progress:
    """进度聚合器：向 UI 回调（自带节流）"""

    def __init__(self, cb: Optional[Callable[[dict], None]] = None, interval: float = 0.15):
        self.cb = cb
        self.interval = interval
        self._lock = threading.Lock()
        self._last = 0.0
        self.state: Dict[str, object] = {
            "stage": "", "file": "", "done_bytes": 0, "total_bytes": 0,
            "done_items": 0, "total_items": 0, "speed": 0.0, "eta": 0.0,
            "finished": False, "error": "",
        }
        self.meter = RateMeter()

    def set_stage(self, stage: str, **kw):
        with self._lock:
            self.state.update(stage=stage, **kw)
        self.emit(force=True)

    def add_bytes(self, n: int):
        self.meter.add(n)
        with self._lock:
            self.state["done_bytes"] = int(self.state["done_bytes"]) + n
            self.state["speed"] = self.meter.speed()
            total = int(self.state["total_bytes"])
            done = int(self.state["done_bytes"])
            sp = float(self.state["speed"])
            self.state["eta"] = (total - done) / sp if sp > 1 else 0.0
        self.emit()

    def add_items(self, n: int = 1):
        with self._lock:
            self.state["done_items"] = int(self.state["done_items"]) + n
        self.emit()

    def set(self, **kw):
        with self._lock:
            self.state.update(**kw)
        self.emit(force=True)

    def emit(self, force: bool = False):
        now = time.time()
        if not force and now - self._last < self.interval:
            return
        self._last = now
        if self.cb:
            try:
                with self._lock:
                    snap = dict(self.state)
                self.cb(snap)
            except Exception:
                pass

    def finish(self, error: str = ""):
        self.set(finished=True, error=error)
        self.emit(force=True)


class ChunkTransfer:
    def __init__(self, api: Api, concurrency: int = 4, chunk_size: int = 4 * 1024 * 1024,
                 progress: Optional[Progress] = None, cancel: Optional[CancelToken] = None,
                 log: Callable[[str], None] = print):
        self.api = api
        self.concurrency = max(1, min(32, concurrency))
        self.chunk_size = chunk_size
        self.progress = progress or Progress()
        self.cancel = cancel or CancelToken()
        self.log = log

    # ------------------------------------------------------------ 上传
    def upload_chunks(self, tasks: List[dict], max_retry: int = 3) -> int:
        """
        tasks: [{hash, path, offset, size}]
        返回成功上传的字节数
        """
        total = sum(t["size"] for t in tasks)
        self.progress.set(total_bytes=total, done_bytes=0, total_items=len(tasks), done_items=0)
        if not tasks:
            return 0

        def work(t: dict) -> int:
            last_err = None
            for attempt in range(max_retry):
                self.cancel.raise_if()
                try:
                    with open(t["path"], "rb") as f:
                        f.seek(t["offset"])
                        data = f.read(t["size"])
                    if len(data) != t["size"]:
                        raise MCSyncError(f"读取片段失败：{t['path']}")
                    # 块级压缩：能压就压，压不动（region 本身已压缩）就原样存，
                    # 由 1 字节前缀标识，读取时自动还原
                    blob = pack_piece(data)
                    self.api.put_blob(t["hash"], blob, len(blob))
                    self.progress.add_bytes(len(blob))
                    self.progress.add_items(1)
                    return len(data)
                except MCSyncError as e:
                    last_err = e
                    if self.cancel.cancelled:
                        raise
                    time.sleep(0.6 * (attempt + 1))
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    time.sleep(0.6 * (attempt + 1))
            raise MCSyncError(f"数据块上传失败（{t['hash'][:8]}）：{last_err}")

        done = 0
        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="up") as ex:
            futs = [ex.submit(work, t) for t in tasks]
            for fu in as_completed(futs):
                self.cancel.raise_if()
                done += fu.result()
        return done

    # ------------------------------------------------------------ 下载
    def download_chunks(self, hashes: List[str], cache_dir: str) -> Dict[str, str]:
        """
        下载缺失块到 cache_dir/<hash>，返回 hash -> 本地缓存路径
        已存在的缓存直接复用（天然断点续传）
        """
        os.makedirs(cache_dir, exist_ok=True)
        self.last_new_downloads = 0
        todo: List[tuple] = []
        result: Dict[str, str] = {}
        for h in hashes:
            p = os.path.join(cache_dir, h)
            if os.path.exists(p) and os.path.getsize(p) > 0:
                result[h] = p
            else:
                todo.append((h, p))
        total_size_est = self.chunk_size * len(todo)
        self.progress.set(total_bytes=int(self.progress.state.get("total_bytes", 0)) + total_size_est,
                          total_items=int(self.progress.state.get("total_items", 0)) + len(todo))
        self.last_new_downloads = len(todo)
        if not todo:
            return result

        def work(item) -> tuple:
            h, p = item
            last_err = None
            for attempt in range(3):
                self.cancel.raise_if()
                try:
                    n = self.api.get_blob_to_file(h, p)
                    self.progress.add_bytes(n)
                    self.progress.add_items(1)
                    return h, p
                except MCSyncError as e:
                    last_err = e
                    if self.cancel.cancelled:
                        raise
                    time.sleep(0.6 * (attempt + 1))
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    time.sleep(0.6 * (attempt + 1))
            raise MCSyncError(f"数据块下载失败（{h[:8]}）：{last_err}")

        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="dl") as ex:
            futs = [ex.submit(work, it) for it in todo]
            for fu in as_completed(futs):
                self.cancel.raise_if()
                h, p = fu.result()
                result[h] = p
        return result


def assemble_file(chunks: List[str], cache: Dict[str, str], dest: str, size: int = 0) -> int:
    """按块顺序拼接写入目标文件（先写 .part 再原子替换）"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    written = 0
    with open(tmp, "wb") as out:
        for h in chunks:
            src = cache.get(h)
            if not src or not os.path.exists(src):
                raise MCSyncError(f"缺少数据块 {h[:8]}，无法组装 {os.path.basename(dest)}")
            with open(src, "rb") as f:
                while True:
                    buf = f.read(1024 * 1024)
                    if not buf:
                        break
                    out.write(buf)
                    written += len(buf)
    if size and written != size:
        os.remove(tmp)
        raise MCSyncError(f"文件组装大小不符：{os.path.basename(dest)}")
    if os.path.exists(dest):
        os.remove(dest)
    os.replace(tmp, dest)
    return written
