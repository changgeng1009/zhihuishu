# -*- coding: utf-8 -*-
"""V5 批量跑课：枚举全部视频任务点，逐个播放到完（像 cx 的 run_chapter）。

身份模型（2026-09-20 定稿）：
  平台给每个任务条目稳定 id（DOM: <div class="child-info hasvideo" id="part1000129959">）。
  定位、断点、完成证据全部用 part id —— 标题后缀（未练习/掌握度N%/免考）会漂移，
  不能做身份。

完成证据（平台侧）：
  条目首子元素出现 .el-progress（Element-Plus 进度环），aria-valuenow = 观看百分比。
  截图实证：0.1 完成态 = 绿色对勾；1.2.2 观看中 = 蓝色环。
  aria-valuenow >= 100 才计 confirmed；其余一律待确认，绝不凭「播完」猜。

流程（每个任务点）：
  按 part id 点击 → 等元数据 → 静音 → 真实 UI 设 1.5x → 循环监视：
    - 弹题（handle_popup 工单制；超时 240s → 暂停转人工并停止批量）
    - 播完（cur >= dur - 1.5）→ 读进度环 → confirmed/unconfirmed
  每讲播完 Page.reload 换新页（播完的页面播放器会进错误态，点击只挪高亮不加载；
  实测 navigate 同 URL 不触发重载，reload 才有效）。

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
from orchestrator import ticket_store as ts_module
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


def _hide_noise(page) -> None:
    """隐藏「课程提醒」「学前必读」等遮挡弹窗（只 display:none 不点击，
    避开 btn01 滑块验证码坑）。弹窗渲染比列表慢，需在点击前反复执行。"""
    page.eval(r"""(()=>{for (const sel of ['.el-dialog__wrapper','.el-overlay','.el-dialog']) {
      for (const el of document.querySelectorAll(sel)) el.style.display = 'none';
    }})()""", wait=False)


def enumerate_tasks(page) -> list[dict]:
    """枚举左侧列表的全部视频任务点（滚动加载全覆盖）。

    返回 [{id: part id, t: 展示文本, pct: 平台进度(0-100 int | None)}]。
    """
    page.eval(r"""(() => {
      const box = document.querySelector('.left-aside, .chapter-list, [class*=list]');
      if (box) box.scrollTop = 0;
    })()""", wait=False)
    seen: dict[str, dict] = {}
    for _ in range(14):                      # 逐步滚动逼出懒加载条目
        raw = page.eval(r"""JSON.stringify((() => {
          const out = [];
          document.querySelectorAll('.child-info.hasvideo').forEach(el => {
            if (!el.id || !el.id.startsWith('part')) return;
            const p = el.querySelector(':scope > .el-progress');
            const pct = p ? parseInt(p.getAttribute('aria-valuenow') || '', 10) : NaN;
            out.push({id: el.id,
                      t: (el.textContent||'').replace(/\s+/g,' ').trim().slice(0,40),
                      pct: isNaN(pct) ? null : pct});
          });
          return out;
        })())""", wait=False)
        for it in (json.loads(raw) if isinstance(raw, str) else raw) or []:
            seen.setdefault(it["id"], it)    # part id 去重
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


def _task_coords(page, part_id: str) -> dict | None:
    """按 part id 滚动到条目可见处，返回可点坐标（须在视口内）。"""
    page.eval(
        "(() => { const el = document.getElementById(T);"
        "if (el) el.scrollIntoView({block: 'center'}); })()".replace(
            "T", json.dumps(part_id), 1),
        wait=False,
    )
    time.sleep(0.7)
    raw = page.eval(
        r"""JSON.stringify((() => {
      const el = document.getElementById(T);
      if (!el) return null;
      const target = el.firstElementChild || el;   // 实测点内层才生效
      const r = target.getBoundingClientRect();
      if (r.y < 0 || r.y > innerHeight - 10 || r.width < 5) return null;  // 视口外
      return {x: Math.round(r.x + Math.min(r.width/2, 90)), y: Math.round(r.y + r.height/2)};
    })())""".replace("T)", json.dumps(part_id) + ")", 1),
        wait=False,
    )
    return json.loads(raw) if isinstance(raw, str) else raw


def _task_pct(page, part_id: str, wait_s: float = 12.0) -> int | None:
    """读平台侧该条目的进度环数值（0-100）。读不到返回 None。

    这是完成判定的唯一平台证据：>=100 才算平台认可完成。
    """
    deadline = time.time() + wait_s
    while time.time() < deadline:
        raw = page.eval(
            r"""JSON.stringify((() => {
          const el = document.getElementById(T);
          if (!el) return null;
          const p = el.querySelector(':scope > .el-progress');
          if (!p) return -1;                     // 无进度环 = 平台未记录
          const n = parseInt(p.getAttribute('aria-valuenow') || '', 10);
          return isNaN(n) ? -1 : n;
        })())""".replace("T)", json.dumps(part_id) + ")", 1), wait=False)
        try:
            v = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            v = None
        if isinstance(v, int) and v >= 0:
            return v
        time.sleep(2)
    return None


def _load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}                     # 状态损坏退化为全量重跑（不跳过任何任务）


def _save_state(path: Path, state: dict) -> None:
    ts_module.atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2))


def _refresh_page(page) -> bool:
    """硬刷新学习页并等列表渲染。

    实测：Page.navigate 到相同 URL 不触发真正重载（SPA 原地不动，旧播放器
    状态全保留）；Page.reload 才是有效恢复。
    """
    client = getattr(page, "_client", None)
    if client is not None:
        client.call("Page.enable", {})
        client.call("Page.reload", {"ignoreCache": True})
    else:
        page.navigate(URL)
    dl = time.time() + 40
    while time.time() < dl:
        time.sleep(2)
        d = page.eval("JSON.stringify({url: location.href, n: document.querySelectorAll('.child-info.hasvideo').length})",
                      wait=False)
        try:
            d = json.loads(d) if isinstance(d, str) else d
        except Exception:
            d = {}
        if isinstance(d, dict) and d.get("n", 0) > 50 and "login" not in d.get("url", ""):
            _hide_noise(page)          # 弹窗此刻可能才刚渲染
            time.sleep(1)
            _hide_noise(page)
            return True
    return False


def _click_until_loaded(page, task: dict, prev_dur: float | None,
                        attempts: int = 3) -> tuple[bool, dict]:
    """按 part id 点击并等视频元数据切换。返回 (loaded, 最后一次 video_state)。"""
    expected = _expected_dur(task["t"])
    v: dict = {}
    for attempt in range(attempts):
        _hide_noise(page)
        coords = _task_coords(page, task["id"])
        if not coords:
            time.sleep(2)
            continue
        click(page, coords["x"], coords["y"])
        for _ in range(20):                       # 最多 40s 等 loader
            time.sleep(2)
            v = video_state(page) or {}
            dur, cur = v.get("dur"), v.get("cur", 0)
            if not dur:
                continue
            if expected and abs(dur - expected) < 6:
                return True, v
            if not expected and (prev_dur is None or abs(dur - prev_dur) > 6 or cur < 30):
                return True, v
        print(f"    [warn] 第 {attempt+1} 次点击后视频未切换（dur={v.get('dur')}）", flush=True)
    return False, v


def play_one(page, task: dict, wait_answer_s: int, prev_dur: float | None,
             stats: dict) -> tuple[str, float | None]:
    """播一个任务点到结束。返回 (confirmed/unconfirmed/timeout/skip, 视频时长)。

    完成计数分桶（stats）：confirmed（进度环 >=100）/ unconfirmed（播完但
    平台进度不足）/ skipped / failed —— 后两者绝不计入成功。
    """
    loaded, v = _click_until_loaded(page, task, prev_dur)
    if not loaded:
        # 播完的页面播放器会进错误态（点击只挪高亮不加载）。
        # 恢复 = Page.reload 硬刷新后再试一轮（实测 reload 后点击稳定）。
        print("    [warn] 点击不切换 → Page.reload 后重试", flush=True)
        if _refresh_page(page):
            loaded, v = _click_until_loaded(page, task, prev_dur)
    if not loaded:
        stats["skipped"] += 1
        return "skip", v.get("dur") if v else prev_dur

    page.eval("document.querySelector('video').muted = true", wait=False)
    print(f"    [play] dur={v['dur']:.0f}s cur={v['cur']:.0f}s（已静音）", flush=True)
    if v.get("cur", 0) >= v["dur"] - 10:
        # 播放器续在末尾：只认进度环证据
        pct = _task_pct(page, task["id"])
        print(f"    [note] 续在末尾，平台进度={pct}%", flush=True)
        if pct is not None and pct >= 100:
            stats["confirmed"] += 1
            return "confirmed", v["dur"]
        stats["unconfirmed"] += 1
        return "unconfirmed", v["dur"]

    if v.get("cur", 0) < v["dur"] - 5:             # 从头看的才设倍速
        print("    [speed]", set_speed_via_ui(page), flush=True)

    # 播放到结束；弹题工单制
    while True:
        time.sleep(2)
        v = video_state(page)
        if not v:
            break                                  # 播完元素销毁 → 走完成核验
        if v.get("dur") and v.get("cur", 0) >= v["dur"] - 1.5:
            break
        r = handle_popup(page, ANSWER_DIR, wait_answer_s=wait_answer_s,
                         course_id="1000007974", chapter_id=task["id"])
        if r["status"] == "timeout":
            stats["failed"] += 1
            return "timeout", v.get("dur")
        if r["status"] in ("answered", "already_submitted"):
            if r.get("verified"):
                stats["quizzes_ok"] += 1
            else:
                stats["quizzes_unverified"] += 1
            print(f"    [quiz] {r['status']} verified={r['verified']} {r['detail']}", flush=True)
            if r["status"] == "answered":          # already_submitted 已在内部恢复
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

    # ── 完成核验：播完 ≠ 成功，以进度环为准 ──
    pct = _task_pct(page, task["id"], wait_s=15.0)
    print(f"    [note] 播完，平台进度={pct}%", flush=True)
    if pct is not None and pct >= 100:
        stats["confirmed"] += 1
        return "confirmed", v["dur"] if v else None
    stats["unconfirmed"] += 1
    return "unconfirmed", v["dur"] if v else None


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
        done_now = [t for t in tasks if (t.get("pct") or 0) >= 100]
        print(f"[batch] 视频任务点 {len(tasks)} 个（平台进度>=100 的 {len(done_now)} 个）", flush=True)
        stats = {"confirmed": 0, "unconfirmed": 0, "skipped": 0, "failed": 0,
                 "quizzes_ok": 0, "quizzes_unverified": 0, "skipped_confirmed": 0}
        state_path = Path("runs/batch_state.json")
        state = _load_state(state_path)
        # 断点：跳过 平台进度>=100 或 state=confirmed 的；其余重跑
        todo = []
        for task in tasks:
            if (task.get("pct") or 0) >= 100:
                print(f"[batch] 跳过（平台进度100）{task['t'][:36]}", flush=True)
                stats["skipped_confirmed"] += 1
                continue
            if state.get(task["id"], {}).get("status") == "confirmed":
                print(f"[batch] 跳过（本地已确认）{task['t'][:36]}", flush=True)
                stats["skipped_confirmed"] += 1
                continue
            todo.append(task)
        print(f"[batch] 待跑 {len(todo)}（跳过已完成 {len(tasks)-len(todo)}）", flush=True)

        prev_dur: float | None = None
        t0 = time.time()
        for i, task in enumerate(todo):
            if time.time() - t0 > args.limit_min * 60:
                print(f"[batch] 到达时限", flush=True)
                break
            print(f"[batch] ({i+1}/{len(todo)}) {task['t'][:36]}（平台进度 {task.get('pct')}%）", flush=True)
            r, prev_dur = play_one(page, task, args.wait_answer, prev_dur, stats)
            tid = task["id"]
            if r == "confirmed":
                state[tid] = {"status": "confirmed", "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                _save_state(state_path, state)
            elif r == "unconfirmed":
                state[tid] = {"status": "unconfirmed", "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                _save_state(state_path, state)     # 待确认也记录：下轮重跑复核
            if r == "timeout":
                state[tid] = {"status": "failed", "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                _save_state(state_path, state)
                print("[batch] 弹题超时转人工，批量停止", flush=True)
                _summary(stats)
                return 2
            print(f"[batch] ✓ {r}（确认 {stats['confirmed']} / 待确认 {stats['unconfirmed']}）",
                  flush=True)
            if r in ("confirmed", "unconfirmed", "skip"):
                # 主动换新页：播完的页面播放器已死，直接点下一讲必失败
                if not _refresh_page(page):
                    print("[batch] 页面刷新失败，停止（下次启动会重新拉起）", flush=True)
                    _summary(stats)
                    return 1
        _summary(stats)
    return 0


def _summary(stats: dict) -> None:
    print("[batch] 统计："
          f"确认完成 {stats['confirmed']} ｜ 待确认 {stats['unconfirmed']} ｜ "
          f"跳过 {stats['skipped']} ｜ 失败 {stats['failed']} ｜ "
          f"弹题✓ {stats['quizzes_ok']} ｜ 弹题待确认 {stats['quizzes_unverified']} ｜ "
          f"跳过(已完成) {stats['skipped_confirmed']}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
