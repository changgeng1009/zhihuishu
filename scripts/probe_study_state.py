# -*- coding: utf-8 -*-
"""探针：摸清学习页从导航到可播放的完整状态机。

单次调用内完成「启动浏览器 → 导航 → 周期轮询」，因为沙箱在工具调用
之间会回收浏览器进程，跨调用的状态不可依赖。

轮询指标：
  - URL（是否发生 SPA 内部跳转）
  - .child-main / .child-info 条目数（章节列表是否已渲染）
  - video 元素数与状态（播放器是否已挂载）
  - iframe 数（播放器可能在 iframe 里）
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import browser as B
from orchestrator.adapters.zhs_browser import _PageSession

URL = (
    "https://studywisdomh5.zhihuishu.com/study/index"
    "?recruitAndCourseId=4e5e50514d5a4859454a5859584250445c"
)

POLL_JS = r"""
JSON.stringify((() => {
  const n = (sel) => document.querySelectorAll(sel).length;
  const v = document.querySelector('video');
  return {
    url: location.href.slice(0, 110),
    ready: document.readyState,
    childMain: n('.child-main'),
    childInfo: n('.child-info'),
    hasvideo: n('.child-info.hasvideo'),
    videos: n('video'),
    videoState: v ? {dur: Math.round(v.duration||0), cur: Math.round(v.currentTime||0),
                     paused: v.paused, rs: v.readyState} : null,
    iframes: n('iframe'),
    bodyLen: (document.body ? document.body.innerText : '').length,
  };
})())
"""


def main() -> int:
    plan, proc = B.launch(background="offscreen")
    deadline = time.time() + 40
    while time.time() < deadline:
        if B.probe_cdp(plan.port).alive:
            break
        time.sleep(1)
    print("[probe] CDP ready, port =", plan.port)

    with _PageSession(plan.port) as page:
        page.enable_page()
        page.navigate(URL)
        samples = []
        for i in range(30):  # 60s, every 2s
            try:
                d = json.loads(page.eval(POLL_JS, wait=False) or "{}")
            except Exception as exc:  # noqa: BLE001
                d = {"err": f"{type(exc).__name__}: {exc}"[:80]}
            samples.append(d)
            print(f"[{i*2:3d}s]", json.dumps(d, ensure_ascii=False))
            # 连续 3 次稳定（有任务点或有视频在播）就提前收工
            if i > 5:
                recent = samples[-3:]
                if all(s.get("childMain", 0) > 50 for s in recent):
                    print("[probe] 列表已稳定渲染，结束轮询")
                    break
            time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
