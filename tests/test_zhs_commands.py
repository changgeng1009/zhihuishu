"""智慧树专属辅助命令的集成测试（safety / safety_check / upstreams）。

这三条命令是本项目相对姊妹项目**新增**的对外接口，所以必须有测试覆盖：
它们是使用者与排障者手上唯一的"守卫可核"入口。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from orchestrator.bootstrap import build

ROOT = Path(__file__).resolve().parent.parent


class ZhsCommandCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ctx = build(root=ROOT)

    def run_cmd(self, command: str, **params):
        return self.ctx.orchestrator.run(command, params)


class TestSafetyCommand(ZhsCommandCase):
    def test_lists_all_rule_groups(self):
        env = self.run_cmd("safety")
        self.assertTrue(env.ok)
        rules = env.data["rules"]
        kinds = {r["kind"] for r in rules}
        self.assertIn("practice", kinds)
        self.assertIn("exam_blocked", kinds)
        self.assertIn("learning", kinds)

    def test_exposes_dom_markers(self):
        env = self.run_cmd("safety")
        markers = env.data["dom_markers"]
        self.assertIn("考试须知", markers)
        self.assertIn("监考", markers)

    def test_declares_fixed_order(self):
        env = self.run_cmd("safety")
        self.assertIn("practice", env.data["order"])
        self.assertIn("不可调整", env.data["order"])


class TestSafetyCheckCommand(ZhsCommandCase):
    def test_exam_url_denied(self):
        env = self.run_cmd(
            "safety_check",
            url="https://examloop.zhihuishu.com/exam",
            action="answer",
        )
        self.assertTrue(env.ok)  # 命令本身成功执行
        self.assertFalse(env.data["allowed"])
        self.assertEqual(env.data["page_kind"], "exam_blocked")
        self.assertTrue(env.warnings)

    def test_practice_url_allowed(self):
        env = self.run_cmd(
            "safety_check",
            url="https://studywisdomh5.zhihuishu.com/exam",
            action="answer",
            page_text="知识点掌握 单选题",
        )
        self.assertTrue(env.data["allowed"])
        self.assertEqual(env.data["page_kind"], "practice")

    def test_unknown_url_write_needs_manual(self):
        env = self.run_cmd("safety_check", url="https://example.com/x", action="play")
        self.assertFalse(env.data["allowed"])
        self.assertEqual(env.data["verdict"], "manual")

    def test_dom_text_can_override_whitelist(self):
        env = self.run_cmd(
            "safety_check",
            url="https://studywisdomh5.zhihuishu.com/exam",
            action="answer",
            page_text="期末考试 考试须知 诚信考试承诺",
        )
        self.assertFalse(env.data["allowed"])
        self.assertEqual(env.data["verdict"], "deny")

    def test_read_action_on_practice_allowed(self):
        env = self.run_cmd("safety_check", url="https://studywisdomh5.zhihuishu.com/exam")
        self.assertTrue(env.data["allowed"])


class TestUpstreamsCommand(ZhsCommandCase):
    def test_lock_file_is_consistent(self):
        env = self.run_cmd("upstreams")
        rows = env.data["upstreams"]
        self.assertEqual(len(rows), 4)
        for row in rows:
            with self.subTest(adapter=row["id"]):
                self.assertTrue(row["cloned"], f"{row['id']} 未 clone")
                self.assertTrue(row["match"], f"{row['id']} commit 与锁文件不一致")
                self.assertFalse(row["dirty"], f"{row['id']} 工作区被改动（违反红线 R2）")
        self.assertTrue(env.ok, f"upstreams 校验未通过：{env.warnings}")

    def test_reports_license_and_isolation(self):
        env = self.run_cmd("upstreams")
        by_id = {r["id"]: r for r in env.data["upstreams"]}
        self.assertEqual(by_id["ocsjs"]["license"], "MIT")
        self.assertEqual(by_id["ocsjs"]["isolation"], "browser")
        self.assertEqual(by_id["Autovisor"]["isolation"], "process")
        # 两个只读参考项目不得被声明为可执行接触
        self.assertEqual(by_id["zhihuishu-zhangwodu"]["isolation"], "reference-only")
        self.assertEqual(
            by_id["zhihuishu-resource-helper"]["isolation"], "reference-only"
        )

    def test_nc_and_noassertion_licenses_are_recorded(self):
        """许可证事实必须写在锁文件里，不能只存在于口头约定。"""
        env = self.run_cmd("upstreams")
        by_id = {r["id"]: r for r in env.data["upstreams"]}
        self.assertIn("NC", by_id["zhihuishu-zhangwodu"]["license"])
        self.assertEqual(by_id["zhihuishu-resource-helper"]["license"], "NOASSERTION")


class TestCommandRegistry(ZhsCommandCase):
    def test_zhs_commands_are_registered(self):
        from orchestrator.services import COMMANDS

        for name in ("safety", "safety_check", "upstreams"):
            with self.subTest(command=name):
                self.assertIn(name, COMMANDS)
                self.assertEqual(COMMANDS[name].kind, "aux")

    def test_interface_is_shared_with_sister_projects(self):
        """对外接口一致性：命令名与能力编号必须与 cx / zhihui 对齐。"""
        from orchestrator.services import COMMANDS

        for name in (
            "list_courses", "get_course", "get_progress", "scan_tasks",
            "run_course", "run_chapter", "run_video_tasks", "run_reading_tasks",
            "status", "pause", "resume", "retry", "stop",
            "adapters", "probe", "accounts",
            "answer_pending", "answer_submit", "answer_stats",
        ):
            with self.subTest(command=name):
                self.assertIn(name, COMMANDS)


if __name__ == "__main__":
    unittest.main()
