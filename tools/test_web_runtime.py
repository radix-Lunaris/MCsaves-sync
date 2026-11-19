# -*- coding: utf-8 -*-
"""
管理页面「脚本能否真正跑起来」运行时测试

背景（真实事故，两次）：
  1. 一个字符串字面量里混进了换行 → 整段 <script> 解析失败
  2. $('#btnNewAcct').onclick=newAccount 写在了
     window.newAccount = function(){} 之前 → ReferenceError

两者的后果完全一样：脚本在执行到 boot() 之前就中断，
于是既不发请求、也不弹登录框，页面永远停在静态占位
「正在连接服务器…」。用户在浏览器里只能看到"一直连接服务器"，
而服务端其实是好的 —— 这类问题靠肉眼看 HTML 极难发现。

静态语法检查（test_web_boot.py）只能挡住第 1 类，
挡不住第 2 类"语法正确但一跑就崩"。所以这里用 node + 极简 DOM 桩件
真正执行页面脚本，断言：
  - 执行过程不抛异常
  - boot() 真的发起了请求
  - 连不上时会渲染出可见的错误提示（而不是静默）

用法： python3 tools/test_web_runtime.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "server", "static", "index.html")
HARNESS = "/tmp/mcsync_dom_test.js"

OK: list = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  — {extra}" if extra else ""))
    OK.append((name, cond))
    if not cond:
        raise SystemExit(f"测试失败：{name} {extra}")


HARNESS_SRC = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const main = blocks[blocks.length - 1];

const els = {};
function mk(id){
  if(els[id]) return els[id];
  els[id] = {
    id, _text:'', innerHTML:'', style:{}, dataset:{},
    classList:{ add(){}, remove(){}, contains(){return false} },
    set textContent(v){ this._text=v; }, get textContent(){ return this._text; },
    querySelector(){ return null; }, appendChild(){}, insertBefore(){},
    remove(){}, focus(){}, closest(){ return null; },
    onclick:null, value:'',
  };
  return els[id];
}
global.document = {
  documentElement:{ dataset:{} },
  querySelector(s){ return mk(String(s).replace('#','')); },
  getElementById(id){ return mk(id); },
  createElement(){ return mk('tmp'+Math.random()); },
  hidden:false,
};
global.window = global;
global.location = { origin:'http://127.0.0.1:1', reload(){} };
global.localStorage = { getItem(){return null}, setItem(){} };
global.setTimeout = () => 0;
global.clearTimeout = () => {};
global.setInterval = () => 0;

let fetchCalls = 0;
const mode = process.argv[3] || 'fail';
global.fetch = async () => {
  fetchCalls++;
  if (mode === 'fail') throw new TypeError('Failed to fetch');
  return { ok:true, status:200, json: async () => ({}) };
};

let crashed = null;
try { eval(main); } catch (e) { crashed = e.message; }

setImmediate(() => {
  const out = {
    crashed,
    fetchCalls,
    saveList: (els['saveList'] || {}).innerHTML || '',
    connErr:  (els['connErr']  || {}).innerHTML || '',
    connErrShown: (els['connErr'] || {}).style ? (els['connErr'].style.display !== 'none') : false,
  };
  console.log('@@RESULT@@' + JSON.stringify(out));
});
"""


def run_harness(mode: str) -> dict:
    with open(HARNESS, "w", encoding="utf-8") as f:
        f.write(HARNESS_SRC)
    r = subprocess.run(["node", HARNESS, HTML, mode],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return {"crashed": (r.stderr or "").strip()[:300], "fetchCalls": 0,
                "saveList": "", "connErr": "", "connErrShown": False}
    for line in (r.stdout or "").splitlines():
        if line.startswith("@@RESULT@@"):
            return json.loads(line[len("@@RESULT@@"):])
    return {"crashed": "no result", "fetchCalls": 0, "saveList": "",
            "connErr": "", "connErrShown": False}


def main():
    print("\n=== 管理页面脚本运行时测试 ===\n")
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        print("  [跳过] 未安装 node，无法做运行时校验")
        return

    print("[1] 脚本能完整执行（不因前向引用/语法错误中断）")
    r = run_harness("ok")
    check("执行过程未抛异常", not r["crashed"], str(r["crashed"]))
    check("boot() 确实发起了请求", r["fetchCalls"] > 0, str(r["fetchCalls"]))

    print("\n[2] 连不上时必须渲染可见错误（不能静默）")
    r2 = run_harness("fail")
    check("失败路径也未崩溃", not r2["crashed"], str(r2["crashed"]))
    check("显示了连接错误横幅", "无法连接服务端" in r2["connErr"], "")
    check("错误横幅可见（display 未被设为 none）", r2["connErrShown"], "")
    check("加载占位被替换为失败说明", "无法加载" in r2["saveList"], "")
    check("给出重试入口", "location.reload" in r2["saveList"]
          or "重试" in r2["saveList"], "")
    check("提示排查代理/端口", ("代理" in r2["connErr"]) and ("端口" in r2["connErr"]), "")

    print("\n[3] 事故复现：模拟旧写法必须被抓到")
    # 故意注入"前向引用"写法，确认本测试确实能发现它
    src = open(HTML, encoding="utf-8").read()
    broken = src.replace("async function newAccount(){",
                         "window.newAccount___x = async function(){", 1)
    broken = broken.replace("$('#btnNewAcct').onclick=newAccount;",
                            "$('#btnNewAcct').onclick=newAccount___x;", 1)
    tmp_html = "/tmp/mcsync_broken.html"
    with open(tmp_html, "w", encoding="utf-8") as f:
        f.write(broken)
    with open(HARNESS, "w", encoding="utf-8") as f:
        f.write(HARNESS_SRC)
    r3 = subprocess.run(["node", HARNESS, tmp_html, "ok"],
                        capture_output=True, text=True, timeout=60)
    out3 = ""
    for line in (r3.stdout or "").splitlines():
        if line.startswith("@@RESULT@@"):
            out3 = line
    check("旧写法会被判定为崩溃（测试有效）",
          '"crashed":' in out3 and "not defined" in out3, out3[:160])

    print("\n" + "=" * 58)
    bad = [n for n, o in OK if not o]
    if bad:
        print(f"❌ 失败 {len(bad)} 项: {bad}")
        raise SystemExit(1)
    print(f"✅ 全部 {len(OK)} 项通过 — 页面脚本能真正跑起来")
    print("=" * 58)


if __name__ == "__main__":
    main()
