# -*- coding: utf-8 -*-
"""
「40MB 存档显示要下载 5.2G」回归测试。

事故复盘
────────
client/core/transfer.py 的 download_chunks() 旧实现：

    total_size_est = self.chunk_size * len(todo)     # 固定 4MiB × 分片数
    self.progress.set(total_bytes=旧值 + total_size_est,
                      total_items=旧值 + len(todo))

分片是自适应 / region 覆盖式切分的，一块常常只有 32KB~256KB。
一个 40MB 的存档若含 1300 个 region 小分片：
    4 MiB × 1300 = 5.2 GiB
进度条显示「要下载 5.2G」，且 done 只有 40MB → 永远停在 0.8%，
ETA 也按虚高分母算得离谱。而且 total_items 被累加成两倍。

修复
────
1. 用云端清单里每个分片的**真实长度**求和作为总量估计。
   分片有序、不重叠、总长恰好等于文件大小，所以 sum(sizes) == 存档大小。
2. 替换而非累加 total_bytes / total_items，并重置 done_bytes。
3. 服务端存的是压缩后 blob，实际字节更小 —— 每块下载完按实际值
   adjust_total 收敛，进度条能正常走满。
4. 下载前体检清单：历史版本若是旧版上传的膨胀分片，直接给人话提示并中止，
   否则不只是显示错，而是真的会拉下几十倍数据并写坏档。

用法： python3 tools/test_download_size.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "server"))

TMP = "/tmp/mcsync-download-size"
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["MCSYNC_CONFIG_DIR"] = os.path.join(TMP, "cfg")
os.makedirs(os.environ["MCSYNC_CONFIG_DIR"], exist_ok=True)

OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


def hsize(n: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}"
        n /= 1024.0


def file_hash(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    print("\n=== 下载总量显示事故 回归测试 ===\n")
    from core.config import cfg
    from core.protocol import human_size
    from core.sync import SyncEngine
    from core.transfer import ChunkTransfer, Progress
    from core.api import Api

    CHUNK = int(cfg.chunk_size)
    N = 1300
    PIECE = 32 * 1024                      # region 小分片典型大小

    # ---------------------------------------------------------------- 1
    print("[1] 复现旧公式：4MiB × 1300 分片 = 5.2GiB")
    old = CHUNK * N
    check("旧公式确实算出约 5.2 GB", 5.0e9 < old < 5.6e9,
          f"{hsize(old)}（chunk_size={hsize(CHUNK)}, 分片数={N}）")
    real = PIECE * N
    check("真实数据量只有约 40 MB", real < 50 * 1024 * 1024, hsize(real))
    check("虚高倍数 > 100 倍", old / real > 100, f"{old / real:.0f} 倍")

    # ---------------------------------------------------------------- 2
    print("\n[2] 新实现：按清单真实分片长度求和，且校准到实际下载量")

    class FakeApi:
        """模拟服务端：blob 是压缩后的，比解压后分片小得多。"""
        def __init__(self, blob_size: int = 4096):
            self.blob_size = blob_size
            self.calls = 0

        def get_blob_to_file(self, h, dest, chunk=1024 * 1024):
            self.calls += 1
            with open(dest, "wb") as f:
                f.write(b"z" * self.blob_size)
            return self.blob_size

    peaks = []
    finals = {}

    def on_progress(st):
        peaks.append(int(st.get("total_bytes", 0) or 0))
        finals.update(st)

    prog = Progress(cb=on_progress, interval=0)
    prog.cb = on_progress
    tr = ChunkTransfer(api=FakeApi(), concurrency=8, chunk_size=CHUNK,
                       progress=prog, log=lambda m: None)
    hashes = [f"{i:032x}" for i in range(N)]
    sizes = {h: PIECE for h in hashes}
    cache = os.path.join(TMP, "cache")
    got = tr.download_chunks(hashes, cache, sizes=sizes)

    check("全部块下载完成", len(got) == N, str(len(got)))
    check("total_bytes 峰值 ≈ 40MB 而非 5.2GB",
          max(peaks) <= real * 1.05,
          f"峰值 {hsize(max(peaks))}，真实 {hsize(real)}")
    check("峰值不再等于 4MiB × 分片数", max(peaks) != old, hsize(max(peaks)))
    check("total_items 未被累加成两倍",
          int(finals.get("total_items", 0)) == N, str(finals.get("total_items")))
    check("done_items 正确", int(finals.get("done_items", 0)) == N)
    actual_total = N * 4096
    check("total 已校准到实际传输量（压缩后）",
          abs(int(finals.get("total_bytes", 0)) - actual_total) <= actual_total * 0.02,
          f"{hsize(int(finals.get('total_bytes', 0)))} vs {hsize(actual_total)}")
    check("done_bytes 收敛到实际传输量",
          abs(int(finals.get("done_bytes", 0)) - actual_total) <= actual_total * 0.02,
          hsize(int(finals.get("done_bytes", 0))))
    check("进度条能走到 100%",
          int(finals.get("total_bytes", 1)) > 0 and
          int(finals.get("done_bytes", 0)) >= int(finals.get("total_bytes", 0)))

    # ---------------------------------------------------------------- 3
    print("\n[3] 缺失 size 信息时按项数显示，绝不编造总量")
    peaks2 = []
    prog2 = Progress(cb=lambda st: peaks2.append(int(st.get("total_bytes", 0) or 0)),
                     interval=0)
    cfg2 = os.path.join(TMP, "cache2")
    hashes2 = [f"b{i:031x}" for i in range(10)]
    tr2 = ChunkTransfer(api=FakeApi(), concurrency=4, chunk_size=CHUNK,
                        progress=prog2, log=lambda m: None)
    tr2.download_chunks(hashes2, cfg2)            # 不传 sizes（兼容旧调用）
    check("无 sizes 时 total_bytes 保持 0（UI 走项数分支）",
          max(peaks2) == 0, str(max(peaks2)))

    # ---------------------------------------------------------------- 4
    print("\n[4] 下载前清单体检：膨胀的历史版本直接拒绝")
    good = {
        "region/r.0.0.mca": {
            "size": 40 * 1024 * 1024,
            "pieces": [[i * PIECE, PIECE, f"h{i:031x}"]
                       for i in range(40 * 1024 * 1024 // PIECE)],
        },
        "level.dat": {"size": 0, "pieces": []},
        "data/": {"type": "dir", "size": 0, "pieces": []},
        "old.bin": {"size": 1024, "chunks": ["aa" * 16]},   # 旧清单，跳过
    }
    SyncEngine._validate_download_manifest(good, list(good))
    check("正常清单放行", True)

    bloated = {
        "region/r.0.0.mca": {
            "size": 40 * 1024 * 1024,
            # 旧版按区块索引分组：128 组彼此重叠，合计 96 倍
            "pieces": [[0, 40 * 1024 * 1024, "x" * 32] for _ in range(96)],
        },
    }
    try:
        SyncEngine._validate_download_manifest(bloated, ["region/r.0.0.mca"])
        check("膨胀清单被拒绝", False, "竟然放行了")
    except Exception as e:
        msg = str(e)
        check("膨胀清单被拒绝", True)
        check("错误信息给出实际大小与清单合计",
              "40.0 MB" in msg or "40 MB" in msg, msg[:120])
        check("错误信息给出可操作建议", "重新上传" in msg or "删除" in msg)

    # ---------------------------------------------------------------- 5
    print("\n[5] 真实 SyncEngine.download 全流程（内存服务端，1300 个分片）")
    from core.protocol import pack_piece

    class MemoryApi:
        """只实现 download() 会用到的三个接口，blob 走压缩格式。

        用内存服务端而不是起 uvicorn：测试环境里第三方依赖可能缺失，
        而且这里要验证的正是「进度条分母」和「清单体检」，与网络层无关。
        """
        def __init__(self, files, blobs):
            self.files, self.blobs = files, blobs
            self.downloaded = 0

        def versions(self, name):
            return [{"id": 1, "created_at_str": "2026-11-17 00:00"}]

        def manifest(self, name, vid):
            return {"files": self.files, "version": {"id": vid}}

        def get_blob_to_file(self, h, dest, chunk=1024 * 1024):
            data = self.blobs[h]
            with open(dest, "wb") as f:
                f.write(data)
            self.downloaded += 1
            return len(data)

    pieces = []
    blobs = {}
    payload = b"M" * PIECE                    # 高压缩率，模拟真实 region
    for i in range(N):
        h = f"{i + 7000:032x}"
        pieces.append([i * PIECE, PIECE, h])
        blobs[h] = pack_piece(payload)
    size_total = N * PIECE
    mem_files = {
        "region/r.0.0.mca": {"size": size_total, "pieces": pieces,
                             "hash": "hdr", "mtime": time.time()},
        "level.dat": {"size": 0, "pieces": [], "hash": "", "mtime": time.time()},
        "data/": {"type": "dir", "size": 0, "pieces": []},
    }
    mem = MemoryApi(mem_files, blobs)
    cfg.set("auto_fix_playerdata", False)      # 避免玩家数据修正干扰断言
    shutil.rmtree(os.path.join(cfg.dir, "blobcache"), ignore_errors=True)

    dl_peaks = []
    prog3 = Progress(cb=lambda st: dl_peaks.append(int(st.get("total_bytes", 0) or 0)),
                     interval=0)
    eng3 = SyncEngine(mem, progress=prog3, log=lambda m: None)
    dest = os.path.join(TMP, "downloaded", "SizeWorld")
    r = eng3.download(dest, "SizeWorld")

    peak = max(dl_peaks) if dl_peaks else 0
    check("下载完成", r.get("downloaded_files", 0) >= 2, str(r)[:120])
    check("进度总量 ≈ 存档实际大小，而非 4MiB × 分片数",
          peak <= size_total * 1.05 and peak > size_total * 0.5,
          f"峰值 {hsize(peak)}，存档 {hsize(size_total)}，旧公式 {hsize(CHUNK * N)}")
    check("确实发起了 1300 次块下载", mem.downloaded == N, str(mem.downloaded))
    outp = os.path.join(dest, "region", "r.0.0.mca")
    check("组装后文件大小正确",
          os.path.exists(outp) and os.path.getsize(outp) == size_total,
          hsize(os.path.getsize(outp)) if os.path.exists(outp) else "缺失")
    with open(outp, "rb") as f:
        ok_head = f.read(PIECE) == payload
        f.seek(size_total - PIECE)
        ok_tail = f.read(PIECE) == payload
    check("首片与末片内容还原正确", ok_head and ok_tail)

    # download() 里三处接入点必须都在，防止以后被重构掉
    import inspect
    src_dl = inspect.getsource(SyncEngine.download)
    check("download() 调用了清单体检", "_validate_download_manifest" in src_dl)
    check("download() 传了真实分片长度", "sizes=piece_sizes" in src_dl)
    check("set_stage 重置了字节进度", "total_bytes=0, done_bytes=0" in src_dl)
    check("transfer 不再使用 4MiB × 分片数 估算",
          "self.chunk_size * len(todo)" not in
          inspect.getsource(ChunkTransfer.download_chunks))

    print("\n" + "=" * 70)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 下载总量不再虚高，膨胀清单已拦截")
    print("=" * 70)


if __name__ == "__main__":
    main()
