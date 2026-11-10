"""展板回归测试；仅内存样本，不生成第二份 HTML。"""
import datetime
import re
import unittest
from unittest.mock import patch

import render_dashboard as dashboard


def row(date, progress="人工进展", next_step="人工下一步 &amp; <b>保留</b>"):
    return '<tr data-manual="yes">' + ''.join(
        f'<td contenteditable="true">{c}</td>'
        for c in (date, "手工项目", progress, next_step)
    ) + '</tr>'


class CheckinTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "now": datetime.datetime(2026, 11, 4, 9, 15),
            "wiki": {"total": 200, "deep": 150},
            "ragdb": {"sources": 268, "chunks": 7712, "books": 11},
            "tasks": {"done": 8, "claimed": 2, "pending": 1},
            "inflight": {"running": 2, "pending_close": 6},
        }

    def test_replace_duplicates_and_preserve_history(self):
        old = row("2026-11-03")
        rows = row("2026-11-04 08:01") + old + row("2026-11-04", next_step="重复行")
        result = dashboard.upsert_today_checkin(rows, self.snapshot)
        self.assertEqual(result.count("2026-11-04"), 1)
        self.assertIn(old, result)
        self.assertIn("自动刷新(09:15)：wiki 200篇/深篇150", result)
        self.assertIn("任务账本8done/2claimed/1pending；在途2跑中+6待收口", result)
        self.assertIn("人工下一步 &amp; <b>保留</b>", result)
        self.assertEqual(dashboard.upsert_today_checkin(result, self.snapshot), result)

    def test_new_day_inherits_manual_next_step(self):
        result = dashboard.upsert_today_checkin(row("2026-11-03"), self.snapshot)
        self.assertLess(result.index("2026-11-04"), result.index("2026-11-03"))
        self.assertEqual(result.count("人工下一步 &amp; <b>保留</b>"), 2)

    def test_empty_history_has_empty_next_step(self):
        result = dashboard.upsert_today_checkin("", self.snapshot)
        self.assertIn('<td contenteditable="true"></td>', result)

    def test_keep_thirty_rows_and_drop_oldest_not_last(self):
        dates = [datetime.date(2026, 11, 3) - datetime.timedelta(days=i) for i in range(35)]
        # 老行顺序故意不是日期倒序。
        old = ''.join(row(str(d)) for d in reversed(dates))
        result = dashboard.upsert_today_checkin(old, self.snapshot)
        self.assertEqual(len(re.findall(r'<tr\b', result)), 30)
        self.assertNotIn(str(dates[-1]), result)
        self.assertIn(str(dates[28]), result)
        self.assertIn(str(dates[0]), result)


class FusionTests(unittest.TestCase):
    def test_real_wbs_does_not_promote_landed_article_to_done(self):
        result = dashboard.scan_fusion(dashboard.WBS.read_text(encoding="utf-8"))
        self.assertEqual(result["l4"], {"done": 0, "running": 1, "todo": 7,
                                        "total": 8, "pct": 0.0})
        self.assertEqual(result["b32"]["done"], 6)
        self.assertEqual(result["b32"]["status"], "✅")

    def test_explicit_completion_requires_evidence(self):
        source = '''### 批1 · L4-RAG工程：八主题收敛 🔄执行中
- **B1.01 主题1 ✅已完成**
  - 实测：新篇与引用验收通过，旧篇替换完成。
- **B1.02 主题2 ✅已完成**
  - 尚未取得收口证据。
### 批1 · L4 剩余主题 ⏳待办
- **B1.03 主题3**：待办。
### 其它
- **B1.04 主题4 ✅已完成**
  - 实测：无关节不能计数。
'''
        self.assertEqual(dashboard.scan_fusion(source)["l4"], {
            "done": 1, "running": 1, "todo": 6, "total": 8, "pct": 12.5})

    def test_missing_source_is_not_completed(self):
        result = dashboard.scan_fusion("")
        self.assertEqual(result["l4"]["done"], 0)
        self.assertEqual(result["b32"]["status"], "未标注")
        self.assertEqual(result["b32"]["done"], 0)


class RefreshBoundaryTests(unittest.TestCase):
    def test_inflight_probe_suppresses_its_refresh_callback(self):
        with patch.object(dashboard.subprocess, "run") as run:
            run.return_value.stdout = "🔄 跑中(2)\n✅ 待收口(6)"
            run.return_value.stderr = ""
            result = dashboard.scan_inflight()
        self.assertEqual(result["pending_close"], 6)
        self.assertEqual(run.call_args.kwargs.get("env", {}).get("DASHBOARD_RENDER_ACTIVE"), "1")


