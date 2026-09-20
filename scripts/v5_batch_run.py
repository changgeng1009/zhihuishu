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
import re
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


def _expected_dur(title: str) -> float | None:
    """从条目文本解析时长「00:14:26」→ 秒。用于校验视频真的切换了。"""
    m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", title)
    if not m:
        m = re.search(r"(\d{1,2}):(\d{2})", title)
        if not m:
            return None
        return int(m.group(1)) * 60 + int(m.group(2))
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _task_coords(page, title12: str) -> dict | None:
    """滚动到条目可见处，返回可点坐标（须在视口内）。"""
    page.eval(
        "(() => { const el = [...document.querySelectorAll('.child-info.hasvideo')]"
        ".find(e => (e.textContent||'').replace(/\\s+/g,' ').trim().startsWith(T));"
        "if (el) el.scrollIntoView({block: 'center'}); })()".replace(
            "T", json.dumps(title12), 1),
        wait=False,
    )
    time.sleep(0.7)
    raw = page.eval(
        r"""JSON.stringify((() => {
      const el = [...document.querySelectorAll('.child-info.hasvideo')]
        .find(e => (e.textContent||'').replace(/\s+/g,' ').trim().startsWith(T));
      if (!el) return null;
      const target = el.firstElementChild || el;   // 实测点内层才生效
      const r = target.getBoundingClientRect();
      if (r.y < 0 || r.y > innerHeight - 10 || r.width < 5) return null;  // 视口外
      return {x: Math.round(r.x + Math.min(r.width/2, 90)), y: Math.round(r.y + r.height/2)};
    })())""".replace("T)", json.dumps(title12) + ")", 1).replace(
            "T)", json.dumps(title12) + ")", 1),
        wait=False,
    )
    return json.loads(raw) if isinstance(raw, str) else raw


def play_one(page, task: dict, wait_answer_s: int, prev_dur: float | None) -> tuple[str, float | None]:
    """播一个任务点到结束。返回 (ok/timeout/skip, 当前视频时长)。"""
    expected = _expected_dur(task["t"])
    title12 = task["t"][:12]

    # 点开并确认视频真的切换了（dur ≈ 条目时长；或 dur 变化 / cur 归零）
    v = video_state(page) or {}
    loaded = False
    for attempt in range(3):
        coords = _task_coords(page, title12)
        if not coords:
            time.sleep(2)
            continue
        click(page, coords["x"], coords["y"])
        for _ in range(20):                       # 最多 40s 等 loader
            time.sleep(2)
            v = video_state(page)
            if not v or not v.get("dur"):
                continue
            dur, cur = v.get("dur"), v.get("cur", 0)
            if expected and abs(dur - expected) < 6:
                loaded = True
                break
            if not expected and (prev_dur is None or abs(dur - prev_dur) > 6 or cur < 30):
                loaded = True
                break
        if loaded:
            break
        print(f"    [warn] 第 {attempt+1} 次点击后视频未切换（dur={v.get('dur')}）", flush=True)
    if not loaded:
        return "skip", v.get("dur") if v else prev_dur

    page.eval("document.querySelector('video').muted = true", wait=False)
    print(f"    [play] dur={v['dur']:.0f}s cur={v['cur']:.0f}s（已静音）", flush=True)
    if v.get("cur", 0) >= v["dur"] - 10:
        return "ok", v["dur"]                      # 播放器续在末尾 = 之前已看完

    if v.get("cur", 0) < v["dur"] - 5:             # 从头看的才设倍速
        print("    [speed]", set_speed_via_ui(page), flush=True)

    # 播放到结束；弹题工单制
    while True:
        time.sleep(3)
        v = video_state(page)
        if not v:
            return "ok", v["dur"] if v else None   # 播完元素销毁
        if v.get("dur") and v.get("cur", 0) >= v["dur"] - 1.5:
            return "ok", v["dur"]
        r = handle_popup(page, ANSWER_DIR, wait_answer_s=wait_answer_s)
        if r == "timeout":
            return "timeout", v.get("dur")
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
        prev_dur: float | None = None
        for i, task in enumerate(tasks):
            if time.time() - t0 > args.limit_min * 60:
                print(f"[batch] 到达时限，已完成 {done}", flush=True)
                break
            print(f"[batch] ({i+1}/{len(tasks)}) {task['t'][:36]}", flush=True)
            r, prev_dur = play_one(page, task, args.wait_answer, prev_dur)
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
