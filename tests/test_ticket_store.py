# -*- coding: utf-8 -*-
"""ticket_store：原子写 / 去重 / 答案回读 / 超时归档 的回归测试。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.answer_broker import TicketState
from orchestrator import ticket_store as ts


def make_ticket(**kw):
    stem = "泰勒于1871年出版了《原始文化》一书。"
    kw.setdefault("raw_prompt", f"1、[判断题] {stem}A对 B错")
    kw.setdefault(
        "questions",
        [{"index": 1, "stem": stem,
          "type": "judge", "options": [{"letter": "A", "text": "对"}]}],
    )
    kw.setdefault("dedupe", ts.dedupe_key(stem))
    return ts.build_ticket(**kw)


class TestDedupeKey(unittest.TestCase):
    def test_same_question_diff_punctuation_same_key(self):
        a = ts.dedupe_key("1、[判断题] 泰勒于1871年出版了《原始文化》一书。")
        b = ts.dedupe_key("1.[判断题]泰勒于1871年出版了《原始文化》一书")
        self.assertEqual(a, b)

    def test_diff_question_diff_key(self):
        a = ts.dedupe_key("泰勒出版了《原始文化》")
        b = ts.dedupe_key("数学的文化特征有哪些")
        self.assertNotEqual(a, b)

    def test_key_is_stable_and_short(self):
        k = ts.dedupe_key("任意题干")
        self.assertEqual(k, ts.dedupe_key("任意题干"))
        self.assertLessEqual(len(k), 16)


class TestAtomicPending(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_save_pending_is_atomic_and_complete(self):
        t = make_ticket()
        path = ts.save_pending(self.dir, t)
        self.assertTrue(path.is_file())
        # 无 .tmp 残留
        leftovers = list(self.dir.glob("*.tmp"))
        self.assertEqual(leftovers, [])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["ticket_id"], t.ticket_id)
        self.assertIn("raw_prompt", data)

    def test_no_tmp_left_after_many_writes(self):
        for i in range(5):
            ts.save_pending(self.dir, make_ticket(ticket_id=f"q{i}"))
        self.assertEqual(list(self.dir.glob("*.tmp")), [])


class TestAnswerFlow(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.ticket = make_ticket()
        ts.save_pending(self.dir, self.ticket)
        self.dedupe = self.ticket.note[len("dedupe:"):]

    def test_load_answer_missing_returns_none(self):
        self.assertIsNone(ts.load_answer(self.dir, self.ticket.ticket_id))

    def test_load_answer_list_and_string_forms(self):
        (self.dir / f"answer_{self.ticket.ticket_id}.json").write_text(
            json.dumps({"answers": ["A"]}), encoding="utf-8")
        self.assertEqual(ts.load_answer(self.dir, self.ticket.ticket_id), ["A"])
        (self.dir / f"answer_{self.ticket.ticket_id}.json").write_text(
            json.dumps({"answer": "a,b"}), encoding="utf-8")
        self.assertEqual(ts.load_answer(self.dir, self.ticket.ticket_id), ["A", "B"])

    def test_record_answered_indexes_and_archives(self):
        ts.record_answered(self.dir, self.ticket, ["A"])
        # 去重索引命中
        self.assertEqual(ts.known_answer(self.dir, self.dedupe), ["A"])
        # pending 已被归档
        self.assertFalse((self.dir / f"pending_{self.ticket.ticket_id}.json").exists())
        done = self.dir / f"done_{self.ticket.ticket_id}.json"
        self.assertTrue(done.is_file())
        data = json.loads(done.read_text(encoding="utf-8"))
        self.assertEqual(data["state"], str(TicketState.ANSWERED))
        self.assertEqual(data["answers"], ["A"])

    def test_dedupe_survives_index_reload(self):
        ts.record_answered(self.dir, self.ticket, ["A"])
        # 模拟新进程：重新走 known_answer
        self.assertEqual(ts.known_answer(self.dir, self.dedupe), ["A"])

    def test_corrupt_index_degrades_to_no_dedupe(self):
        (self.dir / ts.KEYS_FILE).write_text("{broken", encoding="utf-8")
        self.assertIsNone(ts.known_answer(self.dir, self.dedupe))

    def test_mark_timeout_archives_and_removes_pending(self):
        ts.mark_timeout(self.dir, self.ticket)
        self.assertFalse((self.dir / f"pending_{self.ticket.ticket_id}.json").exists())
        tout = self.dir / f"timeout_{self.ticket.ticket_id}.json"
        self.assertTrue(tout.is_file())
        self.assertIn("timeout", json.loads(tout.read_text(encoding="utf-8"))["note"])


if __name__ == "__main__":
    unittest.main()


class TestAtomicWritePublic(unittest.TestCase):
    def test_public_atomic_write(self):
        """v5 断点状态依赖的公开原子写入口（曾因缺此名崩在首轮 _save_state）。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "batch_state.json"
            ts.atomic_write(p, json.dumps({"done": ["x"]}, ensure_ascii=False))
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"done": ["x"]})
            self.assertFalse(p.with_suffix(".json.tmp").exists())   # 无残留 tmp
