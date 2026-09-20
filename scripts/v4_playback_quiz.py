# -*- coding: utf-8 -*-
"""V2+V4：后台播放 + 弹题 Agent 作答（agent-in-the-loop）。

## 流程

  启动(offscreen+防节流) → 恢复会话 → 课程页 → 真实点击任务点 → 播放
  → 找播放器内的真实倍速 UI 并点击 1.5（仅在播放器区域内找，避免点到
  左侧课程列表的章节编号"1.5"）
  → 循环监视弹题：
      发现弹题 → 提取题干与选项 → 写工单 runs/answer/pending_<id>.json
      → 等待 Agent 写 runs/answer/answer_<id>.json（超时 90s）
        - 拿到答案：真实鼠标点选项 → 点「提交作答」→ 关闭弹窗 → 继续播
        - 超时：暂停视频，退出（铁律：宁可慢，不可错，绝不随机作答）

## 红线

  - 视频页零注入：读 DOM 用 Runtime.evaluate（不留全局、不 patch 函数），
    所有点击走 Input.dispatchMouseEvent（isTrusted=true）。
  - 只处理「随堂练习/弹题」；考试/作业/监考类页面一律不碰（safety.py 另有守卫）。

用法（后台运行）：
    python scripts/v4_playback_quiz.py --limit-min 15
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 独立端口 + 独立 profile：不与用户手动用的主实例（9333）互相干扰。
# 必须在 import orchestrator.browser 之前设置（debug_port/profile_dir 读环境变量）。
import os

os.environ.setdefault("ORCH_EDGE_DEBUG_PORT", "9334")
os.environ.setdefault(
    "ORCH_EDGE_PROFILE", r"D:\CodexWork\智慧树刷课\accounts\quiz_profile")

from orchestrator import browser as B
from orchestrator import cdp as cdp_mod
from orchestrator.adapters.zhs_browser import _PageSession
from orchestrator.cookies import CookieStore, ZHS_DOMAIN_SUFFIXES

URL = (
    "https://studywisdomh5.zhihuishu.com/study/index"
    "?recruitAndCourseId=4e5e50514d5a4859454a5859584250445c"
)
ANSWER_DIR = Path("runs/answer")

STATE_JS = r"""JSON.stringify((() => {
  const v = document.querySelector('video');
  return {t: Date.now(),
          n: document.querySelectorAll('.child-main').length,
          url: location.href.slice(0, 60),
          v: v ? {dur: v.duration, cur: v.currentTime, paused: v.paused} : null};
})())"""

#: 弹题容器：能找到「提交作答」按钮的可见容器
POPUP_JS = r"""JSON.stringify((() => {
  const vis = (el) => { if (!el) return false;
    const r = el.getClientRects(); if (!r.length) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none'; };
  // 1) 找提交按钮
  let submit = null;
  for (const el of document.querySelectorAll('button, .btn, [class*=submit], a, span, div')) {
    if (!vis(el) || el.children.length > 2) continue;
    const t = (el.textContent || '').replace(/\s+/g, '');
    if (t === '提交作答') { submit = el; break; }
  }
  if (!submit) return null;
  // 2) 向上找容器（含题干的最近祖先）
  let box = submit;
  for (let i = 0; i < 8 && box.parentElement; i++) {
    box = box.parentElement;
    if ((box.textContent || '').length > 60) break;
  }
  const br = box.getBoundingClientRect();
  return {found: true, box: {x: br.x, y: br.y, w: br.width, h: br.height},
          text: (box.innerText || '').slice(0, 1200)};
})())"""

#: 弹题内选项坐标（可见的 A/B/C/D 项）
OPTIONS_JS = r"""JSON.stringify((() => {
  const vis = (el) => { const r = el.getClientRects(); return r.length > 0; };
  const out = [];
  for (const el of document.querySelectorAll('li, label, div, p, span')) {
    if (el.children.length) continue;
    const t = (el.textContent || '').trim();
    const m = t.match(/^([A-D])\s*[.、．:：]?\s*(.+)$/);
    if (!m) continue;
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 5 || r.height < 5) continue;
    out.push({letter: m[1], text: m[2].slice(0, 60),
              x: Math.round(r.x + Math.min(r.width / 2, 60)), y: Math.round(r.y + r.height / 2)});
  }
  return out;
})())"""

SUBMIT_JS = r"""JSON.stringify((() => {
  const vis = (el) => { const r = el.getClientRects(); return r.length > 0; };
  for (const el of document.querySelectorAll('button, .btn, [class*=submit], a, span, div')) {
    if (!vis(el) || el.children.length > 2) continue;
    const t = (el.textContent || '').replace(/\s+/g, '');
    if (t === '提交作答') {
      const r = el.getBoundingClientRect();
      return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};
    }
  }
  return null;
})())"""


def mouse(page, ev, x, y, pressed=False):
    page._client.call("Input.dispatchMouseEvent", {
        "type": ev, "x": x, "y": y, "button": "left",
        "buttons": 1 if pressed else 0, "clickCount": 1 if pressed else 0,
    })


def click(page, x, y):
    mouse(page, "mouseMoved", x, y)
    time.sleep(0.15)
    mouse(page, "mousePressed", x, y, True)
    mouse(page, "mouseReleased", x, y)


def video_state(page) -> dict | None:
    raw = page.eval(r"""JSON.stringify((()=>{const v=document.querySelector('video');
        return v?{dur:v.duration,cur:v.currentTime,paused:v.paused}:null})())""", wait=False)
    if isinstance(raw, str):
        return json.loads(raw or "null")
    return raw


def set_speed_via_ui(page) -> str:
    """通过播放器真实 UI 设 1.5 倍速（2026-09-20 实测打通的流程）。

    播放器是 ableVideoPlayer：控制栏右侧有「X 1.0」当前倍率标签，
    悬浮它展开竖排菜单（X 1.5 / X 1.25 / X 1.0，在触发器上方）。
    刻意不走 video.playbackRate 直改：实测 rate 被接受但 cur 冻结
    （播放器抗篡改）。触发器必须在 video 矩形内找——左侧课程列表
    的章节编号"1.5"（child-sort）会干扰全局搜索。
    """
    vw = page.eval(
        "(()=>{const v=document.querySelector('video').getBoundingClientRect();"
        "return {x:v.x,y:v.y,width:v.width,height:v.height}})()", wait=False)
    if isinstance(vw, str):
        vw = json.loads(vw)

    def mouse(ev, x, y, pressed=False):
        page._client.call("Input.dispatchMouseEvent", {
            "type": ev, "x": x, "y": y, "button": "left",
            "buttons": 1 if pressed else 0, "clickCount": 1 if pressed else 0})

    def click(x, y):
        mouse("mouseMoved", x, y); time.sleep(0.25)
        mouse("mousePressed", x, y, True); time.sleep(0.05)
        mouse("mouseReleased", x, y); time.sleep(0.8)

    # 让控制栏出现
    mouse("mouseMoved", vw["x"] + vw["width"] * 0.5, vw["y"] + vw["height"] * 0.85)
    time.sleep(0.3)
    mouse("mouseMoved", vw["x"] + vw["width"] * 0.5, vw["y"] + vw["height"] - 15)
    time.sleep(1.2)

    trig = page.eval(r"""JSON.stringify((() => {
      const v = document.querySelector('video').getBoundingClientRect();
      for (const el of document.querySelectorAll('span,div,li')) {
        if (el.children.length) continue;
        const t = (el.textContent||'').replace(/\s+/g,'').toUpperCase();
        if (!/^(X)?1\.0(X)?$/.test(t)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 5 || r.x < v.x || r.x > v.x + v.width) continue;
        if (r.y < v.y + v.height - 100 || r.y > v.y + v.height + 30) continue;
        return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
      }
      return null;
    })())""", wait=False)
    trig = json.loads(trig) if isinstance(trig, str) else trig
    if not trig:
        return "未找到倍率触发器 X 1.0"
    mouse("mouseMoved", trig["x"], trig["y"])
    time.sleep(1.5)          # 纯悬浮等菜单展开（点击反而会收起）
    item = page.eval(r"""JSON.stringify((() => {
      const v = document.querySelector('video').getBoundingClientRect();
      for (const el of document.querySelectorAll('span,div,li')) {
        if (el.children.length) continue;
        const t = (el.textContent||'').replace(/\s+/g,'').toUpperCase();
        if (!/^X?1\.5X?$/.test(t)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 5 || r.x < v.x - 30 || r.x > v.x + v.width + 30) continue;
        if (r.y < v.y - 30 || r.y > v.y + v.height + 30) continue;
        return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
      }
      return null;
    })())""", wait=False)
    item = json.loads(item) if isinstance(item, str) else item
    if not item:
        return "悬浮后菜单未展开"
    mouse("mouseMoved", item["x"], item["y"]); time.sleep(0.5)
    mouse("mousePressed", item["x"], item["y"], True); time.sleep(0.05)
    mouse("mouseReleased", item["x"], item["y"]); time.sleep(2)
    v = video_state(page) or {}
    return f"1.5x 结果: rate={v.get('rate')}"


def handle_popup(page, ticket_dir: Path, wait_answer_s: float = 300.0) -> str:
    """提取弹题 → 写工单 → 等 Agent 答案 → 真实点击提交。"""
    raw = page.eval(POPUP_JS, wait=False)
    popup = json.loads(raw) if isinstance(raw, str) else raw
    if not popup:
        return "no_popup"
    text = popup.get("text", "")

    qtype = ("judge" if ("判断题" in text or "正确" in text or re.search(r"^\s*A\s*[.、]?\s*对", text, re.M))
             else "single")
    raw_opts = page.eval(OPTIONS_JS, wait=False)
    opts = json.loads(raw_opts) if isinstance(raw_opts, str) else (raw_opts or [])
    # 去重（同字母同文本）
    seen, options = set(), []
    for o in opts:
        key = (o["letter"], o["text"])
        if key in seen:
            continue
        seen.add(key)
        options.append(o)

    ticket_id = time.strftime("%H%M%S")
    ticket = {
        "id": ticket_id, "qtype": qtype,
        "question": text[:800], "options": options,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    ticket_dir.mkdir(parents=True, exist_ok=True)
    pending = ticket_dir / f"pending_{ticket_id}.json"
    answer_file = ticket_dir / f"answer_{ticket_id}.json"
    pending.write_text(json.dumps(ticket, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[quiz] 工单已写 {pending.name} | {len(options)} 个选项", flush=True)

    # 等 Agent 作答；超时 → 暂停转人工（铁律）
    deadline = time.time() + wait_answer_s
    ans = None
    while time.time() < deadline:
        if answer_file.is_file():
            try:
                ans = json.loads(answer_file.read_text(encoding="utf-8"))
                break
            except Exception:
                pass
        time.sleep(2)
    if ans is None:
        v = video_state(page)
        if v and not v.get("paused"):
            page.eval("document.querySelector('video').pause()", wait=False)
        pending.rename(ticket_dir / f"timeout_{ticket_id}.json")
        print(f"[quiz] {wait_answer_s}s 未获答案 → 已暂停视频，转人工", flush=True)
        return "timeout"

    letters = [a.strip().upper() for a in re.split(r"[,\s，、]+", str(ans.get("answer", ""))) if a.strip()]
    print(f"[quiz] Agent 答案: {letters}", flush=True)

    # 逐个点选项（真实鼠标）
    for letter in letters:
        opt = next((o for o in options if o["letter"] == letter), None)
        if not opt:
            print(f"[quiz] 选项 {letter} 未定位到坐标", flush=True)
            continue
        click(page, opt["x"], opt["y"])
        time.sleep(0.6)
        print(f"[quiz] 已点 {letter} ({opt['text'][:20]})", flush=True)

    # 提交
    raw_sub = page.eval(SUBMIT_JS, wait=False)
    sub = json.loads(raw_sub) if isinstance(raw_sub, str) else raw_sub
    if sub:
        click(page, sub["x"], sub["y"])
        print("[quiz] 已点提交作答", flush=True)
        time.sleep(3)

    # 关闭可能残留的弹窗（右上角 X）
    close = page.eval(r"""JSON.stringify((() => {
      const vis = (el) => { const r = el.getClientRects(); return r.length > 0; };
      for (const el of document.querySelectorAll('[class*=close], .el-dialog__headerbtn, button')) {
        if (!vis(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width > 8 && r.width < 60 && r.height > 8 && r.height < 60) {
          return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};
        }
      }
      return null;
    })())""", wait=False)
    close = json.loads(close) if isinstance(close, str) else close
    if close:
        click(page, close["x"], close["y"])
        time.sleep(1.5)

    # 恢复播放
    v = video_state(page)
    if v and v.get("paused"):
        page.eval("document.querySelector('video').play()", wait=False)
        time.sleep(1)
    pending.rename(ticket_dir / f"done_{ticket_id}.json")
    return f"answered {letters}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-min", type=int, default=15, help="总运行时长上限")
    ap.add_argument("--wait-answer", type=int, default=300, help="工单等待 Agent 作答的秒数")
    ap.add_argument("--speed", action="store_true", help="尝试通过真实 UI 设 1.5 倍速")
    ap.add_argument("--visible", action="store_true",
                    help="窗口显示在屏幕上（默认 offscreen；需要人工点弹窗时用）")
    args = ap.parse_args()

    port = 9334
    plan, proc = B.launch(background=None if args.visible else "offscreen")
    dl = time.time() + 40
    while time.time() < dl:
        if B.probe_cdp(port).alive:
            break
        time.sleep(1)
    print(f"[boot] offscreen 实例 port={port} 防节流旗标已加", flush=True)

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
            d = json.loads(page.eval(STATE_JS, wait=False) or "{}")
            if "login.zhihuishu.com" in d.get("url", ""):
                print(">> 会话失效，退出", flush=True)
                return 1
            if d.get("n", 0) > 50:
                ready = True
                break
        if not ready:
            print(">> 课程页超时", flush=True)
            return 1
        print("[boot] 课程页就绪", flush=True)

        rect = json.loads(page.eval(r"""JSON.stringify((() => {
          const el = document.querySelector('.child-info.hasvideo');
          const r = el.getBoundingClientRect();
          return {x: r.x + r.width/2, y: r.y + r.height/2};
        })())""", wait=False))
        click(page, rect["x"], rect["y"])
        print("[play] 已点击任务点", flush=True)

        v = None
        for _ in range(40):
            time.sleep(2)
            v = video_state(page)
            if v and v.get("dur"):
                break
        if not (v and v.get("dur")):
            print(">> 播放器未就绪", flush=True)
            return 1
        page.eval("document.querySelector('video').muted = true", wait=False)
        print(f"[play] 播放中 dur={v['dur']:.0f}s cur={v['cur']:.1f}s (已静音)", flush=True)

        if args.speed:
            print("[speed]", set_speed_via_ui(page), flush=True)

        # 主循环：监视弹题 + 周期性报告进度
        t0 = time.time()
        last_report = 0.0
        handled = 0
        while time.time() - t0 < args.limit_min * 60:
            time.sleep(3)
            v = video_state(page)
            now = time.time()
            if now - last_report > 60:
                last_report = now
                print(f"[tick] cur={v['cur']:.0f}/{v['dur']:.0f}s paused={v['paused']}", flush=True)
            if v.get("cur", 0) > 3:
                # 弹题会主动暂停视频，所以不能以 paused 为前提过滤
                r = handle_popup(page, ANSWER_DIR, wait_answer_s=args.wait_answer)
                if r.startswith("answered"):
                    handled += 1
                    print(f"[quiz] 第 {handled} 题处理完成", flush=True)
                elif r == "timeout":
                    print("[quiz] 超时转人工，脚本退出", flush=True)
                    return 2
        print(f"[done] {args.limit_min} 分钟到，共处理 {handled} 题暂停", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
