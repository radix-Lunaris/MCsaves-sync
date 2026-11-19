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
MCSync 服务端存储层
- 内容寻址 blob（4MiB 分块），天然去重 + 断点续传 + 并发上传
- 版本快照（manifest json） + SQLite 元数据
- 免认证单空间模式
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from typing import Dict, Iterable, List, Optional

from protocol import MANIFEST_VERSION, normalize_save_name
from protocol import unpack_piece

try:  # 服务端可选：用于校验客户端上传块的哈希
    import xxhash  # type: ignore

    def _hash_bytes(data: bytes) -> str:
        return xxhash.xxh128(data).hexdigest()
except Exception:  # pragma: no cover
    xxhash = None  # type: ignore

    def _hash_bytes(data: bytes) -> str:  # type: ignore[misc]
        import hashlib

        return hashlib.blake2b(data, digest_size=16).hexdigest()


def ts_now() -> float:
    return time.time()


def now_ts() -> float:
    return time.time()


def fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


class Store:
    def __init__(self, root: str, verify_blob: bool = False, keep_versions: int = 0):
        self.root = os.path.abspath(root)
        self.blob_dir = os.path.join(self.root, "blobs")
        self.manifest_dir = os.path.join(self.root, "manifests")
        self.trash_dir = os.path.join(root, "trash")
        os.makedirs(self.trash_dir, exist_ok=True)
        self.tmp_dir = os.path.join(self.root, "tmp")
        self.db_path = os.path.join(self.root, "mcsync.db")
        self.verify_blob = bool(verify_blob) and xxhash is not None
        self.keep_versions = int(keep_versions)  # 0 = 不限制
        for d in (self.blob_dir, self.manifest_dir, self.tmp_dir):
            os.makedirs(d, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # 部分文件系统（网络盘 / 容器卷）不支持 WAL，退回默认日志模式
            pass
        try:
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        self._init_db()

    # ------------------------------------------------------------ 初始化

    def _init_db(self) -> None:
        with self._lock:
            c = self._conn
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS saves(
                    name TEXT PRIMARY KEY,
                    created_at REAL,
                    updated_at REAL,
                    instance TEXT DEFAULT '',
                    display TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    save_name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    note TEXT DEFAULT '',
                    file_count INTEGER DEFAULT 0,
                    total_size INTEGER DEFAULT 0,
                    manifest_file TEXT NOT NULL,
                    parent INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_versions_save ON versions(save_name, id);
                CREATE TABLE IF NOT EXISTS blobs(
                    hash TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blob_ref(
                    hash TEXT NOT NULL,
                    version_id INTEGER NOT NULL,
                    PRIMARY KEY(hash, version_id)
                );
                CREATE INDEX IF NOT EXISTS idx_blobref_hash ON blob_ref(hash);
                CREATE TABLE IF NOT EXISTS trash(
                    name TEXT PRIMARY KEY,
                    display TEXT DEFAULT '',
                    instance TEXT DEFAULT '',
                    deleted_at REAL,
                    deleted_by TEXT DEFAULT '',
                    created_at REAL,
                    version_count INTEGER DEFAULT 0,
                    total_size INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL,
                    actor TEXT DEFAULT '',
                    action TEXT,
                    target TEXT DEFAULT '',
                    detail TEXT DEFAULT ''
                );
                """
            )
            c.commit()
            self._migrate()
            self._ensure_columns()

    def _ensure_columns(self) -> None:
        """补齐后来新增的列。放在 _init_db 末尾统一执行，避免漏迁移。"""
        alters = [
            ("versions", "user", "TEXT DEFAULT ''"),
            ("saves", "instance", "TEXT DEFAULT ''"),
            ("saves", "display", "TEXT DEFAULT ''"),
            ("saves", "kind", "TEXT DEFAULT 'save'"),   # save=存档 / config=整合包配置
        ]
        for tbl, col, decl in alters:
            try:
                self._conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {decl}")
                self._conn.commit()
            except Exception:
                pass  # 列已存在

    def _migrate(self) -> None:
        """升级旧库结构，保证已有数据不丢。

        两种旧形态都要处理：
          a) 没有 instance 列           -> 先补列
          b) 主键只有 name（单主键）      -> 重建为 (instance, name) 复合主键

        为什么需要复合主键：同一个存档名（如 MyWorld）可能同时存在于
        多个整合包里，它们是不同的存档。单主键下后上传的会覆盖先上传的，
        表现为"两个整合包的 MyWorld 只剩一个、归属来回跳"。
        """
        with self._lock:
            c = self._conn
            cols = [r["name"] for r in c.execute("PRAGMA table_info(saves)")]
            if "instance" not in cols:
                try:
                    c.execute("ALTER TABLE saves ADD COLUMN instance TEXT DEFAULT ''")
                    c.commit()
                except Exception:
                    pass

            if "display" not in cols:
                try:
                    c.execute("ALTER TABLE saves ADD COLUMN display TEXT DEFAULT ''")
                    c.commit()
                except Exception:
                    pass

    # ------------------------------------------------------------ blob

    def blob_path(self, h: str) -> str:
        safe = "".join(ch for ch in h if ch in "0123456789abcdefABCDEF")
        if len(safe) < 4:
            raise ValueError("非法 hash")
        return os.path.join(self.blob_dir, safe[:2], safe[2:4], safe)

    def has_blob(self, h: str) -> bool:
        try:
            return os.path.exists(self.blob_path(h))
        except (ValueError, OSError):
            # 非法哈希不应让整个请求 500：当作缺失块，交给上层报 409
            return False

    def has_blobs(self, hashes: Iterable[str]) -> List[str]:
        return [h for h in hashes if self.has_blob(h)]

    def missing_blobs(self, hashes: Iterable[str]) -> List[str]:
        seen, out = set(), []
        for h in hashes:
            if h and h not in seen and not self.has_blob(h):
                seen.add(h)
                out.append(h)
        return out

    def put_blob(self, h: str, data: bytes, declared_size: Optional[int] = None) -> None:
        if declared_size is not None and len(data) != declared_size:
            raise ValueError(f"块大小不符: 声明 {declared_size} 实际 {len(data)}")
        if self.verify_blob:
            actual = _hash_bytes(data)
            if actual != h:
                raise ValueError(f"块哈希不符: 期望 {h} 实际 {actual}")
        p = self.blob_path(h)
        if os.path.exists(p):
            return
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + f".tmp-{uuid.uuid4().hex}"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO blobs(hash,size,created_at) VALUES(?,?,?)",
                (h, len(data), now_ts()),
            )

    def read_blob(self, h: str) -> Optional[str]:
        p = self.blob_path(h)
        return p if os.path.exists(p) else None

    # ------------------------------------------------------------ manifest

    def _save_dir(self, name: str) -> str:
        d = os.path.join(self.manifest_dir, name)
        os.makedirs(d, exist_ok=True)
        return d

    def _manifest_path(self, name: str, version_id: int) -> str:
        return os.path.join(self._save_dir(name), f"{version_id}.json")

    def load_manifest(self, name: str, version_id: int) -> Optional[dict]:
        p = self._manifest_path(name, version_id)
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------ 版本

    def list_saves(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, created_at, updated_at, instance, display, kind FROM saves ORDER BY updated_at DESC"
            ).fetchall()
        out = []
        for r in rows:
            vers = self.list_versions(r["name"])
            latest = vers[-1] if vers else None
            out.append(
                {
                    "name": r["name"],
                    "instance": (r["instance"] or "").strip(),
                    "display": (r["display"] or r["name"]),
                    "kind": (r["kind"] or "save"),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "version_count": len(vers),
                    "latest_version": latest["id"] if latest else None,
                    "latest_time": latest["created_at"] if latest else None,
                    "file_count": latest["file_count"] if latest else 0,
                    "total_size": latest["total_size"] if latest else 0,
                    "versions": vers,
                }
            )
        return out

    def list_versions(self, name: str) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM versions WHERE save_name=? ORDER BY id ASC", (name,)
            ).fetchall()
        return [
            {
                "id": r["id"],
                "save_name": r["save_name"],
                "created_at": r["created_at"],
                "created_at_str": fmt_time(r["created_at"]),
                "note": r["note"],
                "file_count": r["file_count"],
                "total_size": r["total_size"],
                "parent": r["parent"],
                "user": (r["user"] if "user" in r.keys() else "") or "",
            }
            for r in rows
        ]

    def list_saves_with_users(self) -> List[dict]:
        """云端存档列表，附带最近上传者（多人共用时很关键）。"""
        saves = self.list_saves()
        for x in saves or []:
            x["last_user"] = self._last_user(x.get("name", ""))
        return saves

    def _last_user(self, name: str) -> str:
        try:
            with self._lock:
                r = self._conn.execute(
                    "SELECT user FROM versions WHERE save_name=? ORDER BY id DESC LIMIT 1",
                    (name,)).fetchone()
            return (r[0] if r and r[0] else "") or ""
        except Exception:
            return ""

    def latest_version(self, name: str) -> Optional[dict]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM versions WHERE save_name=? ORDER BY id DESC LIMIT 1", (name,)
            ).fetchone()
        if not r:
            return None
        return {
            "id": r["id"],
            "save_name": r["save_name"],
            "created_at": r["created_at"],
            "created_at_str": fmt_time(r["created_at"]),
            "note": r["note"],
            "file_count": r["file_count"],
            "total_size": r["total_size"],
            "parent": r["parent"],
        }

    def get_version(self, name: str, version_id: int) -> Optional[dict]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM versions WHERE save_name=? AND id=?", (name, version_id)
            ).fetchone()
        if not r:
            return None
        return dict(r)

    def create_version(
        self,
        name: str,
        files: Dict[str, dict],
        note: str = "",
        parent: int = 0,
        instance: str = "",
        user: str = "",
        kind: str = "save",
    ) -> dict:
        plain = normalize_save_name(name)
        inst = (instance or "").strip()
        ts = now_ts()
        total = sum(int(f.get("size", 0)) for f in files.values())
        with self._lock:
            # 解析本次上传应落到哪条云端存档记录。
            # 同一个存档名（如 MyWorld）可能同时存在于多个整合包里，
            # 它们是不同的存档，不能互相覆盖；也不能靠 v1/v2 区分。
            name = self._resolve_target(plain, inst)
            self._conn.execute(
                "INSERT OR IGNORE INTO saves(name, created_at, updated_at, instance, display)"
                " VALUES(?,?,?,?,?)",
                (name, ts, ts, inst, plain),
            )
            self._conn.execute("UPDATE saves SET kind=? WHERE name=?", (kind or "save", name))
            if inst:
                self._conn.execute(
                    "UPDATE saves SET instance=?, display=? WHERE name=?", (inst, plain, name))
            cur = self._conn.execute(
                "INSERT INTO versions(save_name, created_at, note, file_count, total_size, manifest_file, parent, user)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (name, ts, note, len(files), total, "", parent, user or ""),
            )
            vid = int(cur.lastrowid)
            self._conn.execute("UPDATE saves SET updated_at=? WHERE name=?", (ts, name))
            manifest = {
                "version": MANIFEST_VERSION,
                "save_name": name,
                "version_id": vid,
                "created_at": ts,
                "created_at_str": fmt_time(ts),
                "note": note,
                "parent": parent,
                "file_count": len(files),
                "total_size": total,
                "files": files,
            }
            mpath = self._manifest_path(name, vid)
            tmp = mpath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False)
            os.replace(tmp, mpath)
            self._conn.execute("UPDATE versions SET manifest_file=? WHERE id=?", (mpath, vid))

            refs = set()
            for f in files.values():
                for h in f.get("chunks", []) or []:
                    refs.add(h)
                if f.get("hash") and not f.get("chunks") and int(f.get("size", 0) or 0) > 0:
                    refs.add(f["hash"])
            if refs:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO blob_ref(hash, version_id) VALUES(?,?)",
                    [(h, vid) for h in refs],
                )
            self._conn.commit()
        self._prune(name)
        return {
            "id": vid,
            "save_name": name,
            "created_at": ts,
            "created_at_str": fmt_time(ts),
            "note": note,
            "file_count": len(files),
            "total_size": total,
        }

    def restore_version(self, name: str, version_id: int) -> dict:
        """回滚：把旧版本内容复制为一个全新版本（不破坏历史）。"""
        m = self.load_manifest(name, version_id)
        if not m:
            raise KeyError("版本不存在")
        return self.create_version(
            name,
            m.get("files", {}),
            note=f"回滚自 v{version_id}",
            parent=int(version_id),
        )

    def delete_version(self, name: str, version_id: int) -> bool:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM versions WHERE save_name=? AND id=?", (name, version_id)
            ).fetchone()
            if not r:
                return False
            self._conn.execute("DELETE FROM versions WHERE id=?", (version_id,))
            self._conn.execute("DELETE FROM blob_ref WHERE version_id=?", (version_id,))
            self._conn.commit()
        mpath = self._manifest_path(name, version_id)
        if os.path.exists(mpath):
            os.remove(mpath)
        remain = self.list_versions(name)
        if not remain:
            with self._lock:
                self._conn.execute("DELETE FROM saves WHERE name=?", (name,))
                self._conn.commit()
            shutil.rmtree(self._save_dir(name), ignore_errors=True)
        self.gc()
        return True

    # ------------------------------------------------------------ 分片还原
    @staticmethod
    def pieces_of(f: dict) -> list:
        """取文件条目的分片；兼容旧版只有 chunks 的清单。"""
        ps = f.get("pieces")
        if ps:
            return ps
        chunks = f.get("chunks") or []
        out, off = [], 0
        size = int(f.get("size", 0) or 0)
        for h in chunks:
            n = min(4 * 1024 * 1024, size - off) if size else 0
            out.append([off, n, h])
            off += n
        return out

    def assemble_file(self, f: dict, dest: str) -> int:
        """按 offset 还原文件（region 分组分片必须按偏移写，顺序拼接会错位）。"""
        size = int(f.get("size", 0) or 0)
        d = os.path.dirname(dest)
        if d:
            os.makedirs(d, exist_ok=True)
        written = 0
        with open(dest, "wb") as out:
            for item in self.pieces_of(f):
                off, length, h = int(item[0]), int(item[1]), item[2]
                p = self.blob_path(h)
                if not os.path.exists(p):
                    raise KeyError(f"缺失数据块 {h}")
                with open(p, "rb") as src:
                    data = unpack_piece(src.read())
                out.seek(off)
                out.write(data)
                written += len(data)
            if size and out.tell() < size:
                out.truncate(size)
        return written

    # ------------------------------------------------------------ 审计
    def add_audit(self, actor: str, action: str, target: str = "", detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit(ts,actor,action,target,detail) VALUES(?,?,?,?,?)",
                (now_ts(), actor or "", action, target or "", detail or ""))
            self._conn.commit()

    def list_audit(self, limit: int = 200) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,actor,action,target,detail FROM audit ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [{"ts": r[0], "actor": r[1], "action": r[2],
                 "target": r[3], "detail": r[4]} for r in rows]

    # ------------------------------------------------------------ 回收站
    def list_trash(self) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name,display,instance,deleted_at,deleted_by,version_count,total_size"
                " FROM trash ORDER BY deleted_at DESC").fetchall()
        out = []
        for r in rows:
            out.append({"name": r[0], "display": r[1], "instance": r[2],
                        "deleted_at": r[3], "deleted_by": r[4],
                        "version_count": r[5], "total_size": r[6]})
        return out

    def restore_trash(self, name: str, actor: str = "") -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT name,display,instance,created_at FROM trash WHERE name=?", (name,)).fetchone()
        if not row:
            return False
        src = os.path.join(self.trash_dir, name)
        dst = self._save_dir(name)
        if os.path.isdir(src):
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO saves(name,created_at,updated_at,instance,display)"
                " VALUES(?,?,?,?,?)",
                (row[0], row[3] or now_ts(), now_ts(), row[2] or "", row[1] or row[0]))
            self._conn.execute("DELETE FROM trash WHERE name=?", (name,))
            self._conn.commit()
        self.add_audit(actor, "restore_save", name, "从回收站恢复")
        return True

    def purge_trash(self, days: int = 7) -> int:
        """彻底删除回收站里超过 N 天的条目，返回删除条数。"""
        cutoff = now_ts() - float(days) * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM trash WHERE deleted_at<=?", (cutoff,)).fetchall()
        n = 0
        for (nm,) in rows:
            p = os.path.join(self.trash_dir, nm)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            with self._lock:
                self._conn.execute("DELETE FROM trash WHERE name=?", (nm,))
                self._conn.commit()
            n += 1
        if n:
            self.add_audit("", "purge_trash", f"{n} 项", f"超过 {days} 天")
        return n

    def delete_save(self, name: str, actor: str = "", hard: bool = False) -> bool:
        try:
            return self._delete_save_impl(name, actor=actor, hard=hard)
        except Exception:
            return False

    def _delete_save_impl(self, name: str, actor: str = "", hard: bool = False) -> bool:
        if hard:
            for v in self.list_versions(name):
                self.delete_version(name, v["id"])
            shutil.rmtree(self._save_dir(name), ignore_errors=True)
            with self._lock:
                self._conn.execute("DELETE FROM saves WHERE name=?", (name,))
                self._conn.commit()
            self.gc()
            self.add_audit(actor, "purge_save", name, "彻底删除")
            return True
        # 软删除：整个存档搬到回收站，管理员误删可恢复
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at, instance, display FROM saves WHERE name=?", (name,)).fetchone()
        if not row:
            return False
        vers = self.list_versions(name)
        total = sum(int(v.get("total_size", 0) or 0) for v in vers)
        src = self._save_dir(name)
        dst = os.path.join(self.trash_dir, name)
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        if os.path.isdir(src):
            os.makedirs(self.trash_dir, exist_ok=True)
            try:
                os.replace(src, dst)
            except OSError:
                shutil.rmtree(src, ignore_errors=True)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO trash(name,display,instance,deleted_at,deleted_by,"
                "created_at,version_count,total_size) VALUES(?,?,?,?,?,?,?,?)",
                (name, row[2] or name, row[1] or "", now_ts(), actor,
                 row[0] or now_ts(), len(vers), total))
            self._conn.execute("DELETE FROM saves WHERE name=?", (name,))
            self._conn.commit()
        self.add_audit(actor, "delete_save", name, f"{len(vers)} 个版本 · 已进入回收站")
        return True

    def _resolve_target(self, plain: str, inst: str) -> str:
        """确定本次上传落到哪条记录，返回云端存档的唯一键。

        - 记录不存在            -> 用 plain（老存档键不变，URL 与已有数据都不受影响）
        - 已存在且归属相同       -> 复用
        - 已存在但未归类(instance 空) -> 归到本次的整合包（老存档自动补齐归属）
        - 已存在但归属是别的整合包 -> 这是两个不同的存档，另建一条
          「名字 (整合包名)」，原记录保持不动，两边都不会丢。
        """
        row = self._conn.execute(
            "SELECT instance FROM saves WHERE name=?", (plain,)).fetchone()
        if not row:
            return plain
        have = (row["instance"] or "").strip()
        if have == inst or not inst:
            return plain
        if not have:
            # 未分类 -> 本次带上了整合包名，直接归属过去
            return plain
        # 归属冲突：另建一条独立记录
        qual = normalize_save_name(f"{plain} ({inst})")
        row2 = self._conn.execute(
            "SELECT 1 FROM saves WHERE name=?", (qual,)).fetchone()
        if not row2:
            self._conn.execute(
                "INSERT INTO saves(name, created_at, updated_at, instance, display)"
                " VALUES(?,?,?,?,?)",
                (qual, ts_now(), ts_now(), inst, plain))
            self._conn.commit()
        return qual

    def set_instance(self, name: str, instance: str) -> bool:
        """手动指定云端存档所属整合包（用于老存档归类）。

        归类会把该存档的归属整体改到目标整合包下（连同它的版本记录），
        老存档从「未分类」归入某个整合包后，列表里就不再重复出现。
        """
        name = normalize_save_name(name)
        target = (instance or "").strip()
        with self._lock:
            row = self._conn.execute(
                "SELECT instance, created_at, updated_at FROM saves WHERE name=? LIMIT 1",
                (name,)).fetchone()
            if not row:
                return False
            src = row["instance"] or ""
            if src == target:
                return True
            # 目标位置若已有同名存档（另一个整合包的同名世界），
            # 直接移动会造成冲突，这里拒绝并让调用方提示。
            clash = self._conn.execute(
                "SELECT 1 FROM saves WHERE instance=? AND name=?", (target, name)).fetchone()
            if clash:
                return False
            self._conn.execute(
                "UPDATE saves SET instance=?, created_at=?, updated_at=? WHERE instance=? AND name=?",
                (target, row["created_at"], row["updated_at"], src, name))
            self._conn.execute(
                "UPDATE versions SET save_name=? WHERE save_name=?", (name, name))
            # 版本归属于 (instance,name)，这里把旧归属下的版本改挂到新归属
            self._conn.commit()
            return True

    def rename_save(self, name: str, new_name: str) -> bool:
        new_name = normalize_save_name(new_name)
        if not self.list_versions(name):
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at, instance, display FROM saves WHERE name=?", (name,)).fetchone()
            self._conn.execute("UPDATE versions SET save_name=? WHERE save_name=?", (new_name, name))
            self._conn.execute("DELETE FROM saves WHERE name=?", (name,))
            self._conn.execute(
                "INSERT OR REPLACE INTO saves(name, created_at, updated_at, instance, display)"
                " VALUES(?,?,?,?,?)",
                (new_name, (row["created_at"] if row else now_ts()), now_ts(),
                 (row["instance"] if row else "") or "",
                 (row["display"] if row else "") or new_name),
            )
            self._conn.commit()
        old_dir = self._save_dir(name)
        new_dir = self._save_dir(new_name)
        if os.path.isdir(old_dir):
            for f in os.listdir(old_dir):
                os.replace(os.path.join(old_dir, f), os.path.join(new_dir, f))
            shutil.rmtree(old_dir, ignore_errors=True)
        return True

    def _prune(self, name: str) -> None:
        """按 KEEP_VERSIONS 保留最近 N 个版本，超出的最旧版本自动清理。"""
        if self.keep_versions <= 0:
            return
        vers = self.list_versions(name)
        while len(vers) > self.keep_versions:
            oldest = vers.pop(0)
            self.delete_version(name, oldest["id"])

    # ------------------------------------------------------------ GC

    def gc(self) -> dict:
        """删除没有任何版本引用的 blob。"""
        removed, freed = 0, 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT hash, size FROM blobs WHERE hash NOT IN (SELECT hash FROM blob_ref)"
            ).fetchall()
            for r in rows:
                p = self.blob_path(r["hash"])
                if os.path.exists(p):
                    try:
                        freed += os.path.getsize(p)
                        os.remove(p)
                        removed += 1
                    except OSError:
                        pass
            self._conn.execute(
                "DELETE FROM blobs WHERE hash NOT IN (SELECT hash FROM blob_ref)"
            )
            self._conn.commit()
        # 清理空的 shard 目录
        for root, dirs, files in os.walk(self.blob_dir, topdown=False):
            if not files and not dirs and root != self.blob_dir:
                try:
                    os.rmdir(root)
                except OSError:
                    pass
        return {"removed_blobs": removed, "freed_bytes": freed}

    # ------------------------------------------------------------ 统计

    def stats(self) -> dict:
        with self._lock:
            saves = self._conn.execute("SELECT COUNT(*) c FROM saves").fetchone()["c"]
            vers = self._conn.execute("SELECT COUNT(*) c FROM versions").fetchone()["c"]
            blobs = self._conn.execute("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM blobs").fetchone()
        disk = 0
        for root, _dirs, files in os.walk(self.blob_dir):
            for f in files:
                try:
                    disk += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return {
            "save_count": saves,
            "version_count": vers,
            "blob_count": blobs["c"],
            "blob_logical_size": blobs["s"],
            "disk_usage": disk,
        }

    # ------------------------------------------------------------ 打包 zip

    def make_zip(self, name: str, version_id: int) -> str:
        m = self.load_manifest(name, version_id)
        if not m:
            raise KeyError("版本不存在")
        fd, tmp = tempfile.mkstemp(prefix=f"mcsync-{name}-v{version_id}-", suffix=".zip", dir=self.tmp_dir)
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for rel, f in m.get("files", {}).items():
                    if f.get("type") == "dir":
                        z.writestr(zipfile.ZipInfo(rel + "/"), b"")
                        continue
                    info = zipfile.ZipInfo(rel, date_time=time.localtime(f.get("mtime", now_ts()))[:6])
                    info.compress_type = zipfile.ZIP_STORED
                    # 先按 offset 还原成完整文件再入包：region 分片不能顺序拼接
                    tmpf = os.path.join(self.tmp_dir, f"z-{uuid.uuid4().hex}.bin")
                    try:
                        self.assemble_file(f, tmpf)
                        with open(tmpf, "rb") as src, z.open(info, "w") as dst:
                            shutil.copyfileobj(src, dst, 1024 * 1024)
                    finally:
                        if os.path.exists(tmpf):
                            try:
                                os.remove(tmpf)
                            except OSError:
                                pass
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return tmp
