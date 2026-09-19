"""考试 / 监考守卫测试（目标 5 的验收依据）。

这是本项目**最重要的一组测试**。如果这组测试失效，"自动完成"这类功能
就可能被用在正式考试上——那是学术诚信事故，不是 bug。

因此这里的用例刻意做得「啰嗦」：把每一个真实页面的 URL 都写死，
并在注释里说明它为什么该被允许或被拒绝。
"""

from __future__ import annotations

import unittest

from orchestrator import safety
from orchestrator.safety import (
    ExamBlockedError,
    GuardVerdict,
    PageKind,
    assert_writable,
)


class TestClassify(unittest.TestCase):
    """URL → PageKind 分类。"""

    def test_exam_pages_are_blocked(self):
        cases = {
            # 共享课正式考试
            "https://zhihuishu.com/stuExamWeb.html#/webExamList/doexamination": "共享课正式考试",
            # 新形态课程考试界面
            "https://examloop.zhihuishu.com/exam": "新形态课程考试界面",
            # 新形态课程考试/作业专用域
            "https://smartcourseexam.zhihuishu.com/ReviewExam": "新形态课程考试/作业专用域",
            # 校内课考试
            "https://zhihuishu.com/atHomeworkExam/stu/examQ/examexercise": "校内课考试",
            # AI 教学中心考试
            "https://zhihuishu.com/stu-exam/answer-exam": "AI 教学中心考试",
            # 掌握提升（名称含 TestOrExam，无法安全区分 → 按考试处理）
            "https://studentexamcomh5.zhihuishu.com/studentReviewTestOrExam": "掌握提升",
        }
        for url, expect_reason in cases.items():
            with self.subTest(url=url):
                kind, reason = safety.classify(url)
                self.assertIs(
                    kind, PageKind.EXAM_BLOCKED, f"{url} 应被判为考试页，实际 {kind}"
                )
                self.assertIn(expect_reason[:4], reason)

    def test_practice_pages_are_allowed(self):
        """掌握度练习的 URL 里也含 `/exam` —— 这正是必须白名单优先的原因。"""
        cases = {
            "https://studywisdomh5.zhihuishu.com/exam": "掌握度练习（新智慧共享课）",
            "https://wisdom-mooc.zhihuishu.com/exam": "掌握度练习（2025-12 新智慧）",
            "https://fusioncourseh5.zhihuishu.com/exam": "AI 助教掌握度练习",
            "https://studywisdomh5.zhihuishu.com/pointOfMastery": "知识点掌握页",
            "https://wisdom-mooc.zhihuishu.com/study/mastery": "掌握度学习页",
            "https://wisdom-mooc.zhihuishu.com/study/analysis": "掌握度分析页",
            "https://zhihuishu.com/stuExamWeb.html#/webExamList/dohomework": "共享课章节作业",
        }
        for url, expect_reason in cases.items():
            with self.subTest(url=url):
                kind, reason = safety.classify(url)
                self.assertIs(
                    kind, PageKind.PRACTICE, f"{url} 应被判为练习页，实际 {kind}"
                )
                self.assertEqual(expect_reason, reason)

    def test_whitelist_takes_precedence_over_blacklist(self):
        """顺序不变式：练习白名单必须先于考试黑名单判定。

        `studywisdomh5.zhihuishu.com/exam` 与 `examloop.zhihuishu.com/exam`
        只差域名，语义却完全相反。若把顺序调换，掌握度练习会被误杀
        （影响可用性）或正式考试会被误放（影响诚信）——后者不可接受。
        """
        practice_url = "https://studywisdomh5.zhihuishu.com/exam"
        exam_url = "https://examloop.zhihuishu.com/exam"

        self.assertIs(safety.classify(practice_url)[0], PageKind.PRACTICE)
        self.assertIs(safety.classify(exam_url)[0], PageKind.EXAM_BLOCKED)

    def test_learning_pages(self):
        for url in (
            "https://studyvideoh5.zhihuishu.com",
            "https://studyplush5.zhihuishu.com/study",
            "https://fusioncourseh5.zhihuishu.com/stuStudy",
            "https://studywisdomh5.zhihuishu.com/study/index",
            "https://wisdom-mooc.zhihuishu.com/study/index",
            "https://smartcoursestudent.zhihuishu.com/learnPage",
            "https://ai-smart-course-student-pro.zhihuishu.com/learnPage",
            "https://zhihuishu.com/aidedteaching/sourceLearning",
        ):
            with self.subTest(url=url):
                self.assertIs(safety.classify(url)[0], PageKind.LEARNING)

    def test_unknown_page(self):
        kind, reason = safety.classify("https://example.com/whatever")
        self.assertIs(kind, PageKind.UNKNOWN)
        self.assertIn("未识别", reason)

    def test_empty_url_is_unknown(self):
        self.assertIs(safety.classify("")[0], PageKind.UNKNOWN)


class TestHostScope(unittest.TestCase):
    def test_zhihuishu_hosts(self):
        self.assertTrue(safety.is_zhihuishu_url("https://studywisdomh5.zhihuishu.com/x"))
        self.assertTrue(safety.is_zhihuishu_url("http://zhihuishu.com"))
        self.assertFalse(safety.is_zhihuishu_url("https://chaoxing.com"))
        self.assertFalse(safety.is_zhihuishu_url("https://zhihuishu.com.evil.com"))

    def test_lookalike_host_is_not_zhihuishu(self):
        """后缀匹配必须防止 `evil-zhihuishu.com` 之类的伪装域。"""
        self.assertFalse(safety.is_zhihuishu_url("https://notzhihuishu.com/x"))
        self.assertFalse(safety.is_zhihuishu_url("https://xzhihuishu.com/x"))


