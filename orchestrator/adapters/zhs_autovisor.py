"""Autovisor 子进程 Adapter（视频写侧主力）。

设计完全对齐 `cx` 项目的 `chaoxing_cli.py`：同样的 `_call_short` / `_call_stream`
两种调用形态、同样的退出码语义（0/1/3/4）、同样的"Router 不调用 setup，
准备必须在 invoke 里幂等自愈"铁律。

## 责任边界

- **负责**：C12 视频、C18 切章、C20 倍速、C01/C02 登录、C05 滑块、C03 重登兜底
- **不负责**：C06–C11 读侧（那是 `zhs-browser` 的活）。
  原因见 `docs/01 §2.2`：Autovisor 是"给课程链接就跑"的独立程序，
  它没有"列出我账号下有哪些课"的接口——读侧必须走浏览器。

## 浏览器隔离（重要）

Autovisor **自带**浏览器启动逻辑（`config.ini` 的 `driver = Edge`），
它启动的是**它自己**的浏览器实例，与本项目的 `.browser/edge-profile` 无关。
因此本 Adapter 与 `zhs-browser` **不可同时运行**——两套浏览器驱动同一账号会互相干扰。

这条约束由 `limits.max_concurrency = 1` 与 `router` 的账号级限流保证，
并在 `manifest.note` 里显式写明。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..errors import AdapterError, Codes, ErrorCategory
from ..models import AccountContext, AdapterResult, ProbeResult, TaskContext
from ..registry import Manifest
from ..safety import assert_writable
from .base import Adapter
from .zhs_worker import EXIT_AUTH, EXIT_DEPS, EXIT_INTERNAL, EXIT_OK

#: 能力 → worker op
OP_BY_CAPABILITY: dict[str, str] = {
    "C12": "run_video",
    "C18": "run_video",
    "C20": "run_video",
    "C01": "run_video",
    "C02": "ping",
    "C03": "ping",
    "C05": "run_video",
}

WRITE_CAPABILITIES: frozenset[str] = frozenset({"C12", "C18", "C20", "C01", "C05"})


class ZhsAutovisorAdapter(Adapter):
    def __init__(self, manifest: Manifest, root: Path | None = None) -> None:
        super().__init__(manifest)
        self._root = root or Path(__file__).resolve().parents[2]
        self._workdir: Path | None = None

    # ------------------------------------------------------------------
    # 准备
    # ------------------------------------------------------------------
    def setup(self, account: AccountContext) -> None:
        """幂等准备：只记录账号工作区，**不写任何文件**。

        config.ini 由 worker 在 `invoke` 时按需生成（写到账号工作区），
        这样 `--dry-run` 不会留下垃圾配置。
        """
        self._workdir = Path(account.workdir)
        self._setup_done = True

    def _ensure_workdir(self, ctx: TaskContext) -> Path:
        if self._workdir is not None:
            workdir = self._workdir
        else:
            workdir = self._root / "accounts" / ctx.account_id
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir

    # ------------------------------------------------------------------
    # 解释器解析
    # ------------------------------------------------------------------
    def _python(self) -> str:
        """worker 与上游所需的解释器。

        优先级：`ZHS_PYTHON` 环境变量 → 项目 `.venv` → 当前解释器。
        上游依赖（playwright 等）装在项目 `.venv`，**不装进统一层的运行环境**。
        """
        env = os.environ.get("ZHS_PYTHON")
        if env and Path(env).exists():
            return env
        for candidate in (
            self._root / ".venv" / "Scripts" / "python.exe",
            self._root / ".venv" / "bin" / "python",
        ):
            if candidate.exists():
                return str(candidate)
        return sys.executable

    def _base_payload(self, op: str, args: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
        workdir = self._ensure_workdir(ctx)
        return {
            "op": op,
            "args": args,
            "account": ctx.account_id,
            "workdir": str(workdir),
            "upstream_dir": str(self._root / "upstreams" / "Autovisor"),
        }

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    def _call_short(
        self, op: str, args: dict[str, Any], ctx: TaskContext, timeout_s: float
    ) -> tuple[int, dict[str, Any]]:
        """一次性调用：取 stdout 的最后一行 JSON 作为结果。"""
        payload = self._base_payload(op, args, ctx)
        env = self._env()
        proc = subprocess.run(
            [self._python(), "-m", "orchestrator.adapters.zhs_worker"],
            input=json.dumps(payload, ensure_ascii=False) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            cwd=str(self._root),
            env=env,
        )
        stdout = (proc.stdout or "").strip()
        parsed: dict[str, Any] = {}
        if stdout:
            try:
                parsed = json.loads(stdout.splitlines()[-1])
            except json.JSONDecodeError:
                parsed = {"ok": False, "code": "ADAPTER_CRASHED", "raw": stdout[-2000:]}
        return proc.returncode, parsed

    def _call_stream(
        self,
        op: str,
        args: dict[str, Any],
        ctx: TaskContext,
        timeout_s: float,
    ) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
        """流式调用：边跑边把事件转发给 ctx.progress_sink。"""
        payload = self._base_payload(op, args, ctx)
        proc = subprocess.Popen(
            [self._python(), "-m", "orchestrator.adapters.zhs_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self._root),
            env=self._env(),
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        proc.stdin.flush()

        events: list[dict[str, Any]] = []
        final: dict[str, Any] = {}
        started = time.monotonic()
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "event" in item:
                    events.append(item)
                    self._forward_event(item, ctx)
                else:
                    final = item
                if ctx.cancelled:
                    proc.kill()
                    break
                if time.monotonic() - started > timeout_s:
                    proc.kill()
                    events.append({"event": "timeout", "after_s": timeout_s})
                    break
            code = proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
        return code, final, events

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        env["ZHS_UPSTREAM_DIR"] = str(self._root / "upstreams" / "Autovisor")
        return env

    def _forward_event(self, payload: dict[str, Any], ctx: TaskContext) -> None:
        name = str(payload.get("event", ""))
        if name == "progress":
            ctx.report(
                "task_point.progress",
                percent=payload.get("percent"),
                raw=payload.get("raw", ""),
            )
        elif name == "chapter":
            ctx.report("chapter.started", chapter_name=payload.get("chapter", ""))
        elif name == "risk_marker":
            ctx.report("risk.control_detected", raw=payload.get("raw", ""))
        elif name == "auth_marker":
            ctx.report("auth.expired", raw=payload.get("raw", ""))
        elif name == "stall_detected":
            ctx.report("task_point.stalled", raw=payload.get("raw", ""))
        else:
            ctx.report(f"upstream.{name}", raw=payload.get("raw", ""))

    # ------------------------------------------------------------------
    def _error_from_exit(
        self, code: int, parsed: dict[str, Any], op: str
    ) -> AdapterError:
        message = str(parsed.get("message") or f"worker 退出码 {code}")
        hint = str(parsed.get("hint") or "")
        raw = str(parsed.get("raw") or "")
        if code == EXIT_AUTH:
            return AdapterError(
                code=Codes.SESSION_INVALID,
                category=ErrorCategory.AUTH,
                message=f"{message}；{hint}".strip("；"),
                retryable=False,
                adapter_raw=raw,
            )
        if code == EXIT_DEPS:
            return AdapterError(
                code=Codes.ADAPTER_NOT_READY,
                category=ErrorCategory.TRANSIENT,
                message=f"{message}；{hint}".strip("；"),
                retryable=True,
                adapter_raw=raw,
            )
        return AdapterError(
            code=str(parsed.get("code") or Codes.ADAPTER_ERROR),
            category=ErrorCategory.INTERNAL if code == EXIT_INTERNAL else ErrorCategory.TRANSIENT,
            message=f"{message}；{hint}".strip("；"),
            retryable=code != EXIT_INTERNAL,
            adapter_raw=raw,
        )

    # ------------------------------------------------------------------
    def invoke(
        self, capability_id: str, params: dict[str, Any], ctx: TaskContext
    ) -> AdapterResult:
        op = OP_BY_CAPABILITY.get(capability_id)
        if op is None:
            return self.unsupported(capability_id)

        # ---- 写能力：先过考试守卫（目标 5 的强制落点） ----
        if capability_id in WRITE_CAPABILITIES and not ctx.dry_run:
            url = str(params.get("course_url") or params.get("url") or "")
            try:
                assert_writable(url, "play", page_text=str(params.get("page_text", "")))
            except Exception as exc:  # ExamBlockedError
                code = getattr(exc, "code", Codes.EXAM_PAGE_BLOCKED)
                return self.failure(
                    AdapterError(
                        code=code,
                        category=ErrorCategory.PERMISSION,
                        message=str(exc),
                        retryable=False,
                    ),
                    raw_output=url,
                )

        timeout_s = self.timeout_for(capability_id)
        try:
            if op == "run_video":
                code, final, events = self._call_stream(op, params, ctx, timeout_s)
            else:
                code, final = self._call_short(op, params, ctx, timeout_s)
                events = []
        except subprocess.TimeoutExpired:
            return self.failure(
                AdapterError(
                    code=Codes.ADAPTER_TIMEOUT,
                    category=ErrorCategory.TRANSIENT,
                    message=f"Autovisor 超时（{timeout_s:.0f}s）",
                    retryable=True,
                )
            )
        except FileNotFoundError as exc:
            return self.failure(
                AdapterError(
                    code=Codes.ADAPTER_NOT_READY,
                    category=ErrorCategory.TRANSIENT,
                    message=f"worker 解释器不可用：{exc}",
                    retryable=True,
                )
            )

        if code != EXIT_OK or not final.get("ok"):
            return self.failure(
                self._error_from_exit(code, final, op),
                raw_output=str(final.get("raw") or "") or json.dumps(final, ensure_ascii=False),
            )

        data = final.get("data") or {}
        warnings: list[str] = []
        if op == "run_video" and not data.get("dry_run"):
            warnings.append(
                "Autovisor 在课中弹题时只暂停、不作答；弹题作答请走 zhs-browser 的 Agent 链路"
            )
        return self.success(data, warnings=warnings)

    def cancel(self, ctx: TaskContext) -> None:
        """协作式取消：置标志。worker 在下一行输出时感知并退出。"""
        ctx.cancel_event.set()

    # ------------------------------------------------------------------
    def probe(self) -> ProbeResult:
        """探活：只做 `ping`（检查上游与依赖是否就位），**不启动浏览器**。"""
        started = time.monotonic()
        fake = TaskContext(request_id="probe", account_id="probe")
        try:
            code, parsed = self._call_short("ping", {}, fake, timeout_s=30.0)
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                healthy=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                detail=f"worker 不可用：{exc}",
            )
        latency = int((time.monotonic() - started) * 1000)
        if code == EXIT_OK and parsed.get("ok"):
            return ProbeResult(healthy=True, latency_ms=latency, detail="上游与依赖就位")
        return ProbeResult(
            healthy=False,
            latency_ms=latency,
            detail=str(parsed.get("hint") or parsed.get("message") or f"退出码 {code}"),
        )

    @staticmethod
    def timeout_for(capability_id: str, default_ms: int = 30000) -> float:
        if capability_id in {"C12", "C18"}:
            return 7200.0  # 整课可能跑几小时
        if capability_id == "C01":
            return 300.0
        if capability_id in {"C02", "C03"}:
            return 60.0
        return default_ms / 1000.0
