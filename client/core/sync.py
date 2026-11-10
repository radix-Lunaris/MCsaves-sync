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
同步引擎：本地扫描 → 差异比对 → 冲突处理 → 分块传输 → 组装 → 提交版本
"""
from __future__ import annotations

import os
import sys
from core.protocol import piece_ranges, piece_size_for, unpack_piece
import shutil
import time
from typing import Callable, Dict, List, Optional, Tuple

from core.api import Api, MCSyncError
from core.config import cfg
from core.hasher import chunk_hashes, cheap_fingerprint, file_hash
from core.hasher import hash_pieces  # noqa: E402
from core.models import SaveState
from core.protocol import human_size, make_ignore, norm_rel, safe_join
from core.transfer import CancelToken, ChunkTransfer, Progress, assemble_file

STATE_MARKER = ".mcsync"


class SyncEngine:
    def __init__(
        self,
        api: Api,
        progress: Optional[Progress] = None,
        cancel: Optional[CancelToken] = None,
        log: Callable[[str], None] = print,
    ):
        self.api = api
        self.progress = progress or Progress()
        self.cancel = cancel or CancelToken()
        self.log = log
        self.chunk_size = int(cfg.chunk_size)
        self.transfer = ChunkTransfer(
            api=api,
            concurrency=int(cfg.concurrency),
            chunk_size=self.chunk_size,
            progress=self.progress,
            cancel=self.cancel,
            log=log,
        )

    # ------------------------------------------------------------ 缓存
    @property
    def cache_dir(self) -> str:
        d = os.path.join(cfg.dir, "blobcache")
        os.makedirs(d, exist_ok=True)
        return d

    def clean_cache(self, limit_mb: int = 2048) -> int:
        d = self.cache_dir
        try:
            files = [(os.path.getsize(os.path.join(d, f)), os.path.getmtime(os.path.join(d, f)), os.path.join(d, f))
                     for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))]
        except OSError:
            return 0
        total = sum(s for s, _, _ in files)
        limit = limit_mb * 1024 * 1024
        freed = 0
        if total > limit:
            files.sort(key=lambda x: x[1])
            for s, _, p in files:
                if total <= limit:
                    break
                try:
                    os.remove(p)
                    total -= s
                    freed += s
                except OSError:
                    pass
        return freed

    # ------------------------------------------------------------ 本地扫描
    def scan_local(self, save_dir: str, known: Optional[Dict[str, dict]] = None,
                   full_hash: bool = False) -> Dict[str, dict]:
        """
        返回 {rel: {size, mtime, hash}}
        若 known 中存在相同路径且 (size, mtime) 未变，直接复用已知哈希，避免重复读盘。
        """
        known = known or {}
        matcher = make_ignore(cfg.get("ignore_rules", []), bool(cfg.get("core_only", False)))
        out: Dict[str, dict] = {}
        reused = 0
        hashed = 0
        items: List[Tuple[str, str, int, float]] = []
        self.last_dirs: List[str] = []

        for root, dirs, files in os.walk(save_dir):
            rel_root = os.path.relpath(root, save_dir)
            rel_root = "" if rel_root in (".", "./") else norm_rel(rel_root)
            keep = []
            for d in dirs:
                rel = (rel_root + "/" + d) if rel_root else d
                if matcher.match(rel):
                    continue
                keep.append(d)
                # 记录目录，供上传时空目录一并同步
                self.last_dirs.append(rel)
            dirs[:] = keep
            for f in files:
                rel = (rel_root + "/" + f) if rel_root else f
                if matcher.match(rel):
                    continue
                fp = os.path.join(root, f)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                items.append((rel, fp, st.st_size, st.st_mtime))

        total = len(items)
        self.progress.set(stage="扫描本地文件", total_items=total, done_items=0,
                          total_bytes=sum(i[2] for i in items), done_bytes=0)
        for idx, (rel, fp, size, mtime) in enumerate(items):
            self.cancel.raise_if()
            h = ""
            if not full_hash:
                k = known.get(rel)
                if k and k.get("hash") and cheap_fingerprint(k.get("size", -1), k.get("mtime", -1)) == cheap_fingerprint(size, mtime):
                    h = k["hash"]
                    reused += 1
            if not h:
                h = file_hash(fp)
                hashed += 1
            out[rel] = {"size": size, "mtime": mtime, "hash": h}
            if idx % 20 == 0:
                self.progress.set(done_items=idx + 1, file=rel)
        self.progress.set(done_items=total, file="")
        self.log(f"扫描完成：{total} 个文件（复用哈希 {reused}，重新计算 {hashed}）")
        return out

    # ------------------------------------------------------------ 备份
    def backup(self, save_dir: str, tag: str = "") -> str:
        root = cfg.get("backup_dir", "") or os.path.join(os.path.dirname(os.path.normpath(save_dir)), "_MCSyncBackups")
        os.makedirs(root, exist_ok=True)
        name = os.path.basename(os.path.normpath(save_dir))
        dst = os.path.join(root, f"{name}-{time.strftime('%Y%m%d-%H%M%S')}{'-' + tag if tag else ''}")
        os.makedirs(dst, exist_ok=True)
        linked, copied = 0, 0
        for r, dirs, files in os.walk(save_dir):
            rel = os.path.relpath(r, save_dir)
            rel = "" if rel == "." else rel
            if STATE_MARKER in dirs:
                dirs.remove(STATE_MARKER)
            target_dir = os.path.join(dst, rel) if rel else dst
            os.makedirs(target_dir, exist_ok=True)
            for f in files:
                s = os.path.join(r, f)
                d = os.path.join(target_dir, f)
                try:
                    os.link(s, d)
                    linked += 1
                except Exception:
                    try:
                        shutil.copy2(s, d)
                        copied += 1
                    except OSError:
                        pass
        self._prune_backups(root, name)
        self.log(f"已备份到 {dst}（硬链接 {linked}，复制 {copied}）")
        return dst

    def _prune_backups(self, root: str, prefix: str) -> None:
        keep = int(cfg.get("keep_backups", 5) or 0)
        if keep <= 0:
            return
        try:
            subs = [os.path.join(root, d) for d in os.listdir(root)
                    if os.path.isdir(os.path.join(root, d)) and d.startswith(prefix + "-")]
        except OSError:
            return
        subs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in subs[keep:]:
            shutil.rmtree(p, ignore_errors=True)

    def _auto_fix_playerdata(self, save_dir: str, stage: str):
        """上传前 / 下载后自动解除共享玩家数据。

        幂等、失败不影响同步主流程。房主数据会被归档成独立 .dat，
        房主按自己 UUID 仍读得回来，所以「上传时一律处理」是无损的。
        """
        try:
            from core import playerdata as pd
            r = pd.fix(save_dir, str(cfg.get("my_uuid", "") or ""))
        except Exception as e:
            self.log(f"{stage}共享玩家数据处理跳过：{e}")
            return None
        if r.changed:
            detail = "；".join(r.messages[:1])
            self.log(f"{stage}已自动摘除房主玩家数据：{r.summary()}"
                     + (f"（{detail}）" if detail else ""))
        elif r.status == "running":
            self.log(f"{stage}共享玩家数据未处理：检测到游戏正在运行")
        elif r.status == "error":
            self.log(f"{stage}共享玩家数据处理失败（存档本身不受影响）："
                     f"{'；'.join(r.messages[:1])}")
        return r

    # ------------------------------------------------------------ 上传
    def upload(self, save_dir: str, cloud_name: str, note: str = "",
               resolver: Optional[Callable[[List[dict]], Dict[str, str]]] = None,
               full_hash: bool = False, instance: str = "", kind: str = "save") -> dict:
        t0 = time.time()
        self.progress.set_stage("准备上传", file="", error="")
        self._check_not_running(save_dir, "上传")
        self._note_resume(save_dir, cloud_name)
        # 云端版本天然「共享友好」：上传前就把内嵌的房主玩家数据归档并摘除。
        # 这样任何人下载后直接是自己的角色，不需要在客户端手动点修复。
        # 房主数据被归档成 playerdata/<UUID>.dat，一起上传，房主照样读得回。
        if kind == "save" and cfg.get("auto_fix_playerdata", True) and os.path.isdir(save_dir):
            self._auto_fix_playerdata(save_dir, "上传前")
        state = SaveState.load(save_dir)
        files = self.scan_local(save_dir, state.files, full_hash=full_hash)
        if not files:
            raise MCSyncError("存档为空或全部被忽略规则过滤")

        self.progress.set_stage("比对云端差异")
        metas = [{"path": k, "size": v["size"], "mtime": v["mtime"], "hash": v["hash"]} for k, v in files.items()]
        plan = self.api.plan(cloud_name, state.last_version, metas)

        cloud_files: Dict[str, dict] = {}
        if plan.get("latest_version"):
            try:
                cloud_files = self.api.manifest(cloud_name, plan["latest_version"]).get("files", {}) or {}
            except MCSyncError:
                cloud_files = {}

        # ---- 冲突处理
        conflicts = plan.get("conflicts", []) or []
        decisions: Dict[str, str] = {}
        if conflicts:
            self.log(f"检测到 {len(conflicts)} 个冲突文件")
            if resolver:
                decisions = resolver(conflicts) or {}
            for c in conflicts:
                decisions.setdefault(c["path"], "local")
        cloud_keep = {p for p, d in decisions.items() if d == "cloud"}

        # ---- 决定哪些文件需要真正算块并上传
        # 冲突文件中选择「用本地 / 都保留」的，等同于需要上传本地版本
        need = set(plan.get("need_upload", []))
        for p, d in decisions.items():
            if d in ("local", "both"):
                need.add(p)
        need -= cloud_keep
        for rel in plan.get("instant", []):
            # 改成分片后不再有「整文件即一块」的走法：
            # 清单里的 pieces 必须真的在云端存在，否则服务端 409。
            # 所以这些文件也要算分片（已在云端的会被 blob_check 跳过，不会重复传）。
            if files.get(rel, {}).get("size", 0) > 0:
                need.add(rel)

        self.progress.set_stage("计算数据块哈希")
        piece_map: Dict[str, list] = {}
        piece_src: Dict[str, dict] = {}
        todo = sorted(need)
        for i, rel in enumerate(todo):
            self.cancel.raise_if()
            fp = safe_join(save_dir, rel)
            size = int(files[rel]["size"])
            try:
                ranges = piece_ranges(fp, size, self.chunk_size)
                # 第二道防膨胀闸门：分片总长必须与文件大小一致。
                # 曾经因 region 分组算法缺陷导致 400MB 存档膨胀到 16GB，
                # 这里兜底——宁可退回最朴素的定长切分，也不允许带病分片上传。
                if ranges and sum(n for _, n in ranges) > size:
                    ranges = []
                    off = 0
                    cs = self.chunk_size or piece_size_for(size)
                    while off < size:
                        n = min(cs, size - off)
                        ranges.append((off, n))
                        off += n
                hashes = hash_pieces(fp, ranges)
            except OSError as e:
                raise MCSyncError(f"读取文件失败 {rel}：{e}")
            plist = [[int(o), int(n), h] for (o, n), h in zip(ranges, hashes)]
            piece_map[rel] = plist
            for (o, n), h in zip(ranges, hashes):
                # 内容相同的分片只保留第一个来源，天然去重
                if h not in piece_src:
                    piece_src[h] = {"path": fp, "offset": int(o), "size": int(n)}
            if i % 5 == 0:
                self.progress.set(total_items=len(todo), done_items=i + 1, file=rel)
        self.progress.set(done_items=len(todo), file="")

        # ---- 上传缺失块
        all_chunks: List[str] = []
        seen = set()
        for rel in todo:
            for _o, _n, h in piece_map.get(rel, []):
                if h not in seen:
                    seen.add(h)
                    all_chunks.append(h)

        self.progress.set_stage("检查云端已有数据块")
        missing = self.api.blob_check(all_chunks) if all_chunks else []
        initial_missing = len(missing)
        self.log(f"需上传数据块 {len(missing)}/{len(all_chunks)}（复用 {len(all_chunks) - len(missing)}）")

        uploaded_bytes = 0
        # 上传 → 复查 → 补传，最多 3 轮。
        # 网络抖动 / 代理 503 可能让个别块静默失败，提交前必须确保云端齐备，
        # 否则服务端会返回 409 missing_chunks。
        for attempt in range(1, 4):
            missing = self.api.blob_check(all_chunks) if all_chunks else []
            if not missing:
                break
            if attempt > 1:
                self.log(f"第 {attempt} 轮补传：仍有 {len(missing)} 个数据块缺失")
            tasks = []
            for h in missing:
                src = piece_src.get(h)
                if not src:
                    continue
                tasks.append({"hash": h, "path": src["path"],
                              "offset": src["offset"], "size": src["size"]})
            if not tasks:
                break
            self.progress.set_stage("上传数据块")
            uploaded_bytes += self.transfer.upload_chunks(tasks)

        # 提交前最终校验：仍缺失就明确报错，不让服务端抛难懂的 409
        final_missing = self.api.blob_check(all_chunks) if all_chunks else []
        if final_missing:
            raise MCSyncError(
                f"有 {len(final_missing)} 个数据块上传失败（已重试 3 轮），请检查网络后重试。"
                f"首个缺失块：{final_missing[0][:12]}"
            )

        # ---- 组装最终清单
        final: Dict[str, dict] = {}
        for rel, f in files.items():
            cf = cloud_files.get(rel)
            if rel in cloud_keep and cf:
                final[rel] = cf
                continue
            # 未改动的文件直接复用云端条目。
            # 新版清单的 chunks 恒为空、块信息在 pieces 里，
            # 只判 chunks 会让这些文件被误判为「没有块」而触发 409。
            if cf and cf.get("hash") == f["hash"] and (
                    cf.get("pieces") or cf.get("chunks") or int(f["size"]) == 0):
                final[rel] = cf
                continue
            # 空文件不产生任何分片（旧版会退化成 [整文件哈希]，
            # 而空内容哈希从未上传，导致服务端 409 missing_chunks）
            final[rel] = {
                "path": rel, "size": f["size"], "mtime": f["mtime"],
                "hash": f["hash"], "chunks": [],
                "pieces": piece_map.get(rel, []),
            }

        # 空目录也进清单：MC 的 datapacks/、data/ 等依赖目录结构存在，
        # 只同步文件会让这些空目录在对方机器上消失。
        for d in getattr(self, "last_dirs", []) or []:
            if d and d not in final:
                final[d] = {"path": d, "size": 0, "mtime": 0, "hash": "",
                            "chunks": [], "pieces": [], "type": "dir"}

        delete = list(plan.get("to_delete", [])) if cfg.get("delete_remote_on_upload", True) else []
        self.progress.set_stage("提交新版本")
        res = self.api.commit(cloud_name, state.last_version, list(final.values()), delete,
                              note=note, instance=instance, kind=kind)
        version = res.get("version", {})

        state.update(final, int(version.get("id", 0)), cfg.server_url, cloud_name)
        self._clear_resume(save_dir)
        elapsed = time.time() - t0
        # 版本 id 只用于接口定位，不对用户展示。
        # 面向用户一律用「上传时间」—— v1/v2/v3 是单个存档内部的序号，
        # 跨存档/跨整合包并排看时毫无意义，只会让人误以为是全局版本。
        vtime = (version.get("created_at_str") or
                 time.strftime("%Y-%m-%d %H:%M", time.localtime()))
        result = {
            "ok": True,
            "direction": "upload",
            "cloud_name": cloud_name,
            "version": version.get("id", 0),
            "version_time": vtime,
            "file_count": len(final),
            "uploaded_bytes": uploaded_bytes,
            "reused_chunks": len(all_chunks) - initial_missing,
            "deleted_remote": len(delete),
            "conflicts": len(conflicts),
            "elapsed": elapsed,
        }
        self.log(
            f"上传完成（{vtime}）：{len(final)} 个文件，"
            f"实际上传 {human_size(uploaded_bytes)}，耗时 {elapsed:.1f}s"
        )
        return result

    def _resume_marker(self, save_dir: str) -> str:
        d = os.path.join(save_dir, ".mcsync")
        return os.path.join(d, "pending_upload.json")

    def _note_resume(self, save_dir: str, cloud_name: str) -> None:
        """记录"正在上传"，下次启动若发现残留就说明上次没传完。

        分片是内容寻址的、已传的在服务端，所以重新上传天然续传：
        缺失的分片才需要再传。这里只是把这件事告诉用户，
        免得他以为要整个重来。
        """
        mk = self._resume_marker(save_dir)
        try:
            if os.path.exists(mk):
                self.log("检测到上次上传未完成，将自动续传（已传过的分片不会重复上传）")
            os.makedirs(os.path.dirname(mk), exist_ok=True)
            import json as _j
            with open(mk, "w", encoding="utf-8") as f:
                _j.dump({"cloud_name": cloud_name, "ts": time.time()}, f)
        except OSError:
            pass

    def _clear_resume(self, save_dir: str) -> None:
        try:
            mk = self._resume_marker(save_dir)
            if os.path.exists(mk):
                os.remove(mk)
        except OSError:
            pass

    @staticmethod
    def is_running(save_dir: str) -> bool:
        """判断游戏是否正在运行。

        MC 打开存档时会独占 session.lock。此时 level.dat / .mca 正在被写，
        同步会拿到不一致快照，装回去可能坏档。

        Windows 上真实去抢一次锁（拿不到就是被占用，最可靠）；
        其他系统退化为"文件最近被写过且非空"的判断。
        """
        lock = os.path.join(save_dir, "session.lock")
        try:
            st = os.stat(lock)
        except OSError:
            return False
        if sys.platform == "win32":
            try:
                import msvcrt
                with open(lock, "r+b") as f:
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                        return False          # 能加锁说明没人占用
                    except OSError:
                        return True           # 抢不到 = MC 正开着
            except Exception:
                return True                   # 打不开 = 被独占
        # 非 Windows（Linux/macOS 只跑服务端，不做同步）无法可靠判定：
        # 靠"最近被写过"会把刚复制过来的存档误判成正在运行，反而挡住正常同步。
        # 客户端是 Windows，那里用抢占锁的方式判断，结果准确。
        return False

    def _check_not_running(self, save_dir: str, what: str) -> None:
        if cfg.get("allow_sync_while_running", False):
            return
        if self.is_running(save_dir):
            raise MCSyncError(
                f"检测到游戏可能正在运行（{what}）。\n"
                f"运行中同步会读到正在写入的半截数据，装回去可能坏档。\n"
                f"请先关闭游戏；若确认没开，可在设置里允许运行同步。")

    # ------------------------------------------------------------ 下载
    def download(self, save_dir: str, cloud_name: str, vid: Optional[int] = None,
                 resolver: Optional[Callable[[List[dict]], Dict[str, str]]] = None,
                 to_new_dir: bool = False,
                 auto_fix_playerdata: Optional[bool] = None) -> dict:
        t0 = time.time()
        self.progress.set_stage("获取云端版本", file="", error="")
        if os.path.isdir(save_dir):
            self._check_not_running(save_dir, "下载")
        vers = self.api.versions(cloud_name)
        if not vers:
            raise MCSyncError(f"云端不存在存档「{cloud_name}」")
        vid = int(vid or vers[-1]["id"])
        m = self.api.manifest(cloud_name, vid)
        cloud_files: Dict[str, dict] = m.get("files", {}) or {}
        if not cloud_files:
            raise MCSyncError("该版本没有文件")

        os.makedirs(save_dir, exist_ok=True)
        state = SaveState.load(save_dir)
        base = state.files
        local = self.scan_local(save_dir, base) if os.path.isdir(save_dir) else {}

        need: List[str] = []
        keep_local: List[str] = []
        conflicts: List[dict] = []
        unchanged = 0
        for rel, cf in cloud_files.items():
            if cf.get("type") == "dir":
                try:
                    os.makedirs(safe_join(save_dir, rel), exist_ok=True)
                except OSError:
                    pass
                continue
            lf = local.get(rel)
            if lf is None:
                need.append(rel)
                continue
            if lf.get("hash") == cf.get("hash"):
                unchanged += 1
                continue
            bh = (base.get(rel) or {}).get("hash")
            if bh == lf.get("hash"):
                need.append(rel)                      # 本地未改，云端更新 -> 覆盖
            elif bh == cf.get("hash"):
                keep_local.append(rel)                # 云端未改，本地改过 -> 保留本地
            else:
                conflicts.append({"path": rel, "local_hash": lf.get("hash"), "cloud_hash": cf.get("hash")})

        decisions: Dict[str, str] = {}
        if conflicts:
            self.log(f"检测到 {len(conflicts)} 个冲突文件")
            if resolver:
                decisions = resolver(conflicts) or {}
            for c in conflicts:
                decisions.setdefault(c["path"], "local")

        both_save: List[str] = []
        for rel, d in decisions.items():
            if d == "cloud":
                need.append(rel)
            elif d == "both":
                need.append(rel)
                both_save.append(rel)
            else:
                keep_local.append(rel)

        # 云端已删除、本地仍存在（且上次同步时存在）的文件
        to_delete_local = []
        if cfg.get("delete_local_on_download", True):
            for rel in local:
                if rel not in cloud_files and rel in base:
                    to_delete_local.append(rel)

        backup_path = ""
        if (need or to_delete_local) and cfg.get("backup_before_sync", True) and os.path.isdir(save_dir):
            self.progress.set_stage("备份本地存档")
            backup_path = self.backup(save_dir, "下载前")

        if both_save:
            self.progress.set_stage("保存冲突副本")
            bdir = os.path.join(save_dir, "_冲突副本", time.strftime("%Y%m%d-%H%M%S"))
            for rel in both_save:
                src = safe_join(save_dir, rel)
                dst = os.path.join(bdir, rel)
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                except OSError:
                    pass

        # ---- 下载块
        all_chunks: List[str] = []
        seen = set()
        for rel in need:
            cf0 = cloud_files[rel]
            if cf0.get("type") == "dir":
                continue
            for _o, _n, h in self._pieces_of(cf0):
                if h and h not in seen:
                    seen.add(h)
                    all_chunks.append(h)
        self.progress.set_stage("下载数据块", total_items=len(all_chunks), done_items=0)
        cache = self.transfer.download_chunks(all_chunks, self.cache_dir) if all_chunks else {}
        new_dl = getattr(self.transfer, "last_new_downloads", 0)
        self.log(f"数据块就绪 {len(all_chunks)} 个（本次真正下载 {new_dl}，本地缓存复用 {len(all_chunks) - new_dl}）")

        # ---- 组装
        self.progress.set_stage("写入本地文件", total_items=len(need), done_items=0, total_bytes=0, done_bytes=0)
        written = 0
        for i, rel in enumerate(need):
            self.cancel.raise_if()
            cf = cloud_files[rel]
            dest = safe_join(save_dir, rel)
            try:
                if cf.get("type") == "dir":
                    os.makedirs(dest, exist_ok=True)
                else:
                    written += self.assemble_pieces(cf, cache, dest)
                    try:
                        mt = cf.get("mtime", time.time())
                        os.utime(dest, (mt, mt))
                    except OSError:
                        pass
            except MCSyncError:
                raise
            except OSError as e:
                raise MCSyncError(f"写入失败 {rel}：{e}")
            if i % 5 == 0 or i == len(need) - 1:
                self.progress.set(done_items=i + 1, file=rel)

        for rel in to_delete_local:
            try:
                os.remove(safe_join(save_dir, rel))
            except OSError:
                pass

        # ---- 更新状态：以云端清单为准
        new_state_files = {rel: {"size": cf.get("size", 0), "mtime": cf.get("mtime", 0), "hash": cf.get("hash", ""),
                                 "chunks": cf.get("chunks", []), "pieces": cf.get("pieces", [])}
                           for rel, cf in cloud_files.items()}
        state.update(new_state_files, vid, cfg.server_url, cloud_name)

        # ---- 兜底：历史云端版本若仍带内嵌房主数据，下载后自动修一次 ----
        # 主路径已改在「上传前」处理，正常情况下这里走 already_clean。
        pd_result = None
        want_fix = (cfg.get("auto_fix_playerdata", True)
                    if auto_fix_playerdata is None else auto_fix_playerdata)
        if want_fix and os.path.isdir(save_dir):
            pd_result = self._auto_fix_playerdata(save_dir, "下载后")
            if pd_result and pd_result.changed:
                # 只更新「云端清单里本来就有」的文件哈希。
                # 新生成的归档 .dat 不写进 state：否则它不在云端清单里，
                # 下次下载会被当成「云端已删除」而误删；不写进去则会在
                # 下次上传时被 scan_local 自然发现并同步上去。
                for rel in pd_result.files_changed:
                    if rel not in new_state_files:
                        continue
                    fp = safe_join(save_dir, rel)
                    if not os.path.isfile(fp):
                        continue
                    stt = os.stat(fp)
                    new_state_files[rel] = {
                        "size": stt.st_size, "mtime": stt.st_mtime,
                        "hash": file_hash(fp), "chunks": [], "pieces": []}
                state.update(new_state_files, vid, cfg.server_url, cloud_name)
        self.clean_cache()

        # 用该版本的上传时间作为面向用户的标识，不再显示 vN
        _vt = ""
        for _v in (vers or []):
            if int(_v.get("id", 0)) == int(vid):
                _vt = _v.get("created_at_str") or ""
                break
        vtime = _vt or time.strftime("%Y-%m-%d %H:%M", time.localtime())
        elapsed = time.time() - t0
        result = {
            "ok": True,
            "direction": "download",
            "cloud_name": cloud_name,
            "version": vid,
            "version_time": vtime,
            "file_count": len(cloud_files),
            "downloaded_files": len(need),
            "written_bytes": written,
            "unchanged": unchanged,
            "kept_local": len(keep_local),
            "deleted_local": len(to_delete_local),
            "conflicts": len(conflicts),
            "backup": backup_path,
            "playerdata": ({"status": pd_result.status,
                            "summary": pd_result.summary(),
                            "owner": pd_result.owner_uuid,
                            "messages": pd_result.messages} if pd_result else None),
            "elapsed": elapsed,
        }
        self.log(
            f"下载完成（{vtime}）：写入 {len(need)} 个文件（{human_size(written)}），"
            f"跳过未变化 {unchanged}，耗时 {elapsed:.1f}s"
        )
        return result

    @staticmethod
    def _pieces_of(cf: dict) -> list:
        """取云端文件条目的分片列表，兼容旧版没有 pieces 的清单。"""
        ps = cf.get("pieces")
        if ps:
            return ps
        chunks = cf.get("chunks") or []
        # 旧清单只有顺序块哈希，按 4MiB 还原偏移
        out, off = [], 0
        size = int(cf.get("size", 0) or 0)
        for h in chunks:
            n = min(CHUNK_SIZE, size - off) if size else 0
            out.append([off, n, h])
            off += n
        return out

    def assemble_pieces(self, cf: dict, cache: Dict[str, str], dest: str) -> int:
        """把分片按各自的 offset 写回文件（旧清单退化为顺序拼接）。

        region 文件用的是「区块分组」分片，必须按 offset 落盘，
        中间的空洞补 0 —— 直接顺序拼接会错位，档就废了。
        """
        size = int(cf.get("size", 0) or 0)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        pieces = self._pieces_of(cf)
        if not pieces:
            open(dest, "wb").close()
            return 0
        written = 0
        with open(dest, "wb") as out:
            for off, length, h in pieces:
                src = cache.get(h)
                if not src:
                    raise MCSyncError(f"缺少数据块 {h[:12]}（文件 {cf.get('path','')}）")
                with open(src, "rb") as f:
                    data = unpack_piece(f.read())
                if len(data) != length:
                    # 长度对不上说明块内容有问题，宁可报错也不要写坏档
                    raise MCSyncError(
                        f"数据块长度不符 {h[:12]}：期望 {length} 实际 {len(data)}")
                out.seek(int(off))
                out.write(data)
                written += len(data)
            if size and out.tell() < size:
                out.truncate(size)
        return written

    # ------------------------------------------------------------ 云端回滚
    def restore_remote(self, cloud_name: str, vid: int) -> dict:
        r = self.api.restore(cloud_name, vid)
        _v = r.get("version") or {}
        _t = _v.get("created_at_str") or time.strftime("%Y-%m-%d %H:%M", time.localtime())
        self.log(f"云端已回滚，已生成 {_t} 的新快照")
        return r

