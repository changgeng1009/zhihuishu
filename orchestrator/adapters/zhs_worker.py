"""Autovisor 子进程胶水（stdin/stdout JSON 协议）。

## 协议（与 `cx` 项目的 worker 完全一致）

- **入**：stdin 读一行 JSON `{"op": ..., "args": {...}, "account": ..., "workdir": ...}`
- **出**：stdout 逐行 JSON。`{"event": ...}` 是进度事件；最后一行是结果
  `{"ok": true, "data": {...}}` 或 `{"ok": false, "code": ..., "message": ..., "hint": ...}`
- **退出码**：`0` 成功 / `1` 内部错误 / `3` 认证失败 / `4` 依赖缺失

## 为什么是 worker 而不是 import

1. **红线 R2**：`upstreams/Autovisor` 只读。worker 通过 `--config <账号工作区>/config.ini`
   把配置写到**项目内的账号工作区**，上游目录一个字节都不动。
2. **依赖隔离**：Autovisor 需要 `playwright / httpx / pillow`，统一层是 stdlib 零依赖。
   两套解释器必须分开（worker 用项目 `.venv`）。
3. **崩溃隔离**：浏览器驱动崩了不该带走统一层。

## 上游行为映射（来自 Autovisor README 与源码）

| op | 上游调用 | 写副作用 |
|---|---|---|
| `ping` | 无（只 import 探测） | 无 |
| `check_browser` | `--check-browser` | 无（官方声明） |
| `check_course` | `--check-course URL` | 无（官方声明：阻止进度上报和自动播放） |
| `run_video` | 默认流程（读 `--config`） | 有（真实播放 + 进度上报） |

⚠️ **`check_course` 是官方声明的只读命令**，所以 `probe` / `scan_tasks` 走它，
不自己造"读课程目录"的实现——这是"优先复用"的落点。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_AUTH = 3
EXIT_DEPS = 4

#: 风控/验证信号（与 orchestrator/risk.py 的 SIGNATURES 保持同源语义）
RISK_MARKERS = ("操作过于频繁", "请稍后再试", "访问受限", "安全验证", "验证码", "异常操作")

AUTH_MARKERS = ("登录失败", "账号或密码", "请先登录", "未登录", "登录已失效", "认证失败")


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _fail(code: int, message: str, hint: str = "", **extra: Any) -> int:
    _emit({"ok": False, "code": message, "message": message, "hint": hint, **extra})
    return code


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------
def _upstream_dir() -> Path:
    """上游目录。由调用方通过 `ZHS_UPSTREAM_DIR` 传入，默认按约定推导。"""
    env = os.environ.get("ZHS_UPSTREAM_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "upstreams" / "Autovisor"


def _check_deps() -> tuple[bool, str]:
    """探测上游依赖是否可用。只 import，不启动浏览器。"""
    try:
        import playwright  # noqa: F401
    except ImportError as exc:
        return False, f"缺少 playwright：{exc}"
    return True, ""


def _write_config(
    workdir: Path,
    course_url: str,
    *,
    speed: float | None = None,
    limit_min: int = 30,
    driver: str = "Edge",
    username: str = "",
    password: str = "",
    sound_off: bool = True,
) -> Path:
    """把 config.ini 写到**账号工作区**（绝不写 upstreams/）。"""
    workdir.mkdir(parents=True, exist_ok=True)
    from .zhs_dom import DEFAULT_SPEED, MAX_SPEED, clamp_speed

    # 用户要求 1.5 倍速（实测也是当前页面最高档），未显式指定时用默认值
    speed = clamp_speed(DEFAULT_SPEED if speed is None else speed)
    lines = [
        "[user-account]",
        f"username = {username}",
        f"password = {password}",
        "",
        "[browser-option]",
        f"driver = {driver}",
        "EXE_PATH =",
        "",
        "[script-option]",
        "enableAutoCaptcha = True",
        "enableHideWindow = False",
        "showDonateCode = False",
        "",
        "[course-option]",
        f"limitMaxTime = {limit_min}",
        f"limitSpeed = {speed}",
        f"soundOff = {sound_off}",
        "",
        "[course-url]",
        f"URL1 = {course_url}",
        "",
    ]
    path = workdir / "config.ini"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 输出解析
# ---------------------------------------------------------------------------
#: 进度事件的正则（Autovisor 的日志是中文文本，解析必须**容错**：
#: 匹配不到就降级为"能报进度但不报细节"，绝不能因此崩掉）
_RE_CHAPTER = re.compile(r"(?:章节|目录|课程)[:：]\s*(.+)")
_RE_PROGRESS = re.compile(r"(\d{1,3}(?:\.\d)?)\s*%")
_RE_STALL = re.compile(r"(进度|视频).{0,6}(无推进|长时间|停滞)")


def _parse_line(line: str) -> dict[str, Any] | None:
    """把一行原始 stdout 翻译成事件。识别不出返回 None。"""
    text = line.strip()
    if not text:
        return None
    if any(m in text for m in RISK_MARKERS):
        return {"event": "risk_marker", "raw": text[:500]}
    if any(m in text for m in AUTH_MARKERS):
        return {"event": "auth_marker", "raw": text[:500]}
    if _RE_STALL.search(text):
        return {"event": "stall_detected", "raw": text[:500]}
    m = _RE_PROGRESS.search(text)
    if m:
        return {"event": "progress", "percent": float(m.group(1)), "raw": text[:500]}
    m = _RE_CHAPTER.search(text)
    if m:
        return {"event": "chapter", "chapter": m.group(1).strip()[:200], "raw": text[:500]}
    if "学习完成" in text or "全部完成" in text or "课程完成" in text:
        return {"event": "course_finished", "raw": text[:500]}
    return None


def _run_upstream(
    cmd: list[str],
    cwd: Path,
    timeout_s: float,
    stream: bool,
) -> tuple[int, str, list[dict[str, Any]]]:
    """跑上游并（可选）流式转发事件。返回 `(退出码, 全部输出, 事件列表)`。"""
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    # 有 HTTP_PROXY 时必须豁免本机，否则上层代理会 502（cx 项目踩过的坑）
    no_proxy = env.get("NO_PROXY", "")
    for host in ("127.0.0.1", "localhost"):
        if host not in no_proxy:
            no_proxy = f"{no_proxy},{host}".strip(",")
    env["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy

    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    events: list[dict[str, Any]] = []
    collected: list[str] = []
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            collected.append(line.rstrip("\n"))
            if stream:
                parsed = _parse_line(line)
                if parsed:
                    events.append(parsed)
                    _emit(parsed)
            if time.monotonic() - started > timeout_s:
                proc.kill()
                events.append({"event": "timeout", "after_s": timeout_s})
                break
        code = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
    return code, "\n".join(collected), events


def _classify_exit(code: int, output: str) -> int:
    """把上游退出码 + 输出映射成本 worker 的退出码。"""
    if code == 0:
        return EXIT_OK
    if any(m in output for m in RISK_MARKERS):
        return EXIT_INTERNAL  # 风控由统一层的 risk.py 识别，这里只保证不误判成 auth
    if any(m in output for m in AUTH_MARKERS):
        return EXIT_AUTH
    return EXIT_INTERNAL


# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------
def op_ping(args: dict[str, Any], workdir: Path) -> int:
    upstream = _upstream_dir()
    entry = upstream / "Autovisor.py"
    if not entry.exists():
        return _fail(
            EXIT_DEPS,
            "ADAPTER_NOT_READY",
            hint=f"未找到上游入口 {entry}；请先 clone（见 upstreams.lock.json）",
        )
    ok, detail = _check_deps()
    if not ok:
        return _fail(EXIT_DEPS, "DEPS_MISSING", hint=detail)
    _emit({"ok": True, "data": {"upstream": "Autovisor", "entry": str(entry)}})
    return EXIT_OK


def op_check_browser(args: dict[str, Any], workdir: Path) -> int:
    upstream = _upstream_dir()
    python = sys.executable
    code, output, _ = _run_upstream(
        [python, str(upstream / "Autovisor.py"), "--check-browser"],
        cwd=workdir,
        timeout_s=float(args.get("timeout_s", 120)),
        stream=False,
    )
    verdict = _classify_exit(code, output)
    if verdict != EXIT_OK:
        return _fail(
            verdict,
            "CHECK_BROWSER_FAILED",
            hint="Autovisor --check-browser 未通过；常见原因：浏览器未安装、未登录并保存 cookie",
            raw=output[-2000:],
        )
    _emit({"ok": True, "data": {"check": "browser", "raw_tail": output[-2000:]}})
    return EXIT_OK


def op_check_course(args: dict[str, Any], workdir: Path) -> int:
    """只读：检查课程目录选择器。**不上报进度、不自动播放**（上游官方声明）。"""
    url = args.get("course_url") or args.get("url")
    if not url:
        return _fail(EXIT_INTERNAL, "INVALID_PARAM", hint="check_course 需要 --url")
    upstream = _upstream_dir()
    config_path = _write_config(workdir, url, speed=1.0, limit_min=0)
    code, output, _ = _run_upstream(
        [
            sys.executable,
            str(upstream / "Autovisor.py"),
            "--config",
            str(config_path),
            "--check-course",
            url,
        ],
        cwd=workdir,
        timeout_s=float(args.get("timeout_s", 180)),
        stream=False,
    )
    verdict = _classify_exit(code, output)
    if verdict != EXIT_OK:
        return _fail(
            verdict,
            "CHECK_COURSE_FAILED",
            hint="课程目录识别失败；可能是课程链接类型不支持或需重新登录",
            raw=output[-2000:],
        )
    _emit(
        {
            "ok": True,
            "data": {
                "check": "course",
                "url": url,
                "raw_tail": output[-4000:],
            },
        }
    )
    return EXIT_OK


def op_run_video(args: dict[str, Any], workdir: Path) -> int:
    """写操作：真实播放课程视频并上报进度。"""
    from .zhs_dom import DEFAULT_SPEED

    url = args.get("course_url") or args.get("url")
    if not url:
        return _fail(EXIT_INTERNAL, "INVALID_PARAM", hint="run_video 需要 --url")

    if args.get("dry_run"):
        _emit(
            {
                "ok": True,
                "data": {
                    "dry_run": True,
                    "would_run": "Autovisor",
                    "course_url": url,
                    "speed": args.get("speed", DEFAULT_SPEED),
                    "limit_min": args.get("limit_min", 30),
                },
            }
        )
        return EXIT_OK

    upstream = _upstream_dir()
    config_path = _write_config(
        workdir,
        url,
        speed=float(args.get("speed", DEFAULT_SPEED)),
        limit_min=int(args.get("limit_min", 30)),
        username=str(args.get("username", "")),
        password=str(args.get("password", "")),
    )
    _emit({"event": "upstream_start", "config": str(config_path), "course_url": url})

    code, output, events = _run_upstream(
        [sys.executable, str(upstream / "Autovisor.py"), "--config", str(config_path)],
        cwd=workdir,
        timeout_s=float(args.get("timeout_s", 7200)),
        stream=True,
    )
    verdict = _classify_exit(code, output)
    if verdict != EXIT_OK:
        return _fail(
            verdict,
            "RUN_VIDEO_FAILED",
            hint="Autovisor 非零退出；原始输出见 runs/*.raw.log",
            raw=output[-4000:],
        )
    _emit(
        {
            "ok": True,
            "data": {
                "course_url": url,
                "events": len(events),
                "raw_tail": output[-4000:],
            },
        }
    )
    return EXIT_OK


OPS = {
    "ping": op_ping,
    "check_browser": op_check_browser,
    "check_course": op_check_course,
    "run_video": op_run_video,
}


def main(argv: list[str] | None = None) -> int:
    raw = sys.stdin.readline()
    if not raw.strip():
        return _fail(EXIT_INTERNAL, "INVALID_PARAM", hint="未收到请求 JSON")
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _fail(EXIT_INTERNAL, "INVALID_PARAM", hint=f"请求不是合法 JSON：{exc}")

    op = request.get("op", "")
    args = request.get("args") or {}
    workdir = Path(request.get("workdir") or os.getcwd())

    handler = OPS.get(op)
    if handler is None:
        return _fail(EXIT_INTERNAL, "INVALID_PARAM", hint=f"未知 op：{op}")

    try:
        return handler(args, workdir)
    except FileNotFoundError as exc:
        return _fail(EXIT_DEPS, "DEPS_MISSING", hint=str(exc))
    except Exception as exc:  # noqa: BLE001 - worker 必须把异常变成退出码
        return _fail(EXIT_INTERNAL, "ADAPTER_ERROR", hint=f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
