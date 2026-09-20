# -*- coding: utf-8 -*-
"""真人操作基线录制（v2 · 零注入 · 纯 CDP 被动监听）。

## 为什么重写 v1

v1 用 API_RECORDER_JS 在页面里 monkey-patch 了 fetch/XHR 并留下 __ZHS__
全局变量 —— 2026-09-20 实测被平台视频页的脚本完整性检测抓到，弹
「异常行为提示：检测到异常脚本」。页面内注入在视频页已被平台识别，弃用。

## v2 原理

- Network 事件在**浏览器进程**里产生，页面 JS 看不见，无法被检测；
- 对页面零写入：不注入脚本、不改函数、不留全局变量；
- 唯一页面交互：每 2 秒读一次 video 状态（Runtime.evaluate，不留痕迹）
  与可选的 cookies_restore（写 cookie 不属于脚本注入）。

## 为什么必须独立监听连接

CdpClient.call() 收响应时会**丢弃**夹在中间的事件帧（cdp.py 注释
「事件通知，跳过」）—— 之前在同一连接上混用 call + 事件监听，事件全被
call 吃掉，表现为"收到 0 条"。v2 开两条连接：
  监听连接：Network.enable 后只跑收帧循环；
  控制器连接（_PageSession）：导航 / 求值 / 鼠标事件。

用法：
    python scripts/record_manual_baseline.py [--seconds 120] [--restore]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import browser as B
from orchestrator import cdp as cdp_mod
from orchestrator.adapters.zhs_browser import _PageSession
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
    v: v ? {dur: v.duration, cur: v.currentTime, paused: v.paused} : null,
  };
})())"""


class EventListener:
    """独立 CDP 连接：只收事件帧，绝不混用 call。

    独立的意义：call() 会丢事件帧（见模块注释），所以监听连接上
    不发任何命令 —— Network.enable 在收帧之前发一次，之后纯 recv。
    """

    def __init__(self, ws_url: str, timeout_s: float = 600.0):
        self._c = cdp_mod.CdpClient(ws_url, timeout_s).connect()
        self.rows: dict[str, dict] = {}      # requestId -> 摘要
        self._next_id = 0

    def enable_network(self) -> None:
        self._next_id += 1
        self._c._ws.send_text(json.dumps(
            {"id": self._next_id, "method": "Network.enable"}))
        # 喝掉 enable 的响应帧
        deadline = time.time() + 5
        while time.time() < deadline:
            raw = self._c._ws.recv_message()
            if raw and f'"id":{self._next_id}' in raw.replace(" ", ""):
                return

    def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            try:
                raw = self._c._ws.recv_message()
            except Exception:
                return  # 超时/断开：本片收帧结束
            if not raw:
                continue
            try:
                m = json.loads(raw)
            except Exception:
                continue
            meth = m.get("method", "")
            p = m.get("params") or {}
            if meth == "Network.requestWillBeSent":
                rid = p.get("requestId", "")
                req = p.get("request", {})
                self.rows[rid] = {
                    "ts": time.time(),
                    "method": req.get("method", ""),
                    "status": None,
                    "url": req.get("url", ""),
                }
            elif meth == "Network.responseReceived":
                rid = p.get("requestId", "")
                resp = p.get("response", {})
                if rid in self.rows:
                    self.rows[rid]["status"] = resp.get("status")
                else:
                    self.rows[rid] = {
                        "ts": time.time(), "method": "",
                        "status": resp.get("status"), "url": resp.get("url", ""),
                    }

    def snapshot(self) -> list[dict]:
        return [
            {"method": r["method"], "status": r["status"], "url": r["url"]}
            for r in sorted(self.rows.values(), key=lambda x: x["ts"])
        ]

    def close(self) -> None:
        try:
            self._c.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=120, help="录制时长")
    ap.add_argument("--restore", action="store_true",
                    help="先恢复落盘 cookie（会话失效时用）")
    args = ap.parse_args()

    if not B.probe_cdp().alive:
        print(">> 未检测到独立浏览器。请先双击「启动独立浏览器.cmd」，再运行本脚本。")
        return 1
    port = B.debug_port()
    print(f"[attach] CDP 在线 (port={port})")

    with _PageSession(port) as page:
        page.enable_page()

        if args.restore:
            store = CookieStore(root=Path("accounts"))
            jar = store.load("acc_01")
            if jar:
                target = [c.to_dict() for c in jar
                          if any(c.domain.lstrip(".").endswith(s) for s in ZHS_DOMAIN_SUFFIXES)]
                ok, errs = cdp_mod.set_all_cookies(port, target)
                print(f"[session] cookie 恢复 {ok}/{len(target)}",
                      ("| " + str(errs[:2])) if errs else "")

        page.navigate(URL)
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline:
            d = json.loads(page.eval(STATE_JS, wait=False) or "{}")
            if "login.zhihuishu.com" in d.get("url", ""):
                print(">> 会话失效。加 --restore 重跑，或在窗口里扫码登录后重跑。")
                return 1
            if d.get("childMain", 0) > 50:
                ready = True
                break
            time.sleep(2)
        if not ready:
            print(">> 课程页加载超时（若弹了滑块验证，请手动完成后重跑）")
            return 1
        print("[page] 课程页就绪 —— 本脚本对页面零注入、纯被动监听")

        target = cdp_mod.pick_page_target(port, timeout_s=5)
        listener = EventListener(str(target["webSocketDebuggerUrl"]))
        listener.enable_network()

        print()
        print(">>> 现在请你手动点击第一节视频「0.1 博大精深的数学文化」，")
        print(f">>> 正常观看/拖动均可。录制 {args.seconds} 秒……")
        print()

        states: list[dict] = []
        t0 = time.time()
        while time.time() - t0 < args.seconds:
            listener.pump(0.8)
            try:
                s = json.loads(page.eval(STATE_JS, wait=False) or "{}")
                states.append(s)
                v = s.get("v")
                if v and v.get("dur"):
                    print(f"[{int(time.time()-t0):3d}s] dur={v['dur']:.0f}s "
                          f"cur={v['cur']:.1f}s paused={v['paused']}")
            except Exception:
                pass

        listener.close()

        reqs = listener.snapshot()
        out = Path("runs/manual_baseline.json")
        out.write_text(json.dumps({
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "mode": "cdp-passive-no-injection",
            "states": states,
            "requests": reqs,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[out] {out}  (states={len(states)}, requests={len(reqs)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
