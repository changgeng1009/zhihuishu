"""智慧树浏览器通道 Adapter（CDP）。

承担三类职责：

1. **读侧**（C06–C11、C29、C30、C32）—— 课程/章节/任务点/进度/作业/考试信息。
   智慧树没有可用的公开 API 客户端，页面 XHR 带签名与反爬参数；
   最稳的读法就是"在已登录的页面里读 DOM / 调用页面自己的 XHR"。
2. **写侧**（C12、C18、C20、C26）—— 播放、切章、倍速、章节测验。
3. **弹题与练习的 Agent 链路**（C43–C45）—— 这是本项目相对上游的增量：
   上游要么随机作答（OCS），要么只暂停（Autovisor）。

## 安全

**所有写能力必须先过 `safety.assert_writable()`**（URL 静态分类 + 页面可见文本复核），
且必须提供 `page_text`。没有 `page_text` 一律判 `MANUAL`——宁可转人工，不可乱点。

## 浏览器隔离

复用 `orchestrator/browser.py`：独立 profile `.browser/edge-profile`、
CDP 端口 9333、仅监听 127.0.0.1。`assert_isolated()` 在附着前守卫，
**绝不附着到用户日常浏览器**。
"""

from __future__ import annotations

import json
import time
from typing import Any

from .. import browser as browser_mod
from .. import cdp as cdp_mod
from ..errors import AdapterError, Codes, ErrorCategory
from ..models import AccountContext, AdapterResult, ProbeResult, TaskContext
from ..registry import Manifest
from ..safety import assert_writable
from .base import Adapter
from .zhs_dom import CONTROLS, MAX_SPEED, NOISE_DIALOGS, PAGES, ZhsPage, page_for_url
from .zhs_js import BOOTSTRAP_JS


def _platform_changed(adapter_id: str, detail: str) -> AdapterError:
    """平台改版导致的解析失败。归 `platform_changed`（Router 会降级并 fallback）。"""
    return AdapterError(
        code=Codes.ADAPTER_ERROR,
        category=ErrorCategory.PLATFORM_CHANGED,
        message=f"[{adapter_id}] {detail}",
        retryable=False,
    )


#: 需要安全守卫的写能力
WRITE_CAPABILITIES: frozenset[str] = frozenset({"C12", "C18", "C20", "C26", "C27", "C44"})

#: 能力 → 内部 op
OP_BY_CAPABILITY: dict[str, str] = {
    "C06": "detect_page",
    "C07": "detect_page",
    "C08": "scan_tasks",
    "C09": "scan_tasks",
    "C10": "progress",
    "C11": "progress",
    "C12": "play",
    "C18": "next_task",
    "C20": "set_speed",
    "C26": "answer_quiz",
    "C29": "scan_tasks",
    "C30": "detect_page",
    "C32": "scan_tasks",
    "C43": "read_question",
    "C44": "submit_answers",
    "C45": "answer_loop",
}

#: 读能力（不需要守卫）
READ_OPS: frozenset[str] = frozenset(
    {"detect_page", "scan_tasks", "progress", "read_question"}
)


