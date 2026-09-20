"""本地 CDP 通道必须绕过 HTTP 代理 —— 回归测试。

## 为什么值得单独一个测试文件

本机环境（沙箱 / 公司网络 / 抓包工具）常设
`HTTP_PROXY=http://127.0.0.1:xxxx`，而 `urllib.request.urlopen`
**默认会把这条代理用在 127.0.0.1 的请求上**。

后果极具误导性：本地 CDP 端点被代理劫持，返回 `502 Bad Gateway` 或超时，
看起来像"浏览器根本没起来"，实际浏览器活得好好的，只是我们自己的请求走错了路。

2026-09-20 实测踩到：`cli browser --status` 报 `alive: false`，
而真实原因是 `HTTP_PROXY` 没被绕开。

所以这里用一个**真实的本地 HTTP 服务器** + **一个故意打不通的代理**来验证：
绕开代理时能拿到数据，不绕开时必然失败。
"""

from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from orchestrator import cdp


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib 约定
        body = json.dumps(
            {
                "Browser": "Edg/153.0.4234.32",
                "Protocol-Version": "1.3",
                "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/fake",
                "path": self.path,
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音服务器日志
        return


class _Server:
    def __enter__(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False


#: 一个必然连不上的代理（保留端口，不会有人监听）
DEAD_PROXY = "http://127.0.0.1:1"

PROXY_ENV = {
    "HTTP_PROXY": DEAD_PROXY,
    "http_proxy": DEAD_PROXY,
    "HTTPS_PROXY": DEAD_PROXY,
    "https_proxy": DEAD_PROXY,
}


class TestLoopbackBypassesProxy(unittest.TestCase):
    def test_http_json_succeeds_despite_dead_proxy(self):
        """设了打不通的代理，`http_json` 仍然必须能访问本地 CDP。"""
        with _Server() as server, mock.patch.dict(os.environ, PROXY_ENV):
            payload = cdp.http_json(server.port, "/json/version", timeout_s=5.0)
        self.assertEqual(payload["Browser"], "Edg/153.0.4234.32")
        self.assertEqual(payload["path"], "/json/version")

    def test_control_naive_urlopen_fails(self):
        """对照：不绕代理时确实会失败 —— 证明上面的测试不是空转。

        如果这条测试开始失败（即裸 urlopen 也能成功），说明环境变了
        （比如 `NO_PROXY` 被设上了），此时本文件的绕代理逻辑仍然正确，
        但"必须绕开"这个前提需要重新评估。
        """
        with _Server() as server, mock.patch.dict(os.environ, PROXY_ENV):
            # urllib 在模块级 getproxies 缓存存在时可能不读新环境变量，
            # 因此显式构造一个会读环境变量的 opener。
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler(urllib.request.getproxies())
            )
            with self.assertRaises((urllib.error.URLError, OSError)):
                opener.open(
                    f"http://127.0.0.1:{server.port}/json/version", timeout=5.0
                ).read()

    def test_list_targets_works(self):
        with _Server() as server:
            # /json/list 返回的必须是数组；这里用同一个 handler 会返回 dict，
            # 所以只断言"不会因为代理问题抛 URLError"。
            with self.assertRaises(cdp.CdpError) as cm:
                cdp.list_targets(server.port, timeout_s=5.0)
            self.assertIn("不是数组", str(cm.exception))

    def test_empty_proxy_handler_registers_no_hooks(self):
        """绕开代理的机制：`ProxyHandler({})` 不会注册任何 `*_open` 钩子。

        `ProxyHandler.__init__` 会按 proxies 映射**动态挂上** `<scheme>_open`
        方法；映射为空时一个都不挂，于是请求直连，代理自然不参与。

        这也是为什么 `ProxyHandler({})` 不会出现在 `OpenerDirector.handlers`
        里 —— 它没有任何可注册的方法。测试直接断言这个机制，
        而不是断言"列表里有 ProxyHandler"（那反而会失败）。
        """
        bypass = urllib.request.ProxyHandler({})
        routed = urllib.request.ProxyHandler({"http": DEAD_PROXY})

        self.assertFalse(hasattr(bypass, "http_open"), "空代理表不该注册钩子")
        self.assertFalse(hasattr(bypass, "https_open"), "空代理表不该注册钩子")
        self.assertTrue(hasattr(routed, "http_open"), "有代理时才会注册钩子")

    def test_opener_reuses_single_instance(self):
        """预建复用：每次 build_opener 都要重建 handler 链，没必要。"""
        self.assertIs(cdp.loopback_opener(), cdp.loopback_opener())
        opener = cdp.loopback_opener()
        self.assertFalse(
            any(isinstance(h, urllib.request.ProxyHandler) for h in opener.handlers),
            "回环 opener 不该注册任何代理 handler",
        )


class TestProbeCdpUsesLoopbackOpener(unittest.TestCase):
    def test_probe_cdp_online_uses_bypass(self):
        from orchestrator import browser

        with _Server() as server, mock.patch.dict(os.environ, PROXY_ENV):
            status = browser.probe_cdp(server.port, timeout_s=5.0)
        self.assertTrue(status.alive, f"探活失败：{status.detail}")
        self.assertIn("Edg/153", status.browser)


if __name__ == "__main__":
    unittest.main()
