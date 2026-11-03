# -*- coding: utf-8 -*-
"""区域差分与块级压缩回归测试"""
from __future__ import annotations
import os, sys, shutil, threading, time, random
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.join(ROOT, "client"))
TMP = "/tmp/mcsync-region"
OK = []
def check(n, c, e=""):
    print(("  ✅ " if c else "  ❌ ") + n + (f"  — {e}" if e else ""))
    OK.append((n, c)); 
    if not c: raise SystemExit(f"失败：{n} {e}")

SECTOR = 4096
def build_mca(path, seed, nchunks=32):
    rnd = random.Random(seed)
    payload = []
    for i in range(nchunks):
        payload.append(bytes(rnd.getrandbits(8) for _ in range(SECTOR * 2)))
    with open(path, "wb") as f:
        f.write(b"\x00" * 8192)
        for i, p in enumerate(payload):
            f.write(p)
        f.flush()
        # 回填 location 表
        for i in range(nchunks):
            off = 2 + i * 2
            f.seek(i * 4)
            f.write(bytes([(off >> 16) & 0xFF, (off >> 8) & 0xFF, off & 0xFF, 2]))

def main():
    print("\n=== 区域差分 & 压缩测试 ===")
    shutil.rmtree(TMP, ignore_errors=True); os.makedirs(TMP)
    os.environ["MCSYNC_DATA"] = os.path.join(TMP, "data")
    os.environ["MCSYNC_AUTH"] = "0"
    os.environ["MCSYNC_CONFIG_DIR"] = os.path.join(TMP, "cfg")
    import uvicorn, app
    threading.Thread(target=lambda: uvicorn.run(app.app, host="127.0.0.1", port=8981,
                                                log_level="error"), daemon=True).start()
    for _ in range(60):
        try:
            import requests
            if requests.get("http://127.0.0.1:8981/api/health", timeout=2).ok: break
        except Exception: time.sleep(0.3)

    from core.config import cfg
    from core.api import Api
    from core.sync import SyncEngine
    cfg.set("server_url", "http://127.0.0.1:8981")
    api = Api("http://127.0.0.1:8981")

    sd = os.path.join(TMP, "save"); os.makedirs(sd)
    mca = os.path.join(sd, "r.0.0.mca")
    build_mca(mca, 1)
    size = os.path.getsize(mca)
    print(f"\n[1] 构造 region 文件 {size/1024:.0f} KB（32 个 MC 区块）")

    eng = SyncEngine(api, log=lambda m: None)
    r1 = eng.upload(sd, "RegionWorld")
    print(f"    首次上传 {r1['uploaded_bytes']/1024:.0f} KB")
    check("首次上传成功", r1["version"] == 1, str(r1))

    # 只改第 5 个区块的内容
    print("\n[2] 只修改 1 个 MC 区块后重新上传")
    with open(mca, "r+b") as f:
        off = (2 + 5 * 2) * SECTOR
        f.seek(off)
        f.write(bytes(random.Random(99).getrandbits(8) for _ in range(SECTOR * 2)))
    os.utime(mca, (time.time() + 5, time.time() + 5))
    r2 = eng.upload(sd, "RegionWorld")
    up2 = r2["uploaded_bytes"]
    print(f"    增量上传 {up2/1024:.0f} KB（整文件 {size/1024:.0f} KB）")
    check("增量远小于整文件（区域差分生效）", up2 < size * 0.6,
          f"{up2/1024:.0f}KB / {size/1024:.0f}KB")
    check("确实只传了少量数据", up2 < 200 * 1024, f"{up2}B")

    print("\n[3] 下载还原后内容一致")
    sd2 = os.path.join(TMP, "save2"); os.makedirs(sd2)
    r3 = eng.download(sd2, "RegionWorld")
    a = open(mca, "rb").read(); b = open(os.path.join(sd2, "r.0.0.mca"), "rb").read()
    check("下载后逐字节一致", a == b, f"{len(a)} vs {len(b)}")
    check("文件大小一致", len(a) == len(b), f"{len(a)} vs {len(b)}")

    print("\n[4] 块级压缩")
    from core.protocol import pack_piece, unpack_piece, piece_size_for
    text = b"{\"a\":1}" * 5000
    pk = pack_piece(text)
    check("可压缩内容被压缩", pk[:1] == b"Z" and len(pk) < len(text) * 0.5,
          f"{len(text)} -> {len(pk)}")
    check("解压可还原", unpack_piece(pk) == text, "")
    rnd = os.urandom(20000)
    pk2 = pack_piece(rnd)
    check("不可压缩内容原样存储", pk2[:1] == b"R" and unpack_piece(pk2) == rnd, "")

    print("\n[5] 自适应分片大小")
    check("小文件用小块", piece_size_for(100 * 1024) == 256 * 1024, "")
    check("大文件用大块", piece_size_for(100 * 1024 * 1024) == 8 * 1024 * 1024, "")

    print("\n" + "=" * 58)
    bad = [n for n, o in OK if not o]
    if bad: print(f"❌ {bad}"); raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 区域差分与压缩生效")
    print("=" * 58)

if __name__ == "__main__":
    main()
