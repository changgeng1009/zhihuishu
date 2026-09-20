# -*- coding: utf-8 -*-
"""V2 播放闭环：我们自己的 CDP 通道（不依赖 Autovisor）。

单次调用内完成「启动 → 恢复会话 → 导航 → 点当前任务点 → 播放 →
1.5 倍速 → 测进度前进」，因为沙箱在工具调用之间回收浏览器。

用法：
    python scripts/v2_playback_own_channel.py [--seconds 30]
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
from orchestrator.adapters.zhs_js import BOOTSTRAP_JS
from orchestrator.cdp import set_all_cookies
from orchestrator.cookies import CookieStore, ZHS_DOMAIN_SUFFIXES
from orchestrator.safety import verify

URL = (
    "https://studywisdomh5.zhihuishu.com/study/index"
    "?recruitAndCourseId=4e5e50514d5a4859454a5859584250445c"
)


def launch_with_session() -> tuple[object, object]:
    """启动独立浏览器并恢复登录态。"""
    plan, proc = B.launch(background="offscreen")
    deadline = time.time() + 40
    while time.time() < deadline:
        if B.probe_cdp(plan.port).alive:
            break
        time.sleep(1)
    store = CookieStore(root=Path("accounts"))
    jar = store.load("acc_01")
    if jar:
        target = [
            c.to_dict() for c in jar
            if any(c.domain.lstrip(".").endswith(s) for s in ZHS_DOMAIN_SUFFIXES)
        ]
        ok, errs = set_all_cookies(plan.port, target)
        print(f"[session] cookie 恢复 {ok}/{len(target)}",
              ("| " + str(errs[:2])) if errs else "")
    return plan, proc


def open_study_page(page: "_PageSession", wait_list_s: float = 50.0) -> bool:
    """导航到学习页并等章节列表渲染。"""
    page.enable_page()
    page.navigate(URL)
    deadline = time.time() + wait_list_s
    while time.time() < deadline:
        d = page.eval(
            r"""JSON.stringify({url: location.href.slice(0, 90),
                 childMain: document.querySelectorAll('.child-main').length})""",
            wait=False,
        ) or "{}"
        d = json.loads(d)
        if "login.zhihuishu.com" in d.get("url", ""):
            print("[page] 被踢到登录页 —— 会话失效，需要重新 cookies_extract")
            return False
        if d.get("childMain", 0) > 50:
            print(f"[page] 列表就绪（{d['childMain']} 条目）")
            return True
        time.sleep(2)
    print("[page] 超时：章节列表未渲染")
    return False


def video_state(page: "_PageSession") -> dict:
    raw = page.eval(
        r"""JSON.stringify((() => { const v = document.querySelector('video');
              return v ? {dur: v.duration, cur: v.currentTime, paused: v.paused,
                          rate: v.playbackRate, rs: v.readyState} : {none: true};
            })())""",
        wait=False,
    )
    return json.loads(raw or "{}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=25, help="观察播放的时长")
    args = ap.parse_args()

    plan, proc = launch_with_session()

    with _PageSession(plan.port) as page:
        if not open_study_page(page):
            return 1

        # 注入 __ZHS__（closeNoiseDialogs / setSpeed 都在里面）
        cfg = json.dumps(build_page_config(URL), ensure_ascii=False)
        page.eval(f"window.__ZHS_CFG__ = {cfg}", wait=False)
        page.eval(BOOTSTRAP_JS, wait=False)
        page.eval("__ZHS__.closeNoiseDialogs()", wait=False)

        # 守卫：学习页 + 页面文本复核，通过才允许"播放"这个写操作
        verdict, reason = verify(
            page.url(), "play", page.eval("(document.body.innerText||'')", wait=False)
        )
        print(f"[guard] {verdict.name} | {reason[:60]}")
        if verdict.name != "ALLOW":
            return 1

        # 点当前任务点（与平台一致：点 .child-info.hasvideo 的第一个）
        click = page.eval(
            r"""(() => {
              const t = document.querySelector('.child-info.hasvideo');
              if (!t) return JSON.stringify({ok: false, reason: 'no_target'});
              const title = (t.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40);
              (t.firstElementChild || t).click();
              return JSON.stringify({ok: true, clicked: title});
            })()""",
            wait=False,
        )
        print("[click]", click)

        # 等播放器出现并拿到时长
        state: dict = {}
        for i in range(30):
            time.sleep(2)
            state = video_state(page)
            if state.get("dur"):
                print(f"[player] {i*2}s 就绪 dur={state['dur']:.0f}s rs={state['rs']}")
                break
        else:
            print("[player] 未出现可播放的视频：", json.dumps(state, ensure_ascii=False))
            return 1

        # 播放（fire-and-forget：play() 的 Promise 在媒体自动播放策略下可能挂起）
        page.eval("document.querySelector('video').play()", wait=False)

        # 1.5 倍速并验证
        sp = page.eval("__ZHS__.setSpeed(1.5)", wait=True)
        print("[speed]", json.dumps(sp, ensure_ascii=False))
        time.sleep(2)

        t0 = video_state(page)
        print(f"[observe] 起点 cur={t0.get('cur', 0):.1f}s rate={t0.get('rate')}")
        played = 0.0
        for i in range(args.seconds // 5):
            time.sleep(5)
            t1 = video_state(page)
            delta = t1.get("cur", 0) - t0.get("cur", 0)
            played += delta
            print(f"[observe] +{5*(i+1)}s  cur={t1.get('cur', 0):.1f}s  "
                  f"rate={t1.get('rate')}  paused={t1.get('paused')}")
            t0 = t1
        print(f">>> 观察 {args.seconds}s，进度前进约 {played:.1f}s（1.5x 理论 ≈ {args.seconds * 1.5:.0f}s）")
        return 0 if played > args.seconds * 0.8 else 2


if __name__ == "__main__":
    raise SystemExit(main())
