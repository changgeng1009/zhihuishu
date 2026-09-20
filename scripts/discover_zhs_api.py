"""一次性发现脚本：找出智慧树"我的课程"用的是哪个接口。

## 为什么需要它

OCS（`upstreams/ocsjs`）的 `zhs.ts` **不调用课程列表接口** —— 它是
"给一个课程链接就跑"的设计，课程列表从来不是它的输入。
Autovisor 同理。所以 `list_courses`（C06）在智慧树上**没有现成的上游可复用**，
必须自己找到平台的接口。

## 做法

在**页面加载前**注入一个 fetch/XHR 记录器（`Page.addScriptToEvaluateOnNewDocument`），
然后导航到学习首页，最后把记录到的请求 URL dump 出来。

为什么不直接用 HTTP 客户端去猜接口：智慧树的 XHR 多半带签名与
反爬参数，而且是同源 cookie 认证。**在已登录的页面里发同源请求**
天然带齐一切 —— 这也是本项目"读侧必须走浏览器"的原因。

用法：
    python scripts/discover_zhs_api.py --url https://onlineweb.zhihuishu.com/onlinestuh5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator import cdp  # noqa: E402

#: 在页面加载前注入。挂在 window.__REQ__ 上，只记录不改行为。
#:
#: 同时记录**响应体**：智慧树的 XHR 带 `secretStr` + `date` 签名参数，
#: 逆向签名算法既脆弱又没必要 —— 让页面自己发请求、我们读它的响应，
#: 天然带齐 cookie 与签名，且平台改算法也不影响我们。
RECORDER_JS = r"""
(() => {
  if (window.__REQ__) return;
  const list = [];
  window.__REQ__ = list;

  const INTERESTING = [
    "getCourseList", "getNoConfirmCourseList", "queryStudentAICourseList",
    "queryShareCourseInfo", "queryMicroCourseInfo", "queryStudentSchoolCourseList",
    "querySelectCourseInfo", "getCourseDetail", "getChapter", "getCoursePoint",
  ];
  const want = (url) => INTERESTING.some((k) => String(url).includes(k));

  const store = (kind, method, url, body, status, data) => {
    try {
      list.push({
        kind, method: String(method || "GET").toUpperCase(), url: String(url || ""),
        body: String(body || "").slice(0, 300),
        status, ts: Date.now(),
        data: data === undefined ? null : data,
      });
    } catch (e) {}
  };

  const parse = (text) => { try { return JSON.parse(text); } catch (e) { return null; } };

  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const p = origFetch.apply(this, arguments);
      if (!want(url)) return p;
      return p.then((resp) => {
        try {
          resp.clone().text().then((t) => store("fetch", (init && init.method) || "GET", url, init && init.body, resp.status, parse(t)));
        } catch (e) {}
        return resp;
      });
    };
  }

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__m = method; this.__u = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const xhr = this;
    if (want(xhr.__u)) {
      xhr.addEventListener("load", () => {
        let data = null;
        try { data = parse(xhr.responseText); } catch (e) {}
        store("xhr", xhr.__m, xhr.__u, body, xhr.status, data);
      });
    }
    return origSend.apply(this, arguments);
  };
})();
"""


def _page_client(port: int) -> cdp.CdpClient:
    target = cdp.pick_page_target(port, timeout_s=6)
    ws = target.get("webSocketDebuggerUrl")
    if not ws:
        raise SystemExit("页面目标缺少 webSocketDebuggerUrl")
    return cdp.CdpClient(ws_url=ws).connect()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="发现智慧树的课程列表接口")
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--url", default="https://onlineweb.zhihuishu.com/onlinestuh5")
    ap.add_argument("--wait", type=float, default=9.0, help="导航后等待秒数")
    ap.add_argument("--out", default=None, help="把结果写到这个 json 文件")
    args = ap.parse_args(argv)

    client = _page_client(args.port)
    try:
        client.call("Page.enable")
        client.call("Runtime.enable")
        client.call(
            "Page.addScriptToEvaluateOnNewDocument", {"source": RECORDER_JS}
        )
        print(f"[navigate] {args.url}")
        client.call("Page.navigate", {"url": args.url})
        time.sleep(args.wait)

        result = client.call(
            "Runtime.evaluate",
            {
                "expression": "JSON.stringify({href: location.href, title: document.title, req: window.__REQ__ || []})",
                "returnByValue": True,
            },
        )
        payload = result.get("result", {}).get("value")
        data = json.loads(payload) if payload else {}
    finally:
        client.close()

    print(f"\n[landed] {data.get('title')}  {data.get('href')}")
    reqs = data.get("req") or []
    print(f"[requests] 共记录 {len(reqs)} 条 | 带响应体的 {sum(1 for r in reqs if r.get('data') is not None)} 条\n")

    # ---- 1. 接口清单 ----
    keywords = (
        "course", "recruit", "list", "my", "student", "learn", "study",
        "catalog", "class", "term", "select",
    )
    interesting = [r for r in reqs if any(k in r["url"].lower() for k in keywords)]
    for r in interesting:
        got = "有响应体" if r.get("data") is not None else ""
        print(f"  {r['kind']:5} {r['method']:4} {r['status']} {got:6} {r['url'][:130]}")

    if not interesting:
        print("  （没有命中关键词，下面是全部请求的 URL 前缀）")
        seen: list[str] = []
        for r in reqs:
            base = r["url"].split("?")[0]
            if base not in seen:
                seen.append(base)
        for base in seen:
            print("   ", base[:150])

    # ---- 2. 响应体形状 ----
    def shape(value, depth: int = 0) -> str:
        if depth > 3:
            return "..."
        if isinstance(value, dict):
            if not value:
                return "{}"
            items = list(value.items())[:12]
            inner = ", ".join(f"{k}: {shape(v, depth + 1)}" for k, v in items)
            more = f", …(+{len(value) - len(items)})" if len(value) > len(items) else ""
            return "{" + inner + more + "}"
        if isinstance(value, list):
            if not value:
                return "[]"
            return f"[{len(value)}× {shape(value[0], depth + 1)}]"
        if isinstance(value, str):
            return f"str({value[:24]!r})" if value else "str('')"
        return type(value).__name__

    print("\n[响应体形状]")
    for r in reqs:
        if r.get("data") is None:
            continue
        path = r["url"].split("?")[0].split("/")[-1]
        print(f"\n  ── {r['method']} {path}")
        print(f"     {shape(r['data'])[:600]}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[written] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
