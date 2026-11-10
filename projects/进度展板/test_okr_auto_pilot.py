"""OKR pilot tests; fixtures are in memory or auto-cleaned temporary directories."""
import unittest

import okr_auto_pilot as pilot


def kr(number="KR2.1", title="派单基线统计", context=""):
    return pilot.KR(number, title, "⚪ 未测", "Hermes", "本地统计报告", context)


class DecisionTests(unittest.TestCase):
    def test_dangerous_tasks_and_manual_approval_are_never_proposed(self):
        for title in ("删有效内容", "删除wiki", "rm -rf", "清空", "重置", "secrets",
                      "密钥", "API_KEY", "花钱", "充值", "换模型", "改架构", "DROP", "TRUNCATE"):
            with self.subTest(title=title):
                decision = pilot.plan([kr(title=title)], [], {}, "2026-11-04")
                self.assertEqual(decision["proposals"], [])
                self.assertEqual(decision["skipped"][0]["reason"], "SKIPPED_NEEDS_HUMAN")
        result = pilot.plan([kr(context="需人工拍板")], [], {}, "2026-11-04")
        self.assertEqual(result["skipped"][0]["reason"], "SKIPPED_NEEDS_HUMAN")

    def test_alias_matching_has_digit_boundaries(self):
        self.assertTrue(pilot.covers("src-landing", "KR3.2"))
        self.assertTrue(pilot.covers("okr-kr32", "KR3.2"))
        self.assertTrue(pilot.covers("eval-KR3.2-20261104", "KR3.2"))
        self.assertFalse(pilot.covers("okr-kr321", "KR3.2"))
        self.assertFalse(pilot.covers("okr-review-oc-v3", "KR3.2"))

    def test_capacity_cycle_and_daily_limits(self):
        rows = [kr("KR2.1"), kr("KR2.2"), kr("KR2.3"), kr("KR3.1")]
        result = pilot.plan(rows, [], {}, "2026-11-04")
        self.assertEqual(len(result["proposals"]), 2)
        items = [{"batch": "unrelated-1", "executor": "Codex", "status": "running"},
                 {"batch": "unrelated-2", "executor": "codex exec", "status": "running"}]
        result = pilot.plan(rows, items, {}, "2026-11-04")
        self.assertTrue(all(p["executor"] != "codex" for p in result["proposals"]))
        state = {"daily": {"2026-11-04": 20}}
        self.assertFalse(pilot.plan(rows, [], state, "2026-11-04")["proposals"])
        state["daily"]["2026-11-04"] = 19
        self.assertEqual(len(pilot.plan(rows, [], state, "2026-11-04")["proposals"]), 1)

    def test_failed_coverage_blocks_new_batch_but_owned_retry_is_bounded(self):
        item = {"batch": "okr-kr21", "executor": "codex", "status": "failed_needs_redispatch"}
        result = pilot.plan([kr()], [item], {}, "2026-11-04")
        self.assertFalse(result["proposals"], "legacy failures need a verified acceptance contract")
        state = {"batches": {"okr-kr21": {"kr": "KR2.1", "status": "failed_needs_redispatch",
                                                "redispatches": 1}}}
        result = pilot.plan([kr()], [item], state, "2026-11-04")
        self.assertEqual(result["proposals"][0]["batch"], "okr-kr21")
        state["batches"]["okr-kr21"]["redispatches"] = 2
        result = pilot.plan([kr()], [item], state, "2026-11-04")
        self.assertFalse(result["proposals"])
        self.assertEqual(result["needs_human"], ["okr-kr21"])

    def test_running_wins_over_older_failed_alias(self):
        items = [{"batch": "src-landing", "status": "failed_needs_redispatch"},
                 {"batch": "kr32-src-landing", "status": "running", "executor": "dsh"}]
        result = pilot.plan([kr("KR3.2")], items, {}, "2026-11-04")
        self.assertFalse(result["proposals"])
        self.assertEqual(result["skipped"][0]["reason"], "COVERED_RUNNING")

    def test_crash_reservation_and_closed_journal_cannot_be_redispatched(self):
        for status in ("launching", "running", "needs_human", "closed"):
            state = {"batches": {"okr-kr21": {"kr": "KR2.1", "status": status}}}
            self.assertFalse(pilot.plan([kr()], [], state, "2026-11-04")["proposals"])


class ParserTests(unittest.TestCase):
    def test_parses_real_nine_krs_without_guardrails(self):
        source = pilot.DEFAULT_ROOT / "wiki/总网流程/OKR-2026Q4.md"
        rows = pilot.parse_okr(source.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 9)
        self.assertEqual(rows[0].id, "KR1.1")
        self.assertEqual(rows[0].status, "⚪ 未测")
        self.assertEqual(rows[0].owner, "Codex")
        self.assertIn("rag/eval_ragas.py", rows[0].evidence)
        self.assertIn("I1.3", rows[0].context)
        self.assertEqual(rows[-1].id, "KR3.3")


if __name__ == "__main__":
    unittest.main()
