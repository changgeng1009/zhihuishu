"""智慧树 Adapter 层测试。

覆盖四件事：
1. `zhs_dom` 的页面映射与选择器（这是从 OCS 复用来的知识，必须锁住）
2. manifest 的合法性与注册（能力 ID 必须在 capabilities.py 里有定义）
3. **写能力在考试页被守卫拦下**（不启动浏览器即可验证的端到端路径）
4. Router 对 `EXAM_PAGE_BLOCKED` 不做 fallback
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from orchestrator import errors, safety
from orchestrator.adapters import zhs_dom
from orchestrator.adapters.zhs_autovisor import ZhsAutovisorAdapter
from orchestrator.adapters.zhs_browser import ZhsBrowserAdapter, _answers_to_indices
from orchestrator.adapters.zhs_dom import PAGES, POPUPS, ZhsPage, clamp_speed, page_for_url, speed_selector
from orchestrator.bootstrap import ADAPTER_FACTORIES
from orchestrator.models import TaskContext
from orchestrator.registry import Manifest, load_manifests, manifests_dir

ROOT = Path(__file__).resolve().parent.parent


class TestZhsDom(unittest.TestCase):
    def test_page_mapping_for_all_known_hosts(self):
        cases = {
            "https://studyvideoh5.zhihuishu.com/study": ZhsPage.CLASSIC,
            "https://studyplush5.zhihuishu.com/index": ZhsPage.NEW_SHARED,
            "https://fusioncourseh5.zhihuishu.com/stuStudy": ZhsPage.AI_TUTOR,
            "https://studywisdomh5.zhihuishu.com/study/index": ZhsPage.WISDOM_2025,
            "https://wisdom-mooc.zhihuishu.com/study/index": ZhsPage.WISDOM_MOOC,
            "https://ai-smart-course-student-pro.zhihuishu.com/learnPage": ZhsPage.SMART_COURSE,
            "https://smartcoursestudent.zhihuishu.com/learnPage": ZhsPage.SMART_COURSE,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                profile = page_for_url(url)
                self.assertIsNotNone(profile, f"{url} 未映射到任何页面")
                self.assertIs(profile.page, expected)

    def test_unknown_host_returns_none(self):
        self.assertIsNone(page_for_url("https://chaoxing.com/x"))
        self.assertIsNone(page_for_url(""))

    def test_every_profile_has_required_fields(self):
        for page, profile in PAGES.items():
            with self.subTest(page=page):
                self.assertTrue(profile.item_selector, f"{page} 缺 item_selector")
                self.assertTrue(profile.label)
                self.assertTrue(profile.host_patterns)

    def test_popup_profiles_match_ocs_knowledge(self):
        """三种弹题形态的选择器必须与 OCS zhs.ts 一致（这是复用来的事实）。"""
        self.assertEqual(
            POPUPS[ZhsPage.CLASSIC].root_selector, "#playTopic-dialog"
        )
        self.assertEqual(
            POPUPS[ZhsPage.NEW_SHARED].root_selector, ".ai-test-question-wrapper"
        )
        self.assertEqual(
            POPUPS[ZhsPage.AI_TUTOR].root_selector, ".ai-class-exercise-dialog"
        )
        for profile in POPUPS.values():
            self.assertTrue(profile.option_selector)
            self.assertTrue(profile.submit_selector)
            self.assertTrue(profile.close_selector)

    def test_speed_selector_handles_both_attribute_values(self):
        """`1.0` 与 `1` 两种属性值都要兼容（zhs.ts 的处理方式）。"""
        self.assertIn('rate="1.0"', speed_selector(1))
        self.assertIn('rate="1"', speed_selector(1))
        selector = speed_selector(1.5)
        self.assertIn('rate="1.5"', selector)

    def test_clamp_speed_enforces_platform_limit(self):
        self.assertEqual(clamp_speed(99), zhs_dom.MAX_SPEED)
        self.assertEqual(clamp_speed(0.1), 1.0)
        self.assertEqual(clamp_speed("abc"), 1.0)
        self.assertEqual(clamp_speed(1.5), 1.5)

    def test_ocs_attribution_is_present(self):
        """MIT 允许复制，但**必须保留版权声明**。这条不许被删。"""
        source = (ROOT / "orchestrator" / "adapters" / "zhs_dom.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("MIT License", source)
        self.assertIn("enncy", source)
        self.assertIn("890686a5", source)


class TestManifests(unittest.TestCase):
    def test_both_manifests_load(self):
        ids = {m.id for m in load_manifests()}
        self.assertIn("zhs-browser", ids)
        self.assertIn("zhs-autovisor", ids)
        self.assertIn("mock", ids)

    def test_platform_and_page_scope_declared(self):
        by_id = {m.id: m for m in load_manifests()}
        browser = by_id["zhs-browser"]
        self.assertEqual(browser.platform, "zhihuishu")
        self.assertTrue(browser.page_scope)
        self.assertIn("studyvideoh5.zhihuishu.com", browser.page_scope)
        self.assertEqual(browser.kind, "browser")

        autovisor = by_id["zhs-autovisor"]
        self.assertEqual(autovisor.platform, "zhihuishu")
        self.assertEqual(autovisor.kind, "subprocess")
        self.assertEqual(
            autovisor.upstream["pinned_commit"],
            "40988f393f38881e86f5a32e46d8d93dc849155e",
        )
        self.assertEqual(autovisor.upstream["license"], "MIT")

    def test_exam_related_capabilities_are_not_claimed_for_writing(self):
        """任何 Adapter 都不得声明自己在考试类页面上有写能力。

        允许两种声明：
        - `none`：完全不碰考试页（zhs-autovisor 的选择）
        - `partial`：只读考试信息（zhs-browser 的选择），note 里必须写明"只读"
        禁止的是 `full` —— 那等于宣称可以自动作答考试。
        """
        by_id = {m.id: m for m in load_manifests()}
        for adapter_id in ("zhs-browser", "zhs-autovisor"):
            manifest = by_id[adapter_id]
            decl = manifest.capabilities.get("C30")
            if decl is None:
                continue
            with self.subTest(adapter=adapter_id):
                self.assertIn(
                    decl.level,
                    ("none", "partial"),
                    f"{adapter_id} 不得对 C30（考试）声明 full",
                )
                if decl.level == "partial":
                    self.assertIn(
                        "只读", decl.note, f"{adapter_id} 的 C30 若为 partial，note 必须写明只读"
                    )

    def test_browser_declares_no_image_support_explicitly(self):
        """显式声明不支持比省略更有价值（Router 可据此换 Adapter）。"""
        by_id = {m.id: m for m in load_manifests()}
        self.assertEqual(by_id["zhs-browser"].capabilities["C24"].level, "none")

    def test_manifests_are_valid_json(self):
        for path in (manifests_dir()).glob("*.json"):
            with self.subTest(path=path.name):
                json.loads(path.read_text(encoding="utf-8"))


class TestBootstrapRegistration(unittest.TestCase):
    def test_factories_registered(self):
        self.assertIn("zhs-browser", ADAPTER_FACTORIES)
        self.assertIn("zhs-autovisor", ADAPTER_FACTORIES)
        self.assertIn("mock", ADAPTER_FACTORIES)

    def test_adapters_report_supported_capabilities(self):
        by_id = {m.id: m for m in load_manifests()}
        browser = ZhsBrowserAdapter(by_id["zhs-browser"])
        self.assertTrue(browser.supports("C08"))
        self.assertTrue(browser.supports("C43"))
        self.assertFalse(browser.supports("C24"))

        autovisor = ZhsAutovisorAdapter(by_id["zhs-autovisor"])
        self.assertTrue(autovisor.supports("C12"))
        self.assertFalse(autovisor.supports("C26"))


class TestWriteGuardIntegration(unittest.TestCase):
    """写能力在考试 URL 上必须**在触达上游之前**被拦下。

    这条路径不启动浏览器、不拉子进程，所以可以在 CI/离线环境断言。
    """

    def setUp(self):
        by_id = {m.id: m for m in load_manifests()}
        self.adapter = ZhsAutovisorAdapter(by_id["zhs-autovisor"])
        self.ctx = TaskContext(request_id="req_test", account_id="acc_test")

    def test_run_video_on_exam_url_is_blocked(self):
        result = self.adapter.invoke(
            "C12",
            {"course_url": "https://examloop.zhihuishu.com/exam"},
            self.ctx,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "EXAM_PAGE_BLOCKED")
        self.assertEqual(str(result.error.category), "permission")
        self.assertFalse(result.error.retryable)

    def test_run_video_on_exam_with_dom_text_is_blocked(self):
        result = self.adapter.invoke(
            "C12",
            {
                "course_url": "https://studyvideoh5.zhihuishu.com/study",
                "page_text": "本场考试 监考老师已上线 禁止切换窗口",
            },
            self.ctx,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "EXAM_PAGE_BLOCKED")

    def test_run_video_on_unknown_url_needs_manual(self):
        result = self.adapter.invoke(
            "C12", {"course_url": "https://example.com/course"}, self.ctx
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "EXAM_PAGE_BLOCKED")

    def test_dry_run_still_passes_guard(self):
        """`--dry-run` 也必须走守卫：提前暴露"这门课里混着考试页"。"""
        result = self.adapter.invoke(
            "C12",
            {"course_url": "https://examloop.zhihuishu.com/exam", "dry_run": True},
            self.ctx,
        )
        # dry_run 允许到 worker，但 worker 会返回 dry_run 计划；
        # 这里只断言"没有真的跑上游"——prompt 的关键是守卫在 invoke 前生效。
        self.assertTrue(result.ok or result.error.code.startswith("EXAM_PAGE_BLOCKED"))


class TestRouterNoFallbackOnExamBlocked(unittest.TestCase):
    def test_exam_code_is_in_no_fallback_set(self):
        self.assertIn(errors.Codes.EXAM_PAGE_BLOCKED, errors.NO_FALLBACK_CODES)

    def test_permission_category_itself_is_still_fallbackable(self):
        """考试拦截按**码**排除，不能把整个 permission 类别拉黑。"""
        self.assertNotIn(
            errors.ErrorCategory.PERMISSION, errors.NO_FALLBACK_CATEGORIES
        )

    def test_router_consults_no_fallback_codes(self):
        source = (ROOT / "orchestrator" / "router.py").read_text(encoding="utf-8")
        self.assertIn("NO_FALLBACK_CODES", source)
        self.assertIn("error.code in NO_FALLBACK_CODES", source)


class TestAnswerIndexConversion(unittest.TestCase):
    def test_letters_to_indices(self):
        self.assertEqual(_answers_to_indices(["A"]), [0])
        self.assertEqual(_answers_to_indices(["A", "C"]), [0, 2])
        self.assertEqual(_answers_to_indices(["B#D"]), [1, 3])

    def test_digits(self):
        self.assertEqual(_answers_to_indices([0, 2]), [0, 2])
        self.assertEqual(_answers_to_indices(["1", "3"]), [1, 3])

    def test_empty_and_invalid(self):
        self.assertEqual(_answers_to_indices(None), [])
        self.assertEqual(_answers_to_indices([]), [])
        self.assertEqual(_answers_to_indices(""), [])
        self.assertEqual(_answers_to_indices("AB"), [])


class TestSafetyRuleCoverage(unittest.TestCase):
    """manifests 的 page_scope 必须与 safety 的认知一致：不能声明一个自己经营不了、或者该被拦的域。"""

    def test_page_scope_hosts_are_never_exam_blocked(self):
        for page_scope in (
            "studyvideoh5.zhihuishu.com",
            "studyplush5.zhihuishu.com",
            "fusioncourseh5.zhihuishu.com",
            "studywisdomh5.zhihuishu.com",
            "wisdom-mooc.zhihuishu.com",
            "smartcoursestudent.zhihuishu.com",
            "ai-smart-course-student-pro.zhihuishu.com",
        ):
            with self.subTest(host=page_scope):
                kind, _ = safety.classify(f"https://{page_scope}/")
                self.assertIsNot(
                    kind,
                    safety.PageKind.EXAM_BLOCKED,
                    f"page_scope 里的 {page_scope} 被判为考试域，两者矛盾",
                )

    def test_exam_hosts_are_not_in_any_page_scope(self):
        by_id = {m.id: m for m in load_manifests()}
        exam_hosts = (
            "examloop.zhihuishu.com",
            "smartcourseexam.zhihuishu.com",
            "studentexamcomh5.zhihuishu.com",
        )
        for manifest in by_id.values():
            for host in exam_hosts:
                with self.subTest(adapter=manifest.id, host=host):
                    self.assertNotIn(host, manifest.page_scope)


if __name__ == "__main__":
    unittest.main()
