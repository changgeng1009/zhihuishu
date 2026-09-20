# -*- coding: utf-8 -*-
"""真人操作基线录制：附着已运行的独立浏览器，记录你手动点击视频
产生的全部请求与播放器状态时间线，供自动化链路对照。

前置：
  1. 双击项目根目录的 启动独立浏览器.cmd（父进程是 explorer，窗口才留得住）
  2. 运行本脚本：python scripts/record_manual_baseline.py
  3. 脚本会自动恢复登录态并打开课程页，看到「>>> 现在请你手动点击……」后，
     在窗口里点第一节视频「0.1 博大精深的数学文化」，然后正常看完/拖动均可
  4. 结束后输出 runs/manual_baseline.json

用法：
    python scripts/record_manual_baseline.py [--seconds 120]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import browser as B
from orchestrator.adapters.zhs_browser import _PageSession, build_page_config
from orchestrator.adapters.zhs_js import BOOTSTRAP_JS, API_RECORDER_JS
from orchestrator.cdp import set_all_cookies, probe_cdp
from orchestrator.cookies import CookieStore, ZHS_DOMAIN_SUFFIXES

URL = (
    "https://studywisdomh5.zhihuishu.com/study/index"
    "?recruitAndCourseId=4e5e50514d5a4859454a5859584250445c"
)

STATE_JS = r"""JSON.stringify((() => {
  const v = document.querySelector('video');
  return {
    t: Date.now(),
    url: location.href.slice(0, 90),
    childMain: document.querySelectorAll('.child-main').length,
    v: v ? {dur: v.duration, cur: v.currentTime, paused: v.paused, rs: v.readyState} : null,
  };
})())"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=120)
    args = ap.parse_args()

    # 1) 附着已运行实例；没有就提示
    if not probe_cdp().alive:
        print(">> 未检测到独立浏览器。请先双击项目根目录的「启动独立浏览器.cmd」，再运行本脚本。")
        return 1
    port = B.debug_port()
    print(f"[attach] CDP 在线 (port={port})")

    with _PageSession(port) as page:
        page.enable_page()
        page.add_init_script(API_RECORDER_JS)

        # 2) 恢复登录态并打开课程页
        store = CookieStore(root=Path("accounts"))
        jar = store.load("acc_01")
        if jar:
            target = [c.to_dict() for c in jar
                      if any(c.domain.lstrip(".").endswith(s) for s in ZHS_DOMAIN_SUFFIXES)]
            ok, errs = set_all_cookies(port, target)
            print(f"[session] cookie 恢复 {ok}/{len(target)}",
                  ("| " + str(errs[:2])) if errs else "")

        page.navigate(URL)
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline:
            d = json.loads(page.eval(STATE_JS, wait=False) or "{}")
            if "login.zhihuishu.com" in d.get("url", ""):
                print(">> 会话失效且恢复无效 —— 请在窗口里重新扫码登录，登录后重跑本脚本。")
                return 1
            if d.get("childMain", 0) > 50:
                ready = True
                break
            time.sleep(2)
        if not ready:
            print(">> 课程页加载超时")
            return 1

        # 关掉「课程提醒」弹窗（下次再说），别让它挡住你的操作
        page.eval(f"window.__ZHS_CFG__ = {json.dumps(build_page_config(URL), ensure_ascii=False)}",
                  wait=False)
        page.eval(BOOTSTRAP_JS, wait=False)
        page.eval(r"""(()=>{const vis=el=>!!el&&el.getClientRects().length>0;
          const b=Array.from(document.querySelectorAll('.el-button.btn02')).find(vis);
          if(b)b.click()})()""", wait=False)

        # 3) 录制窗口：等你手动点击
        print()
        print(">>> 现在请你手动点击第一节视频「0.1 博大精深的数学文化」，")
        print(">>> 然后正常观看/拖动都行。录制中……")
        print()
        t0 = time.time()
        states: list[dict] = []
        while time.time() - t0 < args.seconds:
            try:
                s = json.loads(page.eval(STATE_JS, wait=False) or "{}")
                states.append(s)
                elapsed = int(time.time() - t0)
                v = s.get("v")
                if v and v.get("dur"):
                    print(f"[{elapsed:3d}s] dur={v['dur']:.0f}s cur={v['cur']:.1f}s "
                          f"rate={v.get('rate')} paused={v['paused']}")
            except Exception:
                pass
            time.sleep(2)

        # 4) 导出
        reqs = page.eval(r"""JSON.stringify((window.__ZHS_API__ ?
            window.__ZHS_API__.all() : []).map(r => ({
              t: r.t, url: r.url, kind: r.kind, method: r.method,
              body: r.body, data: r.data })))""", wait=False) or "[]"
        out = Path("runs/manual_baseline.json")
        out.write_text(json.dumps({
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "states": states,
            "requests": json.loads(reqs),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[out] {out}  (states={len(states)}, requests={len(json.loads(reqs))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