class OkrResponsibilityTests(unittest.TestCase):
    def test_all_krs_have_confidence_owner_and_explicit_support(self):
        result = dashboard.scan_okr()
        krs = [k for o in result["os"] for k in o["krs"]]
        self.assertEqual(len(krs), 9)
        for k in krs:
            with self.subTest(kr=k["id"]):
                self.assertRegex(k.get("confidence", ""), r"^[高中低][：:]")
                self.assertIn(k.get("owner"), ("Hermes", "DSH", "Codex", "OC"))
                self.assertTrue(k["inits"])
        self.assertFalse(result["link_basis_tags"].get("prefix"))
        self.assertFalse(result["issues"])

    def test_confidence_and_owner_render_beside_status(self):
        result = dashboard.scan_okr()
        page = dashboard.render_okr_card(result)
        self.assertEqual(page.count('class="okr-confidence"'), 9)
        self.assertEqual(page.count('class="okr-owner"'), 9)
        self.assertIn("信心：低", page)
        self.assertIn("季度达成判断", page)
        self.assertNotIn("未标支撑举措", page)
        self.assertNotIn("条举措未标 KR", page)
        self.assertIn("failed_needs_redispatch", page)


class OkrGuardrailTests(unittest.TestCase):
    def test_guardrails_are_separate_from_nine_scored_krs(self):
        result = dashboard.scan_okr()
        guards = result.get("guardrails", [])
        self.assertEqual(len(guards), 2)
        self.assertEqual(sum(o["total"] for o in result["os"]), 9)
        self.assertEqual(sum(o["done"] for o in result["os"]), 0)
        self.assertIn("100%", str(guards))
        self.assertIn("0 次/季度", str(guards))
        self.assertIn("强制复盘", str(guards))
        krs = {k["id"]: k for o in result["os"] for k in o["krs"]}
        for kid in ("KR2.2", "KR2.3"):
            self.assertEqual(krs[kid]["type"], "改进曲线")
            self.assertIn("→", krs[kid]["target"])
        self.assertIn("40 待收口", krs["KR2.2"]["current"])
        self.assertIn("5 次", krs["KR2.3"]["current"])
        page = dashboard.render_okr_card(result)
        self.assertIn('id="okr-guardrails"', page)
        self.assertIn("质量红线/护栏", page)
        self.assertIn("不计入 KR 达标数", page)
        self.assertIn("触发即拦截", page)
        self.assertIn("不是周基线", page)


class OkrScheduleTests(unittest.TestCase):
    def test_engineering_denominator_never_uses_whole_library_score(self):
        result = dashboard.scan_okr()
        k = next(k for o in result["os"] for k in o["krs"] if k["id"] == "KR3.3")
        self.assertIn("39", k["target"])
        self.assertIn("39", k["current"])
        self.assertIn("未测", k["current"])
        self.assertNotIn("124", k["current"])
        self.assertEqual(k["state"], "untested")
        notes = str(result.get("measurement_notes", []))
        self.assertIn("89/124", notes)
        self.assertIn("不参与", notes)

    def test_four_milestones_have_date_deliverable_dependency_and_checkpoint(self):
        result = dashboard.scan_okr()
        milestones = result.get("milestones", [])
        self.assertEqual(len(milestones), 4)
        for item in milestones:
            self.assertRegex(item["日期"], r"2026-11-\d\d")
            for field in ("Owner", "依赖", "交付物", "检查点", "状态"):
                self.assertTrue(item[field])
        page = dashboard.render_okr_card(result)
        self.assertIn('id="okr-milestones"', page)
        self.assertIn("测基线 → 定 KR 目标值 → 算进度 → 排计划", page)
        self.assertIn("2026-11-09", page)
        self.assertIn('id="okr-measurement-notes"', page)

    def test_register_parser_does_not_swallow_adjacent_sections(self):
        source = '''## 质量红线/护栏
| 护栏 | 约束 |
|---|---|
| G1 | A\\|B & <value> |

## 关键路径/里程碑
| 里程碑 | 日期 |
|---|---|
| M1 | 2026-11-09 |
'''
        result = dashboard._parse_okr_register(source, "质量红线/护栏")
        self.assertEqual(result, [{"护栏": "G1", "约束": "A|B & <value>"}])
        page = dashboard.render_okr_register(result, "test", "标题", "说明")
        self.assertIn("A|B &amp; &lt;value&gt;", page)
        self.assertNotIn("2026-11-09", page)
        self.assertEqual(dashboard._parse_okr_register(source, "不存在"), [])
        self.assertIn("真源未提供", dashboard.render_okr_register([], "test", "标题", "说明"))


if __name__ == "__main__":
    unittest.main()
