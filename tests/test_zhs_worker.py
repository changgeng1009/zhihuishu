"""worker 胶水层测试。

worker 的价值在于"把上游的中文日志翻译成机器可读事件"，所以测试重点在
**解析函数**（纯函数，完全可离线断言）与**协议错误路径**。
真实的视频播放必须由使用者手工冒烟（需要账号），见 docs/04。
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from orchestrator.adapters import zhs_worker as w


class TestParseLine(unittest.TestCase):
    def test_progress(self):
        event = w._parse_line("当前进度 42.5% 继续播放")
        self.assertIsNotNone(event)
        self.assertEqual(event["event"], "progress")
        self.assertEqual(event["percent"], 42.5)

    def test_chapter(self):
        event = w._parse_line("章节：第一章 绪论")
        self.assertIsNotNone(event)
        self.assertEqual(event["event"], "chapter")
        self.assertEqual(event["chapter"], "第一章 绪论")

    def test_risk_marker(self):
        event = w._parse_line("操作过于频繁，请稍后再试")
        self.assertIsNotNone(event)
        self.assertEqual(event["event"], "risk_marker")

    def test_auth_marker(self):
        event = w._parse_line("请先登录后再继续")
        self.assertIsNotNone(event)
        self.assertEqual(event["event"], "auth_marker")

    def test_stall_marker(self):
        event = w._parse_line("视频进度长时间无推进，已停止")
        self.assertIsNotNone(event)
        self.assertEqual(event["event"], "stall_detected")

    def test_course_finished(self):
        event = w._parse_line("本课程学习完成")
        self.assertIsNotNone(event)
        self.assertEqual(event["event"], "course_finished")

    def test_unrecognized_returns_none(self):
        """解析必须**容错**：认不出就返回 None，绝不抛异常。

        上游改文案时应该降级为"能报进度但不报细节"，而不是把整门课跑崩。
        """
        self.assertIsNone(w._parse_line("随便一句无关的话"))
        self.assertIsNone(w._parse_line(""))
        self.assertIsNone(w._parse_line("   "))

    def test_risk_takes_precedence_over_progress(self):
        event = w._parse_line("操作过于频繁 当前进度 10%")
        self.assertEqual(event["event"], "risk_marker")


class TestClassifyExit(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(w.EXIT_OK, w._classify_exit(0, ""))

    def test_auth_output_maps_to_auth(self):
        self.assertEqual(w.EXIT_AUTH, w._classify_exit(1, "登录失败：账号或密码错误"))

    def test_risk_output_does_not_map_to_auth(self):
        """风控不能被误判成认证失败——两者的处置完全不同（熔断 vs 重登）。"""
        self.assertEqual(w.EXIT_INTERNAL, w._classify_exit(1, "操作过于频繁"))

    def test_generic_failure_is_internal(self):
        self.assertEqual(w.EXIT_INTERNAL, w._classify_exit(1, "未知错误"))


class TestProtocolErrors(unittest.TestCase):
    def _run_main(self, stdin_text: str) -> tuple[int, dict]:
        buf = io.StringIO()
        original = w.sys.stdin
        w.sys.stdin = io.StringIO(stdin_text)
        try:
            with redirect_stdout(buf):
                code = w.main([])
        finally:
            w.sys.stdin = original
        out = [line for line in buf.getvalue().splitlines() if line.strip()]
        payload = json.loads(out[-1]) if out else {}
        return code, payload

    def test_empty_stdin_fails(self):
        code, payload = self._run_main("")
        self.assertEqual(code, w.EXIT_INTERNAL)
        self.assertFalse(payload["ok"])

    def test_invalid_json_fails(self):
        code, payload = self._run_main("这不是 JSON\n")
        self.assertEqual(code, w.EXIT_INTERNAL)
        self.assertIn("JSON", payload["hint"])

    def test_unknown_op_fails(self):
        code, payload = self._run_main(json.dumps({"op": "nope"}) + "\n")
        self.assertEqual(code, w.EXIT_INTERNAL)
        self.assertIn("未知 op", payload["hint"])

    def test_check_course_without_url_fails(self):
        code, payload = self._run_main(json.dumps({"op": "check_course", "args": {}}) + "\n")
        self.assertEqual(code, w.EXIT_INTERNAL)
        self.assertIn("url", payload["hint"])

    def test_dry_run_video_does_not_touch_upstream(self):
        """dry-run 必须真的不产生写操作，且不依赖上游存在。"""
        code, payload = self._run_main(
            json.dumps(
                {
                    "op": "run_video",
                    "args": {"course_url": "https://studyvideoh5.zhihuishu.com/x", "dry_run": True},
                }
            )
            + "\n"
        )
        self.assertEqual(code, w.EXIT_OK)
        self.assertTrue(payload["data"]["dry_run"])
        self.assertEqual(payload["data"]["would_run"], "Autovisor")


class TestWriteConfig(unittest.TestCase):
    def test_config_is_written_to_workdir_not_upstreams(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "accounts" / "acc_01"
            path = w._write_config(workdir, "https://studyvideoh5.zhihuishu.com/x", speed=99)
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent, workdir)
            text = path.read_text(encoding="utf-8")
            self.assertIn("URL1 = https://studyvideoh5.zhihuishu.com/x", text)
            # 倍速被夹到平台上限
            self.assertIn("limitSpeed = 1.8", text)
            # 上游目录名不得出现在配置里
            self.assertNotIn("upstreams", text)

    def test_no_donate_code_by_default(self):
        """运行期产物应当干净：默认关掉赞赏码，避免污染 stdout 解析。"""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = w._write_config(Path(tmp), "https://x/y")
            self.assertIn("showDonateCode = False", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