class TestVerifyReads(unittest.TestCase):
    def test_read_allowed_everywhere_except_exam(self):
        self.assertIs(
            safety.verify("https://studywisdomh5.zhihuishu.com/study/index", "read")[0],
            GuardVerdict.ALLOW,
        )
        self.assertIs(
            safety.verify("https://example.com/unknown", "read")[0], GuardVerdict.ALLOW
        )

    def test_read_on_exam_page_denied(self):
        """即使只是"读"，考试页也一律不碰——避免误触发页面的心跳/上报。"""
        verdict, reason = safety.verify("https://examloop.zhihuishu.com/exam", "read")
        self.assertIs(verdict, GuardVerdict.DENY)
        self.assertIn("考试", reason)


class TestVerifyWrites(unittest.TestCase):
    def test_write_on_exam_page_denied(self):
        for url in (
            "https://examloop.zhihuishu.com/exam",
            "https://zhihuishu.com/stuExamWeb.html#/webExamList/doexamination",
            "https://studentexamcomh5.zhihuishu.com/studentReviewTestOrExam",
        ):
            with self.subTest(url=url):
                verdict, _ = safety.verify(url, "answer", page_text="")
                self.assertIs(verdict, GuardVerdict.DENY)

    def test_write_on_unknown_page_requires_manual(self):
        """未知页面绝不自动写：可能是换了域名的考试页。"""
        verdict, reason = safety.verify("https://example.com/x", "play", page_text="")
        self.assertIs(verdict, GuardVerdict.MANUAL)
        self.assertIn("人工", reason)

    def test_write_without_page_text_requires_manual(self):
        """第二层 DOM 复核没有数据 → 无法校验 → 拒绝自动执行。"""
        verdict, reason = safety.verify(
            "https://studyvideoh5.zhihuishu.com/study", "play", page_text=None
        )
        self.assertIs(verdict, GuardVerdict.MANUAL)
        self.assertIn("DOM 复核", reason)

    def test_write_on_learning_page_with_clean_text_allowed(self):
        verdict, _ = safety.verify(
            "https://studyvideoh5.zhihuishu.com/study",
            "play",
            page_text="课程名称：建筑防排烟技术 第一章 绪论 正在播放",
        )
        self.assertIs(verdict, GuardVerdict.ALLOW)

    def test_write_on_practice_page_with_clean_text_allowed(self):
        verdict, _ = safety.verify(
            "https://studywisdomh5.zhihuishu.com/exam",
            "answer",
            page_text="知识点掌握 第 3 题 单选题",
        )
        self.assertIs(verdict, GuardVerdict.ALLOW)

    def test_dom_marker_overrides_whitelisted_url(self):
        """URL 命中了练习白名单，但页面文案是考试现场 → 仍然拒绝。

        这就是"两层校验"的价值：白名单是静态的，课程改版后可能失效；
        页面自身说"这是考试"时，必须听页面的。
        """
        for marker in ("考试须知", "监考", "诚信考试承诺", "切屏将被记录", "人脸识别"):
            with self.subTest(marker=marker):
                verdict, reason = safety.verify(
                    "https://studywisdomh5.zhihuishu.com/exam",
                    "answer",
                    page_text=f"欢迎参加本学期期末考试 {marker} 请遵守纪律",
                )
                self.assertIs(verdict, GuardVerdict.DENY)
                self.assertIn(marker, reason)

    def test_dom_marker_overrides_learning_url(self):
        verdict, reason = safety.verify(
            "https://studyvideoh5.zhihuishu.com/study",
            "play",
            page_text="本场考试 剩余考试时间 59:30",
        )
        self.assertIs(verdict, GuardVerdict.DENY)


class TestAssertWritable(unittest.TestCase):
    def test_raises_exam_blocked_error(self):
        with self.assertRaises(ExamBlockedError) as cm:
            assert_writable("https://examloop.zhihuishu.com/exam", "answer", "")
        exc = cm.exception
        self.assertEqual(exc.code, "EXAM_PAGE_BLOCKED")
        self.assertEqual(exc.category, "permission")
        self.assertIn("examloop", exc.url)

    def test_manual_verdict_also_raises(self):
        """MANUAL 在断言语境下等同于拒绝自动执行。"""
        with self.assertRaises(ExamBlockedError):
            assert_writable("https://example.com/x", "play", "随便什么文本")

    def test_returns_reason_when_allowed(self):
        reason = assert_writable(
            "https://studyvideoh5.zhihuishu.com/study", "play", "第一章"
        )
        self.assertTrue(reason)


class TestAuditTable(unittest.TestCase):
    def test_audit_table_covers_all_rules(self):
        rows = safety.audit_table()
        self.assertGreater(len(rows), 20)
        kinds = {r["kind"] for r in rows}
        self.assertIn("practice", kinds)
        self.assertIn("exam_blocked", kinds)
        self.assertIn("learning", kinds)

    def test_every_rule_has_reason(self):
        for row in safety.audit_table():
            self.assertTrue(row["reason"].strip(), f"规则 {row['pattern']} 缺理由")


if __name__ == "__main__":
    unittest.main()
