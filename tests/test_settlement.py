"""结算/复算引擎的业务规则测试（标准库 unittest）。

这些用例把 README 运行契约与业务委托中的关键边界固定下来，
保证结果可重算、改动可被发现。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settlement  # noqa: E402


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = settlement.build_result(ROOT / "data" / "records.json")
        cls.engine = settlement.SettlementEngine(settlement.load_records())

    # ---------- 输入规模 ---------- #
    def test_counts(self):
        c = self.result["counts"]
        self.assertEqual(c["promises"], 180)
        self.assertEqual(c["evidence"], 168)
        self.assertEqual(c["transfers"], 12)
        self.assertEqual(c["claims"], 5)
        self.assertEqual(c["ledger_lines"], 6)

    # ---------- 分段状态 ---------- #
    def test_segment_status_counts(self):
        g = self.result["aggregation"]["grand"]
        self.assertEqual((g["done"], g["disputed"], g["failed"],
                          g["missing"], g["exempt"]),
                         (142, 2, 6, 30, 0))
        self.assertEqual(g["segments"], 180)

    def test_all_cancellations_ineffective_in_raw_evidence(self):
        reg = self.result["cancellation_register"]
        self.assertEqual(len(reg), 24)
        self.assertTrue(all(not c["exempt"] for c in reg))
        # 标准合同下发起方恰好提前 1 小时（至少1 -> 及时），另一方仅 0.5
        std = [c for c in reg if c["policy"] == "标准"]
        self.assertEqual(len(std), 8)
        self.assertTrue(all(c["own_timely"] for c in std))
        self.assertTrue(all(not c["other_timely"] for c in std))
        # 修订合同 notice=2，发起方 1 小时也不及时
        rev = [c for c in reg if c["policy"] == "修订"]
        self.assertTrue(all(not c["own_timely"] for c in rev))

    def test_six_cancel_only_segments_are_failed(self):
        failed = {s["key"] for s in self.result["segments"]
                  if s["status"] == "failed"}
        self.assertEqual(failed, {
            "8-10/0", "8-24/1", "9-10/0", "9-24/1",
            "10-10/0", "10-24/1",
        })

    # ---------- 承接/责任 ---------- #
    def test_raw_transfers_none_effective_under_strict_boundary(self):
        reg = self.result["transfer_register"]
        self.assertEqual(len(reg), 12)
        self.assertFalse(any(t["effective"] for t in reg))
        approved_boundary = [t for t in reg if t["approved"]]
        self.assertEqual(len(approved_boundary), 6)
        self.assertTrue(all(not t["before_start"] for t in approved_boundary))

    def test_adjudicated_successors(self):
        resp_911 = self.engine.responsible(("9-11", 1))
        resp_881 = self.engine.responsible(("8-8", 1))
        self.assertEqual(resp_911["vendor"], "丙")
        self.assertEqual(resp_911["basis"], "adjudicated_successor")
        self.assertEqual(resp_911["ref"], "X2")
        self.assertEqual(resp_881["vendor"], "乙")
        self.assertEqual(resp_881["ref"], "X5")
        # 原承办方字段保持不变
        self.assertEqual(resp_911["original_vendor"], "甲")
        self.assertEqual(resp_881["original_vendor"], "甲")

    # ---------- 汇总不复制 ---------- #
    def test_dimension_totals_conserve(self):
        a = self.result["aggregation"]
        total_seg = a["grand"]["segments"]
        self.assertEqual(total_seg, 180)
        self.assertEqual(sum(v["segments"] for v in a["by_month"].values()), 180)
        self.assertEqual(sum(v["segments"] for v in a["by_site"].values()), 180)
        self.assertEqual(
            sum(v["segments"] for v in a["by_responsible"].values()), 180)
        fee = a["grand"]["segment_fee_payable_now"]
        self.assertEqual(
            sum(v["segment_fee_payable_now"] for v in a["by_month"].values()), fee)
        self.assertEqual(
            sum(v["segment_fee_payable_now"]
                for v in a["by_responsible"].values()), fee)
        self.assertEqual(fee, 20340)

    # ---------- 联办附加款 ---------- #
    def test_bundle_bonus_counts_and_amounts(self):
        bb = self.result["bundle_bonuses"]
        self.assertEqual(len(bb), 66)
        states = {}
        for b in bb:
            states[b["state"]] = states.get(b["state"], 0) + 1
        self.assertEqual(states, {"qualifies_now": 42,
                                  "pending_evidence": 21, "blocked": 3})
        now = self.result["bundle_bonus_payable_now_by_organizer"]
        self.assertEqual(now, {"甲": 1250, "乙": 1000, "丙": 1250})
        self.assertEqual(sum(now.values()), 3500)
        self.assertEqual(self.result["bundle_bonus_pending_max_exposure"], 1750)

    # ---------- 台账核对 ---------- #
    def test_ledger_lines(self):
        by = {L["line"]: L for L in self.result["ledger_reconciliation"]}
        self.assertEqual((by["L1"]["undisputed_due_payee"],
                          by["L1"]["undisputed_due_amount"]), ("丙", 130))
        self.assertEqual((by["L2"]["undisputed_due_payee"],
                          by["L2"]["undisputed_due_amount"]), ("丙", 150))
        self.assertEqual((by["L3"]["ledger_payee"], by["L3"]["ledger_amount"]),
                         ("甲", 0))
        self.assertEqual((by["L3"]["undisputed_due_payee"],
                          by["L3"]["undisputed_due_amount"]), ("丙", 150))
        self.assertTrue(by["L4"]["hold_disputed"])
        self.assertIsNone(by["L4"]["undisputed_due_payee"])
        # L5 错付甲、无确定补付对象
        self.assertIsNone(by["L5"]["undisputed_due_payee"])
        self.assertEqual((by["L6"]["undisputed_due_payee"],
                          by["L6"]["undisputed_due_amount"]), ("乙", 130))

    # ---------- 冲回与释放 ---------- #
    def test_clawbacks(self):
        cs = {c["line"]: c for c in self.result["corrections"]["clawbacks"]}
        self.assertEqual((cs["L5"]["authorized_clawback"],
                          cs["L5"]["actually_returned"],
                          cs["L5"]["outstanding"]), (150, 35, 115))
        self.assertEqual((cs["L6"]["authorized_clawback"],
                          cs["L6"]["actually_returned"],
                          cs["L6"]["outstanding"]), (130, 75, 55))
        self.assertFalse(cs["L5"]["outstanding_is_spendable"])

    def test_release_schedule(self):
        sch = {p["period_month"]: p
               for p in self.result["corrections"]["schedule"]}
        p9, p10, p11 = sch[9], sch[10], sch[11]
        self.assertEqual(p9["available_capacity"], 255)
        self.assertEqual(p9["released_total"], 255)
        rel9 = {r["auth"]: r for r in p9["releases"]}
        self.assertEqual(rel9["X2-seg"]["released"], 150)
        self.assertEqual(rel9["X2-bonus"]["released"], 95)
        self.assertEqual(rel9["X5"]["released"], 10)
        self.assertEqual(rel9["X5"]["remaining_unconsumed_after"], 120)

        self.assertEqual(p10["available_capacity"], 250)
        rel10 = {r["auth"]: r for r in p10["releases"]}
        self.assertEqual(rel10["X5"]["released"], 120)
        self.assertEqual(rel10["X5"]["remaining_unconsumed_after"], 0)
        self.assertEqual(p10["released_total"], 120)

        self.assertEqual(p11["available_capacity"], 450)
        self.assertEqual(p11["released_total"], 0)
        self.assertEqual(
            self.result["corrections"]["unreleased_after_all_periods"], [])

    # ---------- 季度线 ---------- #
    def test_quarter_all_clear_with_margins(self):
        v = self.result["quarter"]["vendors"]
        self.assertAlmostEqual(v["甲"]["possible_lower"], 0.793839, places=5)
        self.assertAlmostEqual(v["乙"]["possible_lower"], 0.786543, places=5)
        self.assertAlmostEqual(v["丙"]["possible_lower"], 0.821918, places=5)
        for vendor in ("甲", "乙", "丙"):
            q = v[vendor]
            self.assertGreaterEqual(q["possible_lower"], q["threshold"])
            self.assertFalse(q["straddles_threshold"])
            self.assertEqual(q["determined_band"], "达线")
            self.assertEqual(q["next_period_weight_cap"], 24)

    # ---------- 调证 ---------- #
    def test_evidence_plan_excludes_settled_v2_and_leads_v3(self):
        plan = self.result["evidence_plan"]
        self.assertEqual(plan["request_count_reading"]["exclude"], ["V2"])
        self.assertEqual(plan["request_count_reading"]["recommended_order"][0], "V3")
        v2 = next(o for o in plan["options"] if o["id"] == "V2")
        self.assertEqual(v2["resolves"], [])
        self.assertEqual(v2["money_at_stake_now"], 0)

    # ---------- 新来件在项目内重新关联后可重算 ---------- #
    def test_new_evidence_reassociates_and_changes_result(self):
        data = settlement.load_records(ROOT / "data" / "records.json")
        before = settlement.SettlementEngine(data).build()
        self.assertEqual(
            next(s for s in before["segments"] if s["key"] == "9-4/1")["status"],
            "missing")
        # 新来件：丙对 9-4/1 的参与确认，关联到同一 (appointment, part)
        data["evidence"].append({
            "ref": "9-4/1-NEW", "appointment": "9-4", "part": 1,
            "reporter": "丙", "hour": 13, "kind": "参与确认",
            "received_day": 40, "other_party_hour": None,
        })
        after = settlement.SettlementEngine(data).build()
        seg = next(s for s in after["segments"] if s["key"] == "9-4/1")
        self.assertEqual(seg["status"], "done")
        self.assertEqual(seg["responsible_vendor"], "丙")
        self.assertEqual(seg["currently_payable"], 150)
        # 只新增这一段的给付，总额由 20340 -> 20490
        self.assertEqual(
            after["aggregation"]["grand"]["segment_fee_payable_now"], 20490)
        # 查询/重算不写回源文件，也不虚构 approval 或 returned
        self.assertIsNone(next(
            c for c in data["claims"] if c["ref"] == "X3")["approved_day"])


if __name__ == "__main__":
    unittest.main()
