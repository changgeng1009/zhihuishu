# -*- coding: utf-8 -*-
"""V5 批量跑课：枚举全部视频任务点，逐个播放到完（像 cx 的 run_chapter）。

流程（每个任务点）：
  真实点击 → 等元数据 → 静音 → 真实 UI 设 1.5x → 循环监视：
    - 弹题（handle_popup 工单制，Agent 作答；超时 240s → 暂停转人工并停止批量）
    - 播完判定（cur >= dur - 1.5）→ 下一个
全片结束或超时退出。断点：重启后从第一个"未练习"任务点继续（简化：全量重跑，
平台对已完成小节的重复播放会快进/不计，代价可接受）。

红线不变：视频页零注入（只读 DOM + 真实鼠标事件）；考试/作业页一律不碰。

用法：
    python scripts/v5_batch_run.py --limit-min 480 --visible
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("ORCH_EDGE_DEBUG_PORT", "9334")
os.environ.setdefault(
    "ORCH_EDGE_PROFILE", r"D:\CodexWork\智慧树刷课\accounts\quiz_profile")

from orchestrator import browser as B
from orchestrator import cdp as cdp_mod
from orchestrator.adapters.zhs_browser import _PageSession
from orchestrator.cookies import CookieStore, ZHS_DOMAIN_SUFFIXES
from v4_playback_quiz import (
    ANSWER_DIR, URL, handle_popup, set_speed_via_ui, video_state,
)


def mouse(page, ev, x, y, pressed=False):
    page._client.call("Input.dispatchMouseEvent", {
        "type": ev, "x": x, "y": y, "button": "left",
        "buttons": 1 if pressed else 0, "clickCount": 1 if pressed else 0,
    })


def click(page, x, y):
    mouse(page, "mouseMoved", x, y); time.sleep(0.25)
    mouse(page, "mousePressed", x, y, True); time.sleep(0.05)
    mouse(page, "mouseReleased", x, y); time.sleep(0.6)


def enumerate_tasks(page) -> list[dict]:
    """枚举左侧列表的全部视频任务点（滚动加载全覆盖）。"""
    page.eval(r"""(() => {
      const box = document.querySelector('.left-aside, .chapter-list, [class*=list]');
      if (box) box.scrollTop = 0;
    })()""", wait=False)
    seen: dict[str, dict] = {}
    for _ in range(14):                      # 逐步滚动逼出懒加载条目
        raw = page.eval(r"""JSON.stringify((() => {
          const out = [];
          document.querySelectorAll('.child-info.hasvideo').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 5) return;
            out.push({t: (el.textContent||'').replace(/\s+/g,' ').trim().slice(0,40),
                      x: Math.round(r.x + Math.min(r.width/2, 90)),
                      y: Math.round(r.y + r.height/2)});
          });
          return out;
        })())""", wait=False)
        for it in (json.loads(raw) if isinstance(raw, str) else raw) or []:
            seen.setdefault(it["t"], it)
        page.eval(r"""(() => {
          const box = document.querySelector('.left-aside, .chapter-list, [class*=list]');
          if (box) box.scrollTop += 600;
        })()""", wait=False)
        time.sleep(0.4)
    return list(seen.values())


def play_one(page, task: dict, wait_answer_s: int) -> str:
    """播一个任务点到结束。返回 ok / timeout。"""
    # 列表可能滚走了：滚回该条目可见处再点
    page.eval(r"""(() => {
      const el = [...document.querySelectorAll('.child-info.hasvideo')]
        .find(e => (e.textContent||'').replace(/\s+/g,' ').trim()
                   .startsWith(__TASK__.t.slice(0, 12)));
      if (el) el.scrollIntoView({block: 'center'});
    })()""".replace("__TASK__.t.slice(0, 12)", json.dumps(task["t"][:12])), wait=False)
    time.sleep(0.6)
    fresh = page.eval(r"""JSON.stringify((() => {
      const el = [...document.querySelectorAll('.child-info.hasvideo')]
        .find(e => (e.textContent||'').replace(/\s+/g,' ').trim()
                   .startsWith(T.slice(0, 12)));
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {x: Math.round(r.x + Math.min(r.width/2, 90)), y: Math.round(r.y + r.height/2)};
    })())""".replace("T.slice(0, 12)", json.dumps(task["t"][:12])), wait=False)
    fresh = json.loads(fresh) if isinstance(fresh, str) else fresh
    if not fresh:
        return "skip"
    click(page, fresh["x"], fresh["y"])

    v = None
    for _ in range(40):
        time.sleep(2)
        v = video_state(page)
        if v and v.get("dur"):
            break
    if not (v and v.get("dur")):
        return "skip"
    page.eval("document.querySelector('video').muted = true", wait=False)
    print(f"    [play] dur={v['dur']:.0f}s cur={v['cur']:.0f}s（已静音）", flush=True)
    if v.get("cur", 0) < v["dur"] - 5:            # 从头看的才设倍速
        print("    [speed]", set_speed_via_ui(page), flush=True)

    # 播放到结束；弹题工单制
    while True:
        time.sleep(3)
        v = video_state(page)
        if not v:
            return "ok"                            # 播完元素销毁
        if v.get("dur") and v.get("cur", 0) >= v["dur"] - 1.5:
            return "ok"
        r = handle_popup(page, ANSWER_DIR, wait_answer_s=wait_answer_s)
        if r == "timeout":
            return "timeout"
        if r.startswith("answered"):
            print(f"    [quiz] 已答 {r.split(' ', 1)[1]}", flush=True)
            # 面板关闭 + 恢复播放
            page.eval(r"""(()=>{const w=document.querySelector('.ai-test-question-wrapper');
              if (!w) return; for (const el of w.querySelectorAll('[class*=close]')) {
                const r = el.getBoundingClientRect();
                if (r.width > 6 && r.width < 50) {
                  const o = {bubbles:true}; el.dispatchEvent(new MouseEvent('click', o));
                } } })()""", wait=False)
            time.sleep(1.5)
            v2 = video_state(page)
            if v2 and v2.get("paused"):
                page.eval("document.querySelector('video').play()", wait=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-min", type=int, default=480)
    ap.add_argument("--visible", action="store_true")
    ap.add_argument("--wait-answer", type=int, default=240)
    args = ap.parse_args()

    port = 9334
    plan, proc = B.launch(background=None if args.visible else "offscreen")
    dl = time.time() + 40
    while time.time() < dl:
        if B.probe_cdp(port).alive:
            break
        time.sleep(1)
    print("[boot] 浏览器就绪 port=9334 防节流已加", flush=True)

    with _PageSession(port) as page:
        page.enable_page()
        jar = CookieStore(root=Path("accounts")).load("acc_01")
        target = [c.to_dict() for c in jar
                  if any(c.domain.lstrip(".").endswith(s) for s in ZHS_DOMAIN_SUFFIXES)]
        ok, _ = cdp_mod.set_all_cookies(port, target)
        print(f"[boot] cookie 恢复 {ok}/{len(target)}", flush=True)

        page.navigate(URL)
        ready = False
        for _ in range(30):
            time.sleep(2)
            d = page.eval(r"""JSON.stringify({url: location.href.slice(0,50),
                n: document.querySelectorAll('.child-main').length})""", wait=False)
            d = json.loads(d) if isinstance(d, str) else d
            if "login.zhihuishu.com" in d.get("url", ""):
                print(">> 会话失效，退出", flush=True); return 1
            if d.get("n", 0) > 50:
                ready = True; break
        if not ready:
            print(">> 课程页超时", flush=True); return 1

        tasks = enumerate_tasks(page)
        print(f"[batch] 视频任务点 {len(tasks)} 个", flush=True)
        t0 = time.time()
        done = 0
        for i, task in enumerate(tasks):
            if time.time() - t0 > args.limit_min * 60:
                print(f"[batch] 到达时限，已完成 {done}", flush=True)
                break
            print(f"[batch] ({i+1}/{len(tasks)}) {task['t'][:36]}", flush=True)
            r = play_one(page, task, args.wait_answer)
            if r == "timeout":
                print("[batch] 弹题超时转人工，批量停止", flush=True)
                return 2
            if r == "ok":
                done += 1
                print(f"[batch] ✓ 播完（累计 {done}）", flush=True)
        print(f"[batch] 结束：{done}/{len(tasks)} 播完", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