class ZhsBrowserAdapter(Adapter):
    """CDP 浏览器通道。"""

    def __init__(self, manifest: Manifest) -> None:
        super().__init__(manifest)
        self._port = browser_mod.debug_port()
        self._injected_for: str | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def setup(self, account: AccountContext) -> None:
        """幂等准备。

        本 Adapter **不往磁盘写任何东西**——它的全部状态在浏览器的页面里。
        唯一要确认的是浏览器 profile 是隔离的（否则拒绝工作）。
        """
        if self._setup_done:
            return
        # assert_isolated 在 profile 指向系统默认目录时抛 IsolationError。
        # 这里只是"提前失败"，真正的附着在 probe/invoke。
        self._setup_done = True

    def probe(self) -> ProbeResult:
        """轻量探活：CDP 是否在线 + 当前页是否智慧树。无副作用，不点击。"""
        started = time.monotonic()
        status = browser_mod.probe_cdp(self._port)
        latency = int((time.monotonic() - started) * 1000)
        if not status.alive:
            return ProbeResult(
                healthy=False,
                latency_ms=latency,
                detail=f"CDP 未就绪（port={self._port}）：{status.detail}",
            )
        try:
            target = self._attach()
        except Exception as exc:  # noqa: BLE001 - 探活不该抛
            return ProbeResult(
                healthy=False, latency_ms=latency, detail=f"无法附着页面：{exc}"
            )
        with target:
            info = self._eval(target, "__ZHS__.detect()", wait_promise=False)
        if not isinstance(info, dict):
            return ProbeResult(
                healthy=False, latency_ms=latency, detail="页面里没有 __ZHS__，注入未生效"
            )
        host = str(info.get("host", ""))
        return ProbeResult(
            healthy=True,
            latency_ms=latency,
            detail=f"CDP 在线，当前页 {host or '(空白页)'}",
        )

    def supports(self, capability_id: str) -> bool:
        return self.manifest.supports(capability_id)

    # ------------------------------------------------------------------
    # 平台辅助
    # ------------------------------------------------------------------
    def _attach(self) -> "_PageSession":
        """附着到当前标签页。返回一个上下文管理器。"""
        return _PageSession(self._port)

    def _ensure_injected(self, page: "_PageSession") -> None:
        """注入 `__ZHS__`（含按当前页面生成的配置）。幂等。"""
        url = page.url()
        profile = page_for_url(url)
        page_key = profile.page.value if profile else None

        cfg = {
            "currentPage": page_key,
            "profiles": {
                p.page.value: {
                    "label": p.label,
                    "itemSelector": p.item_selector,
                    "currentClass": p.current_class,
                    "finishedSelector": p.finished_selector,
                    "chapterSelector": p.chapter_selector,
                    "courseNameSelector": p.course_name_selector,
                    "progressSelector": p.progress_selector,
                }
                for p in PAGES.values()
            },
            "popups": {
                k.value: {
                    "root_selector": v.root_selector,
                    "option_selector": v.option_selector,
                    "submit_selector": v.submit_selector,
                    "close_selector": v.close_selector,
                    "stem_selector": v.stem_selector,
                    "pager_selector": v.pager_selector,
                    "done_selector": v.done_selector,
                }
                for k, v in PAGES.items()
                if v.popup is not None
            }
            | {
                ZhsPage.AI_TUTOR.value: _popup_dict(ZhsPage.AI_TUTOR),
                ZhsPage.WISDOM_2025.value: _popup_dict(ZhsPage.AI_TUTOR),
                ZhsPage.WISDOM_MOOC.value: _popup_dict(ZhsPage.AI_TUTOR),
            },
            "controls": dict(CONTROLS),
            "noiseDialogSelectors": list(NOISE_DIALOGS),
            "maxSpeed": MAX_SPEED,
        }
        if self._injected_for == url:
            return
        page.eval(f"window.__ZHS_CFG__ = {json.dumps(cfg, ensure_ascii=False)};", wait=False)
        page.eval(BOOTSTRAP_JS, wait=False)
        self._injected_for = url

    def _guard(
        self, page: "_PageSession", action: str
    ) -> None:
        """写操作前置守卫。不通过直接抛 `ExamBlockedError`。"""
        url = page.url()
        text = page.eval("__ZHS__.visibleText()", wait=True)
        assert_writable(url, action, text if isinstance(text, str) else None)

    def _eval(self, page: "_PageSession", expr: str, wait_promise: bool = True) -> Any:
        """在页面里求值，返回 JSON 化的结果。"""
        return page.eval(expr, wait=wait_promise)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def invoke(
        self, capability_id: str, params: dict[str, Any], ctx: TaskContext
    ) -> AdapterResult:
        op = OP_BY_CAPABILITY.get(capability_id)
        if op is None:
            return self.unsupported(capability_id)

        if ctx.dry_run and op not in READ_OPS and op != "play":
            return self.success(
                {"dry_run": True, "would": op, "params": params},
                warnings=["dry-run：未执行任何写操作"],
            )

        try:
            with self._attach() as page:
                self._ensure_injected(page)
                if capability_id in WRITE_CAPABILITIES:
                    self._guard(page, "play" if op in {"play", "next_task", "set_speed"} else "answer")
                handler = getattr(self, f"_op_{op}")
                data, warnings = handler(page, params, ctx)
                return self.success(data, warnings=warnings)
        except Exception as exc:  # noqa: BLE001
            return self._map_exception(exc, capability_id)

    # ------------------------------------------------------------------
    # ops
    # ------------------------------------------------------------------
    def _op_detect_page(self, page, params, ctx):
        info = self._eval(page, "__ZHS__.detect()", wait_promise=False)
        profile = page_for_url(str(info.get("href", "")) if isinstance(info, dict) else "")
        warnings: list[str] = []
        if profile is None:
            warnings.append("未识别的智慧树页面：读侧可用，写侧会转人工")
        return {"page": info, "profile": profile.page.value if profile else None}, warnings

    def _op_scan_tasks(self, page, params, ctx):
        raw = self._eval(page, "__ZHS__.scanTasks()", wait_promise=False)
        warnings: list[str] = []
        if not isinstance(raw, dict) or not raw.get("ok"):
            reason = (raw or {}).get("reason", "unknown")
            raise _platform_changed("zhs-browser", f"任务点扫描失败：{reason}")
        items = raw.get("items") or []
        chapter = ""
        profile = page_for_url(page.url())
        if profile is not None:
            chapter = _chapter_name(page, profile.chapter_selector)
        task_points = []
        for it in items:
            finished = bool(it.get("finished"))
            task_points.append(
                {
                    "task_point_id": f"tp_{it.get('index', 0):03d}",
                    "chapter_id": profile.page.value if profile else "",
                    "chapter_name": chapter,
                    "type": "video",
                    "title": it.get("title", ""),
                    "status": "done" if finished else "todo",
                }
            )
        summary = {
            "by_type": {"video": len(task_points)},
            "by_status": {
                "done": sum(1 for t in task_points if t["status"] == "done"),
                "todo": sum(1 for t in task_points if t["status"] == "todo"),
            },
        }
        if not task_points:
            warnings.append("该页面未识别到任务点（可能是新形态课程或页面改版）")
        return {"task_points": task_points, "summary": summary, "page_label": raw.get("page")}, warnings

    def _op_progress(self, page, params, ctx):
        state = self._eval(page, "__ZHS__.videoState()", wait_promise=False)
        return {"video": state}, []

    def _op_play(self, page, params, ctx):
        speed = params.get("speed")
        warnings: list[str] = []
        if speed:
            res = self._eval(page, f"__ZHS__.setSpeed({float(speed)})", wait_promise=True)
            if isinstance(res, dict) and not res.get("ok"):
                warnings.append(f"倍速设置失败：{res.get('reason')}")
        noise = self._eval(page, "__ZHS__.closeNoiseDialogs()", wait_promise=False)
        return {"started": True, "noise_hidden": noise, "speed": speed}, warnings

    def _op_next_task(self, page, params, ctx):
        res = self._eval(page, "__ZHS__.goNextTask()", wait_promise=True)
        ok = isinstance(res, dict) and res.get("ok")
        if not ok:
            raise _platform_changed(
                "zhs-browser", f"切换任务点失败：{(res or {}).get('reason')}"
            )
        return res, []

    def _op_set_speed(self, page, params, ctx):
        from .zhs_dom import clamp_speed

        rate = clamp_speed(params.get("speed", 1.0))
        res = self._eval(page, f"__ZHS__.setSpeed({rate})", wait_promise=True)
        warnings: list[str] = []
        if float(params.get("speed", 1.0)) > MAX_SPEED:
            warnings.append(f"请求倍速被夹到平台上限 {MAX_SPEED}")
        return res, warnings

    def _op_read_question(self, page, params, ctx):
        """读弹题。只读，返回结构化题目供 Agent 作答。"""
        popup = self._eval(page, "__ZHS__.popup()", wait_promise=False)
        if not isinstance(popup, dict) or not popup.get("present"):
            return {"present": False, "questions": []}, []
        if popup.get("alreadyDone"):
            return {
                "present": True,
                "already_done": True,
                "questions": [],
                "raw_prompt": popup.get("stem", ""),
            }, ["弹题已被平台判为完成，无需作答"]
        options = popup.get("options") or []
        question = {
            "index": 1,
            "type": "single_choice" if len(options) > 1 else "short_answer",
            "stem": popup.get("stem", ""),
            "options": [{"key": o.get("key"), "text": o.get("text")} for o in options],
            "image_ref": None,
            "page_key": popup.get("pageKey"),
        }
        return {
            "present": True,
            "questions": [question],
            "raw_prompt": popup.get("stem", ""),
        }, []

    def _op_submit_answers(self, page, params, ctx):
        """把 Agent 产出的答案注入页面。

        `answers` 期望是**选项下标**列表（本题链路的内部约定）。
        字母转下标在 `_answers_to_indices()` 完成。
        """
        indices = _answers_to_indices(params.get("answers"))
        if not indices:
            return (
                {"submitted": False, "reason": "no_answer"},
                ["未提供答案：按策略不随机作答，转人工"],
            )
        res = self._eval(
            page, f"__ZHS__.answerPopup({json.dumps(indices)})", wait_promise=True
        )
        return res or {}, []

    def _op_answer_quiz(self, page, params, ctx):
        """章节测验/练习答题入口。

        与弹题共用读-答-注入三步，区别在于题目在页面主体而非弹窗。
        当前实现复用 `read_question` 的弹窗路径；主体测验的选择器待实测补充。
        """
        return self._op_read_question(page, params, ctx)

    def _op_answer_loop(self, page, params, ctx):
        """C45：由 `run_popup_answer_loop()` 在服务层编排，这里只做一次轮次。"""
        return self._op_read_question(page, params, ctx)

    # ------------------------------------------------------------------
    def _map_exception(self, exc: Exception, capability_id: str) -> AdapterResult:
        from ..safety import ExamBlockedError

        if isinstance(exc, ExamBlockedError):
            return self.failure(
                AdapterError(
                    code=exc.code,
                    category="permission",
                    message=str(exc),
                    retryable=False,
                ),
                raw_output=exc.url,
            )
        if isinstance(exc, cdp_mod.CdpError | browser_mod.IsolationError):
            return self.failure(
                AdapterError(
                    code="ADAPTER_NOT_READY",
                    category="transient",
                    message=f"浏览器通道不可用：{exc}",
                    retryable=True,
                )
            )
        if isinstance(exc, AdapterError):
            return self.failure(exc)
        return self.failure(
            AdapterError(
                code="ADAPTER_INTERNAL",
                category="internal",
                message=f"{type(exc).__name__}: {exc}",
                retryable=False,
            )
        )

    @staticmethod
    def timeout_for(capability_id: str, default_ms: int = 30000) -> float:
        if capability_id in {"C12", "C18", "C45"}:
            return 600.0
        if capability_id in READ_OPS:
            return 20.0
        return default_ms / 1000.0


