"""考试 / 监考页面守卫（智慧树红线的唯一执行点）。

**这是本项目最重要的一段代码。** 用户要求（目标 5）：

    正式考试、期中、期末、监考页面一律不自动操作。

为什么不能"靠 Adapter 自觉"：智慧树的 URL 里 `/exam` 同时出现在
**掌握度练习**（`studywisdomh5.zhihuishu.com/exam`、`fusioncourseh5.zhihuishu.com/exam`）
和**正式考试**（`examloop.zhihuishu.com/exam`）两种语义完全相反的页面上。
任何一个 Adapter 只要少判断一层，就可能把「自动答题」用在了正式考上——
那是学术诚信事故，不是 bug。所以这条规则必须落在**统一层**，
在 Adapter 之前拦截，并由测试钉死。

设计取舍：**白名单优先于黑名单，未知一律按危险处理。**

    1. 命中"练习白名单" → PRACTICE（允许自动作答）
    2. 命中"考试黑名单" → EXAM_BLOCKED（拒绝一切写操作）
    3. 命中"学习页"     → LEARNING（允许播放/切章）
    4. 其余             → UNKNOWN（只读放行；写操作一律拒绝，转人工）

第 2 步在第 3 步之前、第 1 步在第 2 步之前，顺序不可调换。

补充一层"运行期 DOM 复核"：URL 白名单是静态的，课程改版后可能失效。
因此 Adapter 在真正点击之前**必须**再调一次 `dom_looks_like_exam()`，
用页面自身的文案做二次确认。两层都过才允许写操作。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class PageKind(StrEnum):
    """页面分类。"""

    PRACTICE = "practice"        # 练习 / 掌握度：允许自动作答
    LEARNING = "learning"        # 学习页：允许播放、切章
    EXAM_BLOCKED = "exam_blocked"  # 正式考试 / 监考：拒绝一切写操作
    UNKNOWN = "unknown"          # 未知：只读放行，写操作转人工


class GuardVerdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    MANUAL = "manual"  # 拒绝自动操作，但可人工介入


class ExamBlockedError(RuntimeError):
    """命中考试/监考黑名单。**不可重试、不可 fallback**（换 Adapter 也一样危险）。"""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"拒绝在考试/监考页面执行自动操作：{url}（{reason}）")
        self.url = url
        self.reason = reason
        self.code = "EXAM_PAGE_BLOCKED"
        self.category = "permission"


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    kind: PageKind
    reason: str


def _r(regex: str, kind: PageKind, reason: str) -> Rule:
    return Rule(re.compile(regex, re.IGNORECASE), kind, reason)


# ---------------------------------------------------------------------------
# 1. 练习 / 掌握度白名单（必须排在黑名单之前判断）
# ---------------------------------------------------------------------------
PRACTICE_RULES: tuple[Rule, ...] = (
    # 掌握度 / 知识点掌握 —— 由 upstreams/zhihuishu-zhangwodu 覆盖的页面
    _r(r"studywisdomh5\.zhihuishu\.com/exam", PageKind.PRACTICE, "掌握度练习（新智慧共享课）"),
    _r(r"wisdom-mooc\.zhihuishu\.com/exam", PageKind.PRACTICE, "掌握度练习（2025-12 新智慧）"),
    _r(r"fusioncourseh5\.zhihuishu\.com/exam", PageKind.PRACTICE, "AI 助教掌握度练习"),
    _r(r"/pointOfMastery", PageKind.PRACTICE, "知识点掌握页"),
    _r(r"/study/mastery", PageKind.PRACTICE, "掌握度学习页"),
    _r(r"/study/analysis", PageKind.PRACTICE, "掌握度分析页"),
    # 章节测验 / 普通练习（作业型，非考试型）
    _r(r"stuExamWeb\.html#/webExamList/dohomework", PageKind.PRACTICE, "共享课章节作业"),
    _r(r"/stu/answer-homework", PageKind.PRACTICE, "AI 教学中心-题目作业"),
)

# ---------------------------------------------------------------------------
# 2. 考试 / 监考黑名单
# ---------------------------------------------------------------------------
EXAM_RULES: tuple[Rule, ...] = (
    # 共享课正式考试
    _r(r"stuExamWeb\.html#/webExamList/doexamination", PageKind.EXAM_BLOCKED, "共享课正式考试"),
    # 新形态课程考试界面
    _r(r"examloop\.zhihuishu\.com", PageKind.EXAM_BLOCKED, "新形态课程考试界面"),
    _r(r"smartcourseexam\.zhihuishu\.com", PageKind.EXAM_BLOCKED, "新形态课程考试/作业专用域"),
    # 校内课考试
    _r(r"/atHomeworkExam/stu/examQ/examexercise", PageKind.EXAM_BLOCKED, "校内课考试"),
    # AI 教学中心考试
    _r(r"/stu-exam/answer-exam", PageKind.EXAM_BLOCKED, "AI 教学中心考试"),
    # 语义明确的考试型页面（掌握提升/复习测：名称含 TestOrExam，无法安全区分 → 按考试处理）
    _r(r"studentexamcomh5\.zhihuishu\.com", PageKind.EXAM_BLOCKED, "掌握提升/复习测（无法区分练习与考试，保守按考试处理）"),
    # 监考 / 巡考信号（域名不固定，靠路径关键词）
    _r(r"(proctor|invigilat|monitor[-_]?exam)", PageKind.EXAM_BLOCKED, "在线监考"),
)

# ---------------------------------------------------------------------------
# 3. 学习页（允许播放/切章）
# ---------------------------------------------------------------------------
LEARNING_RULES: tuple[Rule, ...] = (
    _r(r"studyvideoh5\.zhihuishu\.com", PageKind.LEARNING, "经典共享课学习页"),
    _r(r"studyplush5\.zhihuishu\.com", PageKind.LEARNING, "新共享课学习页"),
    _r(r"fusioncourseh5\.zhihuishu\.com", PageKind.LEARNING, "AI 助教课学习页"),
    _r(r"studywisdomh5\.zhihuishu\.com", PageKind.LEARNING, "2025-9 新智慧共享课学习页"),
    _r(r"wisdom-mooc\.zhihuishu\.com", PageKind.LEARNING, "2025-12 新智慧学习页"),
    _r(r"smartcoursestudent\.zhihuishu\.com", PageKind.LEARNING, "新形态课程学习页"),
    _r(r"ai-smart-course-student-pro\.zhihuishu\.com", PageKind.LEARNING, "新形态课程学习页（新域名）"),
    _r(r"/aidedteaching/sourceLearning", PageKind.LEARNING, "校内课学习页"),
    _r(r"onlineweb\.zhihuishu\.com", PageKind.LEARNING, "学习首页"),
)

# ---------------------------------------------------------------------------
# 4. 运行期 DOM 复核：页面自身文案里的考试/监考特征
# ---------------------------------------------------------------------------
#: 命中任意一条即视为考试现场，即使 URL 命中了白名单也要拒绝。
DOM_EXAM_MARKERS: tuple[str, ...] = (
    "考试须知",
    "考前须知",
    "监考",
    "诚信考试承诺",
    "考试时间",
    "剩余考试时间",
    "禁止切换窗口",
    "切屏将被记录",
    "人脸识别",
    "考试中",
    "期中考试",
    "期末考试",
    "本场考试",
)

#: DOM 复核只在这些页面上执行（学习页/练习页才可能被误判，考试页已被 URL 拦下）
DOM_CHECK_KINDS: frozenset[PageKind] = frozenset({PageKind.PRACTICE, PageKind.LEARNING})

#: 允许写操作（播放、点击、作答）的页面类别
WRITE_ALLOWED_KINDS: frozenset[PageKind] = frozenset(
    {PageKind.PRACTICE, PageKind.LEARNING}
)

#: 智慧树主域，用于判断"这个 URL 是否属于本项目管辖范围"。
#: 不属于则一律 UNKNOWN（不接管），避免误伤其他站点。
ZHS_HOST_SUFFIXES: tuple[str, ...] = ("zhihuishu.com", "zhihuishu.net")


# ---------------------------------------------------------------------------
def classify(url: str) -> tuple[PageKind, str]:
    """给 URL 分类。返回 `(PageKind, 理由)`。

    判定顺序即安全语义，**不可调整**：练习白名单 → 考试黑名单 → 学习页 → 未知。
    """
    if not url:
        return PageKind.UNKNOWN, "空 URL"

    for rule in PRACTICE_RULES:
        if rule.pattern.search(url):
            return PageKind.PRACTICE, rule.reason
    for rule in EXAM_RULES:
        if rule.pattern.search(url):
            return PageKind.EXAM_BLOCKED, rule.reason
    for rule in LEARNING_RULES:
        if rule.pattern.search(url):
            return PageKind.LEARNING, rule.reason
    return PageKind.UNKNOWN, "未识别的页面（非智慧树已知页面）"


def is_zhihuishu_url(url: str) -> bool:
    """URL 是否落在智慧树域名下。"""
    host = re.sub(r"^https?://", "", (url or "").strip(), flags=re.IGNORECASE)
    host = host.split("/", 1)[0].split(":", 1)[0].lower()
    return any(host == s or host.endswith("." + s) for s in ZHS_HOST_SUFFIXES)


def dom_looks_like_exam(page_text: str) -> tuple[bool, str]:
    """运行期复核：页面可见文案是否出现考试/监考特征。

    Adapter 必须把**已渲染页面的可见文本**传进来（不是 HTML 源码，
    否则会命中隐藏节点造成误判）。
    """
    text = page_text or ""
    for marker in DOM_EXAM_MARKERS:
        if marker in text:
            return True, f"页面出现考试特征文案：{marker}"
    return False, ""


def verify(
    url: str,
    action: str,
    page_text: str | None = None,
) -> tuple[GuardVerdict, str]:
    """统一裁决入口。

    :param url:      当前页面 URL
    :param action:   `read` | `play` | `answer` | `submit`
    :param page_text: 已渲染页面的可见文本；写操作**必须**提供，否则按 MANUAL 处理
    """
    action = (action or "read").lower()
    kind, reason = classify(url)

    # ---- 读操作：除"确定性考试页"外一律放行（读取本身无副作用） ----
    if action == "read":
        if kind is PageKind.EXAM_BLOCKED:
            return GuardVerdict.DENY, reason
        return GuardVerdict.ALLOW, reason

    # ---- 写操作 ----
    if kind is PageKind.EXAM_BLOCKED:
        return GuardVerdict.DENY, reason

    if kind is PageKind.UNKNOWN:
        # 未知页面绝不自动写：可能是换了域名的考试页
        return GuardVerdict.MANUAL, f"{reason} → 写操作需人工确认"

    # ---- 白名单页面：再做 DOM 复核 ----
    if page_text is None:
        # 写操作未提供页面文本 → 无法完成第二层校验，拒绝自动执行
        return GuardVerdict.MANUAL, "写操作必须先做 DOM 复核，但未提供页面文本"

    hit, dom_reason = dom_looks_like_exam(page_text)
    if hit:
        return GuardVerdict.DENY, dom_reason

    return GuardVerdict.ALLOW, reason


def assert_writable(url: str, action: str, page_text: str | None = None) -> str:
    """写操作前置断言。不通过直接抛 `ExamBlockedError`。

    返回判定理由（供日志记录"为什么允许"）。
    """
    verdict, reason = verify(url, action, page_text)
    if verdict is GuardVerdict.ALLOW:
        return reason
    raise ExamBlockedError(url, reason)


def audit_table() -> list[dict[str, str]]:
    """给 `cli safety` 命令用的规则总览（可人工核对）。"""
    rows: list[dict[str, str]] = []
    for group, rules in (
        ("1-practice", PRACTICE_RULES),
        ("2-exam", EXAM_RULES),
        ("3-learning", LEARNING_RULES),
    ):
        for rule in rules:
            rows.append(
                {
                    "order": group,
                    "kind": str(rule.kind),
                    "pattern": rule.pattern.pattern,
                    "reason": rule.reason,
                }
            )
    return rows
