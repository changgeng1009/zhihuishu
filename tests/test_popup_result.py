# -*- coding: utf-8 -*-
"""handle_popup 结构化返回的回归测试。

背景：V4 曾返回 "answered(already)"（无空格），V5 用 split(' ', 1)[1]
消费 → IndexError，批量直接崩。修复后 handle_popup 一律返回
{"status","detail","verified"}，本文件钉死各分支的返回契约。

FakePage 有状态：点下「提交作答」坐标后，页面才进入「已提交」态 ——
与真实页面一致（done-check 与提交核验是同一表达式，只能靠状态区分）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import v4_playback_quiz as v4  # noqa: E402

PANEL_TEXT = (
    "AI随堂练习 「AI 随堂练习的表现不会影响您的课程成绩」 "
    "1、[判断题] 在近代，给文化一词下明确定义的，首推英国人类学家E．B．泰勒。 A对 B错 提交作答"
)

SUBMIT_COORDS = {"x": 840, "y": 778}
OPT_A_COORDS = (437, 185)
OPT_B_COORDS = (437, 218)


class FakeClient:
    def __init__(self, on_click):
        self.on_click = on_click

    def call(self, method, params=None, **kw):
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mousePressed":
            self.on_click(params["x"], params["y"])
        return {}


class FakePage:
    """有状态的假页面：submitted 翻转后「已提交」检查才返回 true。"""

    def __init__(self):
        self.submitted = False
        self.clicks = []
        self.client = FakeClient(self._on_click)
        self._client = self.client

    def _on_click(self, x, y):
        self.clicks.append((x, y))
        if (x, y) == (SUBMIT_COORDS["x"], SUBMIT_COORDS["y"]):
            self.submitted = True

    def eval(self, expr, wait=False):
        if "/*DONE_CHECK*/" in expr:                   # 已提交检查（含提交核验）
            return "true" if self.submitted else "false"
        if "/*POPUP_JS*/" in expr:                     # POPUP_JS：面板题面
            return json.dumps({"text": PANEL_TEXT, "x": 300, "y": 60, "w": 800, "h": 700})
        if "/*SUBMIT_JS*/" in expr:                    # SUBMIT_JS：提交按钮
            return json.dumps(SUBMIT_COORDS)
        if "/*OPTIONS_JS*/" in expr:                   # OPTIONS_JS：选项坐标
            return json.dumps([
                {"letter": "A", "text": "对", "x": OPT_A_COORDS[0], "y": OPT_A_COORDS[1]},
                {"letter": "B", "text": "错", "x": OPT_B_COORDS[0], "y": OPT_B_COORDS[1]},
            ])
        if "currentTime" in expr:                      # video_state
            return json.dumps({"dur": 664.0, "cur": 171.0, "paused": not self.submitted})
        return None


class TestAlreadySubmitted(unittest.TestCase):
    def test_returns_structured_already_without_indexerror(self):
        """V5 崩溃源：旧返回 "answered(already)" 被 split 越界。钉死新契约。"""
        page = FakePage()
        page.submitted = True
        r = v4.handle_popup(page, Path(tempfile.mkdtemp()), wait_answer_s=1)
        self.assertIsInstance(r, dict)
        self.assertEqual(r["status"], "already_submitted")
        self.assertTrue(r["verified"])
        self.assertFalse(r["status"].startswith("answered("))   # 旧越界形态永不回归


class TestNewQuizFlow(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def answer_soon(self, delay=1.0, answer="A"):
        """模拟 Agent：工单落盘 1 秒后写入答案文件（真实看门狗时序的缩影）。"""
        import threading

        def _write():
            for p in self.dir.glob("pending_*.json"):
                tid = p.stem.replace("pending_", "")
                (self.dir / f"answer_{tid}.json").write_text(
                    json.dumps({"id": tid, "answer": answer}), encoding="utf-8")
        threading.Timer(delay, _write).start()

    def test_new_quiz_writes_pending_then_timeout(self):
        """无答案 → 工单原子落盘 + 超时归档 + 转人工（不计成功）。"""
        page = FakePage()
        r = v4.handle_popup(page, self.dir, wait_answer_s=0)   # 0 → 立即超时
        self.assertEqual(r["status"], "timeout")
        self.assertEqual(list(self.dir.glob("pending_*.json")), [])
        self.assertEqual(len(list(self.dir.glob("timeout_*.json"))), 1)

    def test_answered_with_submit_verification(self):
        """答案到达 → 点选 → 提交 → 页面「已提交」→ verified=True + 去重索引。"""
        page = FakePage()
        self.answer_soon(1.0, "A")
        r = v4.handle_popup(page, self.dir, wait_answer_s=8)
        self.assertEqual(r["status"], "answered")
        self.assertTrue(r["verified"])
        self.assertIn(OPT_A_COORDS, page.clicks)               # 真实点击了选项 A
        dones = list(self.dir.glob("done_*.json"))
        self.assertEqual(len(dones), 1)
        data = json.loads(dones[0].read_text(encoding="utf-8"))
        self.assertEqual(data["state"], "answered")
        keys = json.loads((self.dir / "answered_keys.json").read_text(encoding="utf-8"))
        self.assertEqual(len(keys), 1)

    def test_dedupe_hit_replays_answer_without_new_pending(self):
        """同题第二次弹出 → 去重直接重放，不写新工单、不再打扰 Agent。

        契约：pending 始终为空（不问 Agent）；每次出现各留一份 done 档案；
        去重索引只有一条（一题一键）。
        """
        page = FakePage()
        self.answer_soon(1.0, "A")
        v4.handle_popup(page, self.dir, wait_answer_s=8)   # 首次：建立去重索引
        r = v4.handle_popup(FakePage(), self.dir, wait_answer_s=5)   # 二次：应秒回
        self.assertEqual(r["status"], "answered")
        self.assertIn("dedupe", r["detail"])
        self.assertEqual(list(self.dir.glob("pending_*.json")), [])
        keys = json.loads((self.dir / "answered_keys.json").read_text(encoding="utf-8"))
        self.assertEqual(len(keys), 1)                     # 一题一键，不重复入索引


if __name__ == "__main__":
    unittest.main()