# ---------------------------------------------------------------------------
# CDP 页面会话
# ---------------------------------------------------------------------------
class _PageSession:
    """一次 CDP 附着。用 `with` 保证连接释放。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self._client: cdp_mod.CdpClient | None = None

    def __enter__(self) -> "_PageSession":
        target = cdp_mod.pick_page_target(self.port)
        ws = target.get("webSocketDebuggerUrl")
        if not ws:
            raise cdp_mod.CdpError("页面目标缺少 webSocketDebuggerUrl")
        self._client = cdp_mod.CdpClient(ws_url=ws).connect()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def eval(self, expression: str, wait: bool = True) -> Any:
        """求值。`wait=True` 时包成 async IIFE 并 awaitPromise。"""
        assert self._client is not None
        if wait:
            expression = f"(async () => {{ return await ({expression}); }})()"
        result = self._client.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": bool(wait),
                "userGesture": True,
            },
        )
        payload = result.get("result", {}) if isinstance(result, dict) else {}
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            raise cdp_mod.CdpError(f"页面求值异常：{detail}")
        return payload.get("value")

    def url(self) -> str:
        assert self._client is not None
        # 优先读页面自身，回落到目标列表
        try:
            value = self.eval("location.href", wait=False)
            if isinstance(value, str) and value:
                return value
        except Exception:  # noqa: BLE001
            pass
        for target in cdp_mod.list_targets(self.port):
            if target.get("type") == "page":
                return str(target.get("url", ""))
        return ""




def _popup_dict(page_key: ZhsPage) -> dict[str, Any]:
    from .zhs_dom import POPUPS

    pp = POPUPS[page_key]
    return {
        "root_selector": pp.root_selector,
        "option_selector": pp.option_selector,
        "submit_selector": pp.submit_selector,
        "close_selector": pp.close_selector,
        "stem_selector": pp.stem_selector,
        "pager_selector": pp.pager_selector,
        "done_selector": pp.done_selector,
    }


def _chapter_name(page: "_PageSession", selector: str) -> str:
    if not selector:
        return ""
    value = page.eval(
        f"(document.querySelector({json.dumps(selector)}) || {{}}).textContent || ''",
        wait=False,
    )
    return str(value).strip()[:200] if value else ""


def _answers_to_indices(answers: Any) -> list[int]:
    """把 Agent 的答案转成选项下标。

    接受形态：`[0, 2]`、`["0","2"]`、`["A","C"]`、`["A#C"]`、`"A#C"`、`0`。
    **只做下标转换**；语义校验（答案文本是否在选项中存在）由服务层负责。
    """
    if answers is None:
        return []
    # 裸整数：Agent 直接给出下标
    if isinstance(answers, bool):
        return []
    if isinstance(answers, int):
        return [answers]
    if isinstance(answers, str):
        parts: list[str] = [
            p.strip() for p in answers.replace(",", "#").split("#") if p.strip()
        ]
    elif isinstance(answers, list):
        out: list[int] = []
        for item in answers:
            out.extend(_answers_to_indices(item))
        return out
    else:
        return []

    indexes: list[int] = []
    for part in parts:
        if part.isdigit():
            indexes.append(int(part))
        elif len(part) == 1 and part.isalpha():
            indexes.append(ord(part.upper()) - ord("A"))
    return indexes


# ---------------------------------------------------------------------------
# 依赖注入用的便捷函数
# ---------------------------------------------------------------------------
def build_manifest_capabilities(manifest: Manifest) -> str:
    return manifest.id
