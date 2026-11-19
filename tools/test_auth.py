import os, sys, shutil, subprocess, time, json, hashlib, hmac, secrets
import requests

ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA="/tmp/sec_data"; shutil.rmtree(DATA, ignore_errors=True); os.makedirs(DATA)
TOKEN="Ab3xK9mQ2pL7rTz1"
PORT="18778"
env=dict(os.environ, MCSYNC_DATA=DATA, MCSYNC_PORT=PORT, MCSYNC_TOKEN=TOKEN,
         MCSYNC_AUTH="1", PYTHONPATH=ROOT+"/server")
srv=subprocess.Popen([sys.executable,"-m","uvicorn","app:app","--host","127.0.0.1",
                      "--port",PORT,"--log-level","warning"],
                     cwd=ROOT+"/server", env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
B=f"http://127.0.0.1:{PORT}"
def ok(c,m): print(("   ✅ " if c else "   ❌ ")+m); assert c, m
try:
    for _ in range(60):
        try: requests.get(B+"/api/health",timeout=2); break
        except Exception: time.sleep(0.5)

    print("[1] 未认证访问受保护接口")
    r=requests.get(B+"/api/stats",timeout=5); ok(r.status_code==401, f"/api/stats 无凭证 -> {r.status_code} (期望401)")
    r=requests.get(B+"/api/health",timeout=5); ok(r.status_code==200, f"/api/health 仍公开 -> {r.status_code}")
    r=requests.get(B+"/api/docs",timeout=5); ok(r.status_code!=200, f"交互式文档不可访问 -> {r.status_code}")

    print("\n[2] 账号登录：错误口令 / 正确口令")
    # 先用管理员密钥引导出管理员账号，之后一律走账号登录
    r=requests.post(B+"/api/bootstrap",json={"admin_key":TOKEN,"username":"tester",
                                             "password":"TesterPass123"})
    ok(r.status_code==200, f"引导创建管理员 -> {r.status_code} {r.text[:80]}")
    r=requests.post(B+"/api/login",json={"username":"tester","password":"wrong",
                                         "client_id":"c1","secret":"s"*64})
    ok(r.status_code==401, f"错误口令 -> {r.status_code}")
    CID="pc-test-0001"; SEC=secrets.token_hex(32)
    r=requests.post(B+"/api/login",json={"username":"tester","password":"TesterPass123",
                                         "client_id":CID,"secret":SEC,"name":"测试机"})
    ok(r.status_code==200, f"正确账号登录 -> {r.status_code} {r.text[:80]}")

    print("\n[3] 签名访问")
    def sign(method,path,body=b"",ts=None,nonce=None):
        ts=str(ts or int(time.time())); nonce=nonce or secrets.token_hex(8)
        base="\n".join([ts,nonce,method.upper(),path,hashlib.sha256(body).hexdigest()])
        sig=hmac.new(SEC.encode(),base.encode(),hashlib.sha256).hexdigest()
        return {"X-MCSync-Key":CID,"X-MCSync-Ts":ts,"X-MCSync-Nonce":nonce,"X-MCSync-Sign":sig}
    r=requests.get(B+"/api/stats",headers=sign("GET","/api/stats"),timeout=5)
    ok(r.status_code==200, f"正确签名 -> {r.status_code}")
    # 重放
    h=sign("GET","/api/stats"); r1=requests.get(B+"/api/stats",headers=h,timeout=5)
    r2=requests.get(B+"/api/stats",headers=h,timeout=5)
    ok(r1.status_code==200 and r2.status_code==401, f"同一 nonce 重放 -> {r1.status_code}/{r2.status_code} (期望200/401)")
    # 篡改签名
    h=sign("GET","/api/stats"); h["X-MCSync-Sign"]="0"*64
    ok(requests.get(B+"/api/stats",headers=h,timeout=5).status_code==401, "错误签名 -> 401")
    # 过期时间戳
    h=sign("GET","/api/stats",ts=int(time.time())-1000)
    ok(requests.get(B+"/api/stats",headers=h,timeout=5).status_code==401, "过期时间戳 -> 401")
    # 篡改 body
    body=json.dumps({"base_version":0,"files":[]},separators=(",",":")).encode()
    h=sign("POST","/api/save/w/plan",body)
    r=requests.post(B+"/api/save/w/plan",data=body+b"XX",headers=h,timeout=5)
    ok(r.status_code==401, f"body 被篡改 -> {r.status_code}")

    print("\n[4] Web 登录会话")
    s=requests.Session()
    r=s.post(B+"/api/web/login",json={"username":"tester","password":"wrong"}); ok(r.status_code==401,"错误口令登录 -> 401")
    r=s.post(B+"/api/web/login",json={"username":"tester","password":"TesterPass123"}); ok(r.status_code==200,f"正确账号登录 -> {r.status_code}")
    ok("mcsync_session" in s.cookies.get_dict(), f"已下发 HttpOnly 会话 Cookie: {list(s.cookies.get_dict())}")
    ok(s.get(B+"/api/stats",timeout=5).status_code==200,"带 Cookie 访问 -> 200")
    # 安全信息属管理员接口，普通账号需以管理员身份访问
    ok(s.get(B+"/api/security",timeout=5).status_code==200,"安全信息可读")

    print("\n[5] 暴力防护")
    bad=None
    for i in range(12):
        r=requests.post(B+"/api/web/login",json={"username":"tester","password":"x"*16})
        if r.status_code==429: bad=i+1; break
    ok(bad is not None, f"连续错误口令 -> 第 {bad} 次触发锁定 (429)")

    # 步骤5故意触发了 IP 锁定，重启服务以清空内存中的锁定状态
    srv.terminate(); srv.wait(timeout=10)
    srv=subprocess.Popen([sys.executable,"-m","uvicorn","app:app","--host","127.0.0.1",
                          "--port",PORT,"--log-level","warning"],
                         cwd=ROOT+"/server", env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try: requests.get(B+"/api/health",timeout=2); break
        except Exception: time.sleep(0.5)

    print("\n[6] 客户端完整上传（自动配对）")
    sys.path.insert(0, ROOT+"/client")
    from core.api import Api
    from core.config import cfg
    from core.sync import SyncEngine
    cfg.dir="/tmp/sec_cfg"; os.makedirs(cfg.dir,exist_ok=True)
    cfg.set("server_url",B); cfg.set_secret(""); cfg.set("client_id","")
    cfg.set("paired_server",""); cfg.set("concurrency",4)
    cfg.set("username","tester"); cfg.set_secret("TesterPass123","password")
    SAVE="/tmp/sec_save"; shutil.rmtree(SAVE,ignore_errors=True); os.makedirs(SAVE)
    open(SAVE+"/level.dat","wb").write(os.urandom(2048))
    open(SAVE+"/empty.dat","wb").close()
    api=Api(B)
    ok(api.ensure_login() is True, "自动登录执行成功")
    res=SyncEngine(api, log=lambda m:None).upload(SAVE,"SecWorld")
    ok(res["file_count"]==2, f"上传成功 v{res['version']}, {res['file_count']} 文件")
    # 撤销客户端后应被拒绝
    requests.delete(B+f"/api/security/clients/{CID}",headers=sign("DELETE",f"/api/security/clients/{CID}"),timeout=5)
    r=requests.get(B+"/api/stats",headers=sign("GET","/api/stats"),timeout=5)
    ok(r.status_code==401, f"撤销客户端后访问 -> {r.status_code} (期望401)")
finally:
    srv.terminate(); srv.wait(timeout=10)
print("\n=== 安全测试全部通过 ✅ ===")
