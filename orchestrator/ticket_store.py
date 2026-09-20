# -*- coding: utf-8 -*-
"""弹题工单存储：统一 answer_broker.Ticket 结构 + 原子写 + 去重。

## 为什么存在

v4/v5 脚本此前各自手写 `pending_<时间戳>.json`：
- 非原子写（Agent 可能在半截 JSON 时读到）；
- 无去重（同一题在重播/换会话时重复弹工单，Agent 重复作答）；
- 工单结构自定义，与统一层的 `answer_broker.Ticket` 脱节。

## 设计

- 工单 = `answer_broker.Ticket`（ticket_id / raw_prompt / questions / answers…），
  序列化经 to_dict/from_dict，字段与 MCP / shim 完全一致；
- **原子写**：先写 `.tmp` 再 `os.replace` —— 读方要么看到完整旧文件，
  要么看到完整新文件，绝不读到半截；
- **去重键**：sha1(归一化题干)（去空白、去标点差异）。`answered_keys.json`
  记录 key → 答案。命中即**免等待直接回填**，重播/换会话不再重复打扰 Agent；
- `ticket_id` 仍按次生成（HMS），但同题重复弹出时复用首次的 ticket_id，
  保证「一题一档」。

单写互斥：本模块的所有写都是「临时文件 + replace」，天然避免并发写坏；
不需要文件锁（唯一写方是回放脚本，唯一读方是 Agent 与回放脚本自身）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from orchestrator.answer_broker import Question, Ticket, TicketState

#: 去重/状态索引文件（位于工单目录下）
KEYS_FILE = "answered_keys.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)          # 同盘原子替换


def atomic_write(path: Path, data: str) -> None:
    """公开原子写入口（断点状态等外部持久化共用同一实现）。"""
    _atomic_write(path, data)


def dedupe_key(stem: str) -> str:
    """归一化题干 → 稳定去重键。去空白/中英标点差异/题型前缀。"""
    norm = re.sub(r"[\s，。、；：？！“”‘’（）《》\[\]【】,.:;?!\"'()<>\[\]]", "", stem)
    norm = re.sub(r"^\d+[、.．]?\[(判断题|单选题|多选题)\]", "", norm)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def build_ticket(
    *,
    raw_prompt: str,
    questions: list[dict[str, Any]],
    dedupe: str,
    course_id: str = "",
    chapter_id: str = "",
    timeout_s: float = 300.0,
    ticket_id: str | None = None,
) -> Ticket:
    """由回放侧提取结果构造工单。questions 为 [{index,type,stem,options}]。"""
    return Ticket(
        ticket_id=ticket_id or time.strftime("q%H%M%S"),
        raw_prompt=raw_prompt,
        created_at=_now(),
        course_id=course_id,
        chapter_id=chapter_id,
        timeout_s=timeout_s,
        note=f"dedupe:{dedupe}",
        questions=[
            Question(
                index=int(q.get("index", i)),
                stem=str(q.get("stem", "")),
                question_type=str(q.get("type", "unknown")),
                options={str(k): str(v) for k, v in (q.get("options") or {}).items()}
                if isinstance(q.get("options"), dict)
                else [str(o) for o in (q.get("options") or [])],
            )
            for i, q in enumerate(questions)
        ],
    )


def _keys_path(store_dir: Path) -> Path:
    return store_dir / KEYS_FILE


def _load_keys(store_dir: Path) -> dict[str, Any]:
    p = _keys_path(store_dir)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}          # 索引损坏不致命：退化为不去重


def known_answer(store_dir: Path, dedupe: str) -> list[str] | None:
    """该题以前答过 → 直接返回答案（免等待）。"""
    entry = _load_keys(store_dir).get(dedupe)
    if not entry:
        return None
    ans = entry.get("answers")
    return [str(a) for a in ans] if ans else None


def save_pending(store_dir: Path, ticket: Ticket) -> Path:
    """原子落盘待答工单，返回 pending 文件路径。"""
    store_dir.mkdir(parents=True, exist_ok=True)
    path = store_dir / f"pending_{ticket.ticket_id}.json"
    _atomic_write(path, json.dumps(ticket.to_dict(), ensure_ascii=False, indent=2))
    return path


def load_answer(store_dir: Path, ticket_id: str) -> list[str] | None:
    """读 Agent 的答案文件（pending_<id>.json 的应答）。"""
    p = store_dir / f"answer_{ticket_id}.json"
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    ans = raw.get("answers", raw.get("answer"))
    if ans is None:
        return None
    if isinstance(ans, str):
        return [a.strip().upper() for a in re.split(r"[,\s，、]+", ans) if a.strip()]
    return [str(a).strip().upper() for a in ans]


def record_answered(store_dir: Path, ticket: Ticket, answers: list[str]) -> Path:
    """写去重索引 + 工单归档（done_<id>.json，原子替换 pending）。"""
    keys = _load_keys(store_dir)
    key = ""
    if ticket.note.startswith("dedupe:"):
        key = ticket.note[len("dedupe:"):]
    if key:
        keys[key] = {
            "ticket_id": ticket.ticket_id,
            "answers": answers,
            "answered_at": _now(),
        }
        _atomic_write(_keys_path(store_dir), json.dumps(keys, ensure_ascii=False, indent=2))
    ticket.state = TicketState.ANSWERED
    ticket.answers = answers
    ticket.answered_at = _now()
    done = store_dir / f"done_{ticket.ticket_id}.json"
    _atomic_write(done, json.dumps(ticket.to_dict(), ensure_ascii=False, indent=2))
    pending = store_dir / f"pending_{ticket.ticket_id}.json"
    if pending.is_file():
        pending.unlink()
    return done


def mark_timeout(store_dir: Path, ticket: Ticket) -> Path:
    """超时未答：归档为 timeout_<id>.json（供人工补答与复盘）。"""
    ticket.note = (ticket.note + " |timeout").strip(" |")
    path = store_dir / f"timeout_{ticket.ticket_id}.json"
    _atomic_write(path, json.dumps(ticket.to_dict(), ensure_ascii=False, indent=2))
    pending = store_dir / f"pending_{ticket.ticket_id}.json"
    if pending.is_file():
        pending.unlink()
    return path
