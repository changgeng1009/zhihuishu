"""智慧树页面 DOM 知识（选择器常量 + 页面分类 + 弹题弹窗结构）。

## 来源与许可

选择器常量与页面分类知识**复制自** `ocsjs/ocsjs`（MIT License,
Copyright (c) 2022 enncy）的 `packages/scripts/src/projects/zhs.ts`
（commit `890686a5e54f9a6d52d1169bae9ea5971e0863c7`，3620 行）。

MIT 许可证允许复制与修改，条件是保留版权声明 —— 本文件头部即为该声明。
详见 `upstreams.lock.json` 与 `docs/01 §2.1`。

## 为什么是"复制知识"而不是"调用 OCS"

OCS 是 Vite 构建的浏览器用户脚本，运行需要用户脚本管理器宿主，
无法被 subprocess 直接驱动。而它的**页面知识**（哪个页面用什么选择器、
已完成的视觉标记是什么）正是本项目自建浏览器通道所需要的一切，
且这部分知识**无法凭空获得**（要靠反复实测平台页面）。

所以：**取知识，不取程序**。运行由 `zhs_browser.py` 通过 CDP 承担。
这不是"重写已有能力"——被复用的是一个常量表，不是一段可执行流程。

## 弹题处理的差异（重要）

OCS 对弹题的处理是**随机选一个选项**并提交：

    const random = Math.floor(Math.random() * options.length);
    options[random].click();

本项目**不采用**该策略：随机作答会污染掌握度数据、并在练习里留下错答记录。
本项目改为"读题 → Agent/LLM 作答 → 回填注入"（用户目标 3），
超时默认**暂停转人工**而不是随机作答。详见 `docs/02 §3 缺口 1`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ZhsPage(StrEnum):
    """智慧树学习页类型（对应 OCS 的 ZHSProcessor 实现）。"""

    CLASSIC = "classic"            # StudyVideoH5   经典共享课
    NEW_SHARED = "new_shared"      # StudyPlusH5    新共享课
    AI_TUTOR = "ai_tutor"          # FusionCourseH5 AI 助教课
    WISDOM_2025 = "wisdom_2025"    # WishdomH5      2025-9 新智慧共享课
    WISDOM_MOOC = "wisdom_mooc"    # 2025-12 新智慧（wisdom-mooc）
    SMART_COURSE = "smart_course"  # 新形态课程（只读盘点）


@dataclass(frozen=True)
class PageProfile:
    """一种学习页的完整 DOM 描述。"""

    page: ZhsPage
    label: str
    host_patterns: tuple[str, ...]
    #: 承载任务的条目（每一条 = 一个任务点）
    item_selector: str
    #: 条目上的"当前播放"标记
    current_class: str
    #: 条目上的"已完成"标记（在条目内部查找）
    finished_selector: str
    #: 章节标题的取值点
    chapter_selector: str
    #: 课程名取值点
    course_name_selector: str
    #: 弹题弹窗描述；无弹题的页面为 None
    popup: "PopupProfile | None" = None
    #: 该页面的进度显示点
    progress_selector: str = ""
    #: **真正的任务点约束**：条目或其父级必须含有这个选择器。
    #:
    #: 2026-09-20 在真实课程上实测出来的必要性：在
    #: `studywisdomh5.zhihuishu.com/study/index` 上，`.child-main` 命中 104 个，
    #: 但其中一部分是**章标题**（"上古至秦萌芽时期"），只有父级带 `.child-time`
    #: 的才是真正的视频任务点。不区分就会把章标题当成任务点报上去。
    item_constraint_selector: str = ""
    #: 已知的脆弱点（写进日志/文档，提醒排障者）
    caveats: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PopupProfile:
    """课中弹题弹窗的 DOM 结构。"""

    page: ZhsPage
    root_selector: str
    option_selector: str
    submit_selector: str
    close_selector: str
    #: 题干元素；为空表示"只能在 root 内取文本"
    stem_selector: str = ""
    #: 多题分页时的页码控件（仅经典共享课有）
    pager_selector: str = ""
    #: 是否有"已完成"态（有则只需关闭，不需作答）
    done_selector: str = ""


#: 三种弹题形态，完全来自 zhs.ts 的 `handleTestDialog`
POPUPS: dict[ZhsPage, PopupProfile] = {
    ZhsPage.CLASSIC: PopupProfile(
        page=ZhsPage.CLASSIC,
        root_selector="#playTopic-dialog",
        option_selector="#playTopic-dialog ul .topic-item",
        submit_selector="#playTopic-dialog .close-btn,#playTopic-dialog .btn",
        close_selector="#playTopic-dialog .close-btn",
        stem_selector="#playTopic-dialog .topic",
        pager_selector="#playTopic-dialog .el-pager .number",
    ),
    ZhsPage.NEW_SHARED: PopupProfile(
        page=ZhsPage.NEW_SHARED,
        root_selector=".ai-test-question-wrapper",
        option_selector=".ai-test-question-wrapper .options .option",
        submit_selector=".ai-test-question-wrapper .submit-btn .submits",
        close_selector=".ai-test-question-wrapper .close-box",
        stem_selector=".ai-test-question-wrapper .title",
        done_selector=".ai-test-question-wrapper .done",
    ),
    ZhsPage.AI_TUTOR: PopupProfile(
        page=ZhsPage.AI_TUTOR,
        root_selector=".ai-class-exercise-dialog",
        option_selector=".ai-class-exercise-dialog .ques-list .item .option",
        submit_selector=".ai-class-exercise-dialog .el-dialog__footer .el-button.btn",
        close_selector=".ai-class-exercise-dialog .header-icon",
        stem_selector=".ai-class-exercise-dialog .ques-list .item .title",
    ),
}

#: 弹题的 DOM 轮询间隔（OCS 用 3s；保持一致以免漏掉快速连续弹窗）
POPUP_POLL_INTERVAL_S: float = 3.0


PAGES: dict[ZhsPage, PageProfile] = {
    ZhsPage.CLASSIC: PageProfile(
        page=ZhsPage.CLASSIC,
        label="经典共享课",
        host_patterns=("studyvideoh5.zhihuishu.com",),
        item_selector=".clearfix.video",
        current_class="current_play",
        finished_selector=".time_icofinish",
        chapter_selector=".catalogue_title",
        course_name_selector=".source-name",
        popup=POPUPS[ZhsPage.CLASSIC],
        progress_selector=".time_icofinish",
        caveats=("弹题有分页（.el-pager .number），需逐页作答",),
    ),
    ZhsPage.NEW_SHARED: PageProfile(
        page=ZhsPage.NEW_SHARED,
        label="新共享课",
        host_patterns=("studyplush5.zhihuishu.com", "studywisdomh5.zhihuishu.com"),
        item_selector=".child-main",
        current_class="current",
        finished_selector=".finish-icon",
        chapter_selector=".top-back-box > span:nth-child(2)",
        course_name_selector=".top-back-box > span:nth-child(2)",
        popup=POPUPS[ZhsPage.NEW_SHARED],
        progress_selector=".child-time",
        item_constraint_selector=".child-time",
        caveats=(
            "条目必须满足：父级含 .child-time（视频时长）—— 没有的是章标题，不是任务点",
            "状态文案在条目父级里（『未练习』/『已完成』），不在条目本身",
            "当前项是父级的 class 含 current",
            "实测：studywisdomh5 的实际页面也走这套选择器，与域名无关",
        ),
    ),
    ZhsPage.AI_TUTOR: PageProfile(
        page=ZhsPage.AI_TUTOR,
        label="AI 助教课",
        host_patterns=("fusioncourseh5.zhihuishu.com",),
        item_selector=".clearfix.video",
        current_class="current_play",
        finished_selector=".progress-num",
        chapter_selector=".catalogue_title",
        course_name_selector="",
        popup=None,  # 继承 NEW_SHARED 的弹题形态，运行时按页面实际情况择一
        progress_selector=".progress-num",
        caveats=(
            "两种进度模式：百分比（.progress-num）与必学项目（.resource-box + .resources-item）",
            "resource-box 模式下完成标记是 .isFinish，当前项是 .activeNode",
            "课程名读不到，固定返回『智慧课程-AI』",
        ),
    ),
    ZhsPage.WISDOM_2025: PageProfile(
        page=ZhsPage.WISDOM_2025,
        label="2025-9 新智慧共享课（布局变体，未实测）",
        # ⚠️ `studywisdomh5.zhihuishu.com` 曾被按域名映射到本布局，但 2026-09-20
        # 在真实课程上实测发现该域返回的是 `.child-main` 结构（见 NEW_SHARED）。
        # 域名不是布局的可靠判据 —— 现在以 DOM 命中数为准（`detectLayout`），
        # 这里保留 host 仅用于**打标签**，不再作为选择依据。
        host_patterns=(),
        item_selector=".chapter-content .chapter-item",
        current_class="current",
        finished_selector=".finish-icon",
        chapter_selector=".course-name",
        course_name_selector=".course-name",
        popup=POPUPS[ZhsPage.AI_TUTOR],
        progress_selector=".finish-icon",
        caveats=(
            "该布局来自上游 OCS 的知识，本项目**尚未在真实账号上观测到**",
            "任务点嵌套：.chapter-content-second 才是叶子，需先展开再拍平",
            "按域名映射布局已被证伪，改由 DOM 命中数决定",
        ),
    ),
    ZhsPage.WISDOM_MOOC: PageProfile(
        page=ZhsPage.WISDOM_MOOC,
        label="2025-12 新智慧（wisdom-mooc）",
        host_patterns=("wisdom-mooc.zhihuishu.com",),
        item_selector=".chapter-content .chapter-item",
        current_class="current",
        finished_selector=".finish-icon",
        chapter_selector=".course-name",
        course_name_selector=".course-name",
        popup=POPUPS[ZhsPage.AI_TUTOR],
        progress_selector=".finish-icon",
        caveats=("布局与 WISDOM_2025 同族，但学习提示页在 /study/analysis",),
    ),
    ZhsPage.SMART_COURSE: PageProfile(
        page=ZhsPage.SMART_COURSE,
        label="新形态课程",
        host_patterns=(
            "smartcoursestudent.zhihuishu.com",
            "ai-smart-course-student-pro.zhihuishu.com",
        ),
        # 以下选择器是"页面事实"，来自 zhihuishu-resource-helper 的 README/脚本
        # 声明（该仓库无 LICENSE，**不复制其代码**，仅记录结构事实）
        item_selector=".section-item-collapse .section-item-collapse-info",
        current_class="active",
        finished_selector=".finish-icon",
        chapter_selector="#middle-section-id .point-title-text",
        course_name_selector=".section-item-collapse-title .title-text",
        popup=None,
        progress_selector=".basic-info-video-card-container",
        caveats=(
            "⚠️ 只读支持：写操作未实现（上游无许可证，不复制其逻辑）",
            "资源区 #middle-section-id .resources-section",
            "视频卡片 .basic-info-video-card-container / h5.video-title",
        ),
    ),
}

#: 播放控制（来自 zhs.ts 的 switchPlaybackRate / switchLine）
CONTROLS = {
    "controls_bar": ".controlsBar",
    "speed_list": ".speedList",
    "speed_item": '.speedList [rate="{}"]',
    "line_list": ".definiLines",
    "line_bq": ".definiLines .line1bq:not(.active)",
    "line_gq": ".definiLines .line1gq:not(.active)",
    "volume": ".volumeBox",
}

#: 智慧树倍速上限。Autovisor README 明确最高 1.8 —— 但**实测**这门课的
#: 页面只提供 1.0 / 1.25 / 1.5 三档（1.5 即最高档）。
#: 所以：上限只作夹紧用，真正选哪档由页面实际提供的档位决定（见 zhs_js.setSpeed）。
MAX_SPEED: float = 1.8

#: 本项目的默认播放倍速。
#:
#: 用户明确要求 1.5 倍速。实测这也正是当前课程页面的最高档，
#: 于是取它作默认 —— 既不超出平台允许范围，也不用每个命令都手写 `--speed 1.5`。
#: 需要时用 `--speed` 覆盖，会被 `clamp_speed` 夹到 [1.0, MAX_SPEED]。
DEFAULT_SPEED: float = 1.5

#: 实测观测到的档位（仅作记录与排障提示；真实档位永远从页面读）
OBSERVED_SPEEDS: tuple[float, ...] = (1.0, 1.25, 1.5)

#: 页面上的杂项弹窗（需要关闭/跳过，否则挡住任务流）
NOISE_DIALOGS = (
    ".el-dialog__wrapper",      # 通用通知弹窗（StudyVideoH5.hideDialog）
    ".el-overlay,.el-dialog",   # Element-Plus 弹层（StudyPlusH5/WishdomH5.hideDialog）
)


# ---------------------------------------------------------------------------
def page_for_host(host: str) -> PageProfile | None:
    """按 host 找页面描述。未识别的 host 返回 None（→ 统一层转人工）。"""
    h = (host or "").strip().lower()
    for profile in PAGES.values():
        for pattern in profile.host_patterns:
            if h == pattern or h.endswith("." + pattern):
                return profile
    return None


def page_for_url(url: str) -> PageProfile | None:
    """按 URL 找页面描述。"""
    if not url:
        return None
    rest = url.split("://", 1)[-1]
    host = rest.split("/", 1)[0].split(":", 1)[0]
    return page_for_host(host)


def speed_selector(speed: float) -> str:
    """倍速按钮选择器。`1.0` 与 `1` 两种属性值都要兼容（zhs.ts 的处理方式）。"""
    parsed = float(speed)
    primary = "1.0" if parsed == 1 else str(parsed)
    alt = "1" if parsed == 1 else str(parsed)
    return f'.speedList [rate="{primary}"],.speedList [rate="{alt}"]'


def clamp_speed(speed: float) -> float:
    """把倍速夹到平台允许的区间。"""
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return 1.0
    return max(1.0, min(MAX_SPEED, value))
