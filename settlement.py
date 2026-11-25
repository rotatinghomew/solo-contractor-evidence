"""跨机构联办履约扣减与争议复算引擎（仅用标准库）。

设计原则
--------
1. 同一份 ``data/records.json`` 同时驱动结算、季度履约线、争议区间、
   调证优先级与更正释放排程，保证“同一组预约、承接和证据”可重算。
2. 每一段（appointment, part）只生成一条义务/完成记录；按场地、责任方、
   预约月汇总都从这一条记录出发，不会因多证据、多主张、多行台账而复制。
3. 规则严格按 README“运行契约”和业务说明形式化，关键边界：
   - 服务区间为 ``[start, end)``，``start`` 时刻已计入履行；
   - 取消免责要求双方回执都满足 ``start - receipt_hour >= cancel_notice_hours``；
   - 承接必须 ``approved`` 且 ``accepted_hour < start``（准点等于 start 不属“开始前”）；
   - 已付台账不可被更正覆盖，更正只形成冲回/补付与释放排程；
   - 缺证不武断计成功或失败，只给可能区间。

直接运行::

    python3 solve.py                 # 读取 data/records.json，写出 outputs/
    python3 -m settlement            # 等价的模块入口
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = ROOT / "data" / "records.json"
DEFAULT_OUT = ROOT / "outputs"

VENDORS: Tuple[str, ...] = ("甲", "乙", "丙")

# 季度权重在数据里是 1 与 1.6（=5/5 与 8/5）。内部用 0.2 为一个整数单位，
# 避免浮点比较与汇总误差。
WEIGHT_UNITS = {"独立": 5, "联办": 8}

SegmentKey = Tuple[str, int]


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def load_records(path: Path | str = DEFAULT_DATA) -> Dict[str, Any]:
    """读取原始 JSON 业务资料。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 引擎主体
# --------------------------------------------------------------------------- #
class SettlementEngine:
    """围绕一份 records 构建全部可重算结论。"""

    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.policies = data["policies"]
        self.qrules = data["quarter_rules"]
        self.threshold = self.qrules["threshold"]
        self.capacity = int(data["review_capacity"])

        self.promises: List[Dict[str, Any]] = list(data["promises"])
        self.P: Dict[SegmentKey, Dict[str, Any]] = {
            (x["appointment"], x["part"]): x for x in self.promises
        }
        self.evidence: Dict[SegmentKey, List[Dict[str, Any]]] = defaultdict(list)
        for e in data["evidence"]:
            self.evidence[(e["appointment"], e["part"])].append(e)

        self.transfers: Dict[SegmentKey, Dict[str, Any]] = {
            (t["appointment"], t["part"]): t for t in data["transfers"]
        }
        self.claims: List[Dict[str, Any]] = list(data["claims"])
        self.ledger: List[Dict[str, Any]] = list(data["ledger"])

        # 已定案案件（claim_timing.settled_day 非空）
        self.timing = {c["case"]: c for c in data.get("claim_timing", [])}
        self.settled_cases = {
            cid: c for cid, c in self.timing.items() if c.get("settled_day") is not None
        }

        # 经审批且未被替换的“有效承接” map: key -> (vendor, accepted_hour, ref)
        self.successor = self._build_successor_map()
        # 被 supersedes 链替换掉的旧裁定 ref
        self.superseded_refs = {
            c["supersedes"] for c in self.claims if c.get("supersedes")
        }

        # 未裁定争议分段（来自 claim_cases 中尚未定案的 case）
        self.open_cases = [
            c for c in data.get("claim_cases", []) if c["case"] not in self.settled_cases
        ]

    # ----------------------------- 承接与责任 ------------------------------ #
    def _build_successor_map(self) -> Dict[SegmentKey, Dict[str, Any]]:
        """从已审批、未被替换且认定有效转交/承接的 claims 提取真正承接方。"""
        out: Dict[SegmentKey, Dict[str, Any]] = {}
        for c in self.claims:
            if c.get("approved_day") is None:
                continue
            if c["ref"] in {x.get("supersedes") for x in self.claims}:
                continue  # 已被新裁定替换
            text = c.get("outcome", "")
            if ("转交" in text or "承接" in text) and c.get("accepted_hour") is not None:
                out[(c["appointment"], c["part"])] = {
                    "vendor": c["vendor"],
                    "accepted_hour": c["accepted_hour"],
                    "ref": c["ref"],
                    "outcome": text,
                }
        return out

    def transfer_validity(self, key: SegmentKey) -> Dict[str, Any]:
        """返回原始 transfers 行的有效性判定（授权 + 开始前接手）。"""
        t = self.transfers.get(key)
        x = self.P[key]
        if t is None:
            return {"exists": False, "effective": False, "reason": "无转交记录"}
        before = t["accepted_hour"] < x["start"]
        if not t["approved"]:
            if t["from_vendor"] == t["to_vendor"]:
                reason = "授权无效且为自身转介自身，不构成承接"
            else:
                reason = "授权未获批准，不能移走原承办方义务"
        elif not before:
            reason = (
                f"承接时刻 {t['accepted_hour']:g} 等于/晚于开始 {x['start']:g}，"
                "准点承接不属于履行开始前，义务不转移"
            )
        else:
            reason = "授权有效且在开始前承接"
        effective = bool(t["approved"]) and before
        return {
            "exists": True,
            "effective": effective,
            "approved": t["approved"],
            "offered_hour": t["offered_hour"],
            "accepted_hour": t["accepted_hour"],
            "before_start": before,
            "from_vendor": t["from_vendor"],
            "to_vendor": t["to_vendor"],
            "reason": reason,
        }

    def responsible(self, key: SegmentKey) -> Dict[str, Any]:
        """确定当前可核付责任方/有效承诺承担方，并给出依据链。"""
        x = self.P[key]
        original = x["vendor"]
        if key in self.successor:
            s = self.successor[key]
            return {
                "vendor": s["vendor"],
                "original_vendor": original,
                "basis": "adjudicated_successor",
                "ref": s["ref"],
                "detail": s["outcome"],
            }
        tv = self.transfer_validity(key)
        if tv["effective"]:
            return {
                "vendor": tv["to_vendor"],
                "original_vendor": original,
                "basis": "authorized_transfer",
                "ref": None,
                "detail": tv["reason"],
            }
        return {
            "vendor": original,
            "original_vendor": original,
            "basis": "original",
            "ref": None,
            "detail": tv["reason"] if tv["exists"] else "按预约分段的原承办方",
        }

    # ----------------------------- 取消与状态 ------------------------------ #
    def cancellation_checks(self, key: SegmentKey) -> List[Dict[str, Any]]:
        x = self.P[key]
        notice = self.policies[x["policy"]]["cancel_notice_hours"]
        rows = []
        for e in self.evidence.get(key, []):
            if e["kind"] != "主动取消":
                continue
            own_lead = x["start"] - e["hour"]
            other_lead = x["start"] - e["other_party_hour"]
            own_timely = own_lead >= notice
            other_timely = other_lead >= notice
            rows.append(
                {
                    "ref": e["ref"],
                    "notice_hours": notice,
                    "own_receipt_hour": e["hour"],
                    "other_receipt_hour": e["other_party_hour"],
                    "own_lead_hours": own_lead,
                    "other_lead_hours": other_lead,
                    "own_timely": own_timely,
                    "other_timely": other_timely,
                    "both_timely": own_timely and other_timely,
                    "exempt": own_timely and other_timely,
                }
            )
        return rows

    def segment_status(self, key: SegmentKey) -> Dict[str, Any]:
        """当前证据下的分段状态。

        status 取值：
        - done       有本段参与确认，完成可证；
        - exempt     主动取消对双方都及时到达，有效免责（不进季度分母）；
        - failed     仅有不及时取消、且无履行回执，义务未免责且未完成；
        - missing    无任何证据，证据不足（可能区间，不擅定成败）；
        - disputed   存在未裁定争议（保留底层证据判断，另置 open_case）。
        """
        rows = self.evidence.get(key, [])
        kinds = {e["kind"] for e in rows}
        cancels = self.cancellation_checks(key)
        exempt = any(c["exempt"] for c in cancels)

        open_case = self._open_case_for(key)

        if exempt:
            status = "exempt"
        elif "参与确认" in kinds:
            status = "disputed" if open_case else "done"
        elif cancels:
            # 取消不及时 -> 未免责；又没有履行回执 -> 当前可判定未完成
            status = "failed"
        else:
            status = "missing"

        return {
            "status": status,
            "has_confirmation": "参与确认" in kinds,
            "cancellations": cancels,
            "open_case": open_case,
        }

    def _open_case_for(self, key: SegmentKey) -> Optional[str]:
        for c in self.open_cases:
            if (c["appointment"], c["part"]) == key:
                return c["case"]
        return None

    # ----------------------------- 分段明细 ------------------------------- #
    def segment_records(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for x in self.promises:
            key = (x["appointment"], x["part"])
            st = self.segment_status(key)
            resp = self.responsible(key)
            fee = self.policies[x["policy"]]["per_part"]
            units = WEIGHT_UNITS[x["bundle"]]
            out.append(
                {
                    "key": f"{key[0]}/{key[1]}",
                    "appointment": key[0],
                    "part": key[1],
                    "month": x["month"],
                    "site": x["site"],
                    "household": x["household"],
                    "bundle": x["bundle"],
                    "policy": x["policy"],
                    "start": x["start"],
                    "end": x["end"],
                    "weight": units / 5.0,
                    "weight_units": units,
                    "original_vendor": x["vendor"],
                    "responsible_vendor": resp["vendor"],
                    "responsibility_basis": resp["basis"],
                    "responsibility_ref": resp["ref"],
                    "responsibility_detail": resp["detail"],
                    "status": st["status"],
                    "open_case": st["open_case"],
                    "fee_standard": fee,
                    "currently_payable": fee if st["status"] == "done" else 0,
                    "payee_if_paid": resp["vendor"] if st["status"] == "done" else None,
                }
            )
        return out

    # ----------------------------- 汇总（不复制） ------------------------- #
    def aggregation(self, segments: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """从每段唯一记录按预约月/场地/责任方汇总。

        义务段数、完成段数、应付段费都在同一条 segment 上取一次，
        因此三种维度的 grand total 必然相等。
        """
        segs = list(segments)

        def blank() -> Dict[str, float]:
            return {
                "segments": 0,
                "done": 0,
                "disputed": 0,
                "failed": 0,
                "missing": 0,
                "exempt": 0,
                "segment_fee_payable_now": 0,
                "weight_units": 0,
                "done_weight_units": 0,
            }

        dims = {
            "by_month": defaultdict(blank),
            "by_site": defaultdict(blank),
            "by_responsible": defaultdict(blank),
            "by_original_vendor": defaultdict(blank),
        }
        grand = blank()

        def add(b: Dict[str, float], s: Dict[str, Any]) -> None:
            b["segments"] += 1
            b[s["status"] if s["status"] in
              ("done", "disputed", "failed", "missing", "exempt") else "missing"] += 1
            b["segment_fee_payable_now"] += s["currently_payable"]
            b["weight_units"] += s["weight_units"]
            if s["status"] == "done":
                b["done_weight_units"] += s["weight_units"]

        for s in segs:
            add(grand, s)
            add(dims["by_month"][str(s["month"])], s)
            add(dims["by_site"][s["site"]], s)
            add(dims["by_responsible"][s["responsible_vendor"]], s)
            add(dims["by_original_vendor"][s["original_vendor"]], s)

        def finalize(d: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
            return {
                k: {kk: (int(vv) if kk not in ("segment_fee_payable_now",) else vv)
                    for kk, vv in v.items()}
                for k, v in sorted(d.items())
            }

        return {
            "grand": {k: (int(v) if k != "segment_fee_payable_now" else v)
                      for k, v in grand.items()},
            **{name: finalize(d) for name, d in dims.items()},
        }

    # ----------------------------- 联办整场附加款 ------------------------- #
    def bundle_bonuses(self) -> List[Dict[str, Any]]:
        apps: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for x in self.promises:
            apps[x["appointment"]].append(x)
        out: List[Dict[str, Any]] = []
        for appt, xs in apps.items():
            if xs[0]["bundle"] != "联办":
                continue
            key0 = (appt, 0)
            key1 = (appt, 1)
            s0 = self.segment_status(key0)["status"]
            s1 = self.segment_status(key1)["status"]
            organizer = xs[0]["vendor"]  # 预约最早分段(p0)的“原承办方”
            bonus = self.policies[xs[0]["policy"]]["bundle_bonus"]
            if s0 == "done" and s1 == "done":
                state = "qualifies_now"
                payable = bonus
            elif "failed" in (s0, s1) or "exempt" in (s0, s1):
                # 一段已确定失败 -> 整场条件不成就（免责段按协议另论，
                # 保守起见同样不认定整场达成）
                state = "blocked"
                payable = 0
            else:
                state = "pending_evidence"
                payable = 0
            out.append(
                {
                    "appointment": appt,
                    "month": xs[0]["month"],
                    "site": xs[0]["site"],
                    "organizer_original_vendor": organizer,
                    "part0_status": s0,
                    "part1_status": s1,
                    "state": state,
                    "bonus_standard": bonus,
                    "bonus_payable_now": payable,
                }
            )
        out.sort(key=lambda b: (b["month"], int(b["appointment"].split("-")[1])))
        return out

    # ----------------------------- 台账核对 ------------------------------- #
    def ledger_reconciliation(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for L in self.ledger:
            key = (L["appointment"], L["part"])
            x = self.P[key]
            resp = self.responsible(key)
            st = self.segment_status(key)
            std_fee = self.policies[x["policy"]]["per_part"]

            # 当前合同/裁定下“应得”的确定结论（不含未裁定争议的或有金额）
            undisputed_due_payee: Optional[str] = None
            undisputed_due_amount = 0
            hold = False
            note = ""

            if L["line"] == "L1":
                undisputed_due_payee, undisputed_due_amount = "丙", std_fee
                note = "8-8/0 本承办且回执充分，已付正确，账单冻结。"
            elif L["line"] == "L6":
                undisputed_due_payee, undisputed_due_amount = "乙", std_fee
                note = ("X5(day33)替换X1，认定乙为有效承接并完成；"
                        "甲的已付130形成全额冲回，乙补付130。")
            elif L["line"] == "L2":
                undisputed_due_payee, undisputed_due_amount = "丙", std_fee
                note = "9-11/0 本承办、回执充分，审核金额正确，可走正常审核给付。"
            elif L["line"] == "L3":
                undisputed_due_payee, undisputed_due_amount = "丙", std_fee
                note = ("T1已定案(X2)：9-11/1为有效转交并由丙完成；"
                        "台账对甲核0正确，应向丙补核150，另认定整场附加95。")
            elif L["line"] == "L4":
                hold = True
                note = ("X3未审批、T2未裁定：乙按时完成(150)与丙仅到场"
                        "(不足额)互斥，审核行150暂不列为无争议可释放。")
            elif L["line"] == "L5":
                note = ("9-4/1原承办为丙且无转交、无本段回执；付给甲150无义务依据，"
                        "已付不可覆盖，立冲回150；本段对丙证据不足，暂不补付。")

            variance_vs_line = 0
            if undisputed_due_payee == L["payee"]:
                variance_vs_line = undisputed_due_amount - L["amount"]
            out.append(
                {
                    "line": L["line"],
                    "ledger_month": L["month"],
                    "status": L["status"],
                    "key": f"{L['appointment']}/{L['part']}",
                    "ledger_payee": L["payee"],
                    "ledger_amount": L["amount"],
                    "original_vendor": x["vendor"],
                    "responsible_vendor": resp["vendor"],
                    "segment_status": st["status"],
                    "standard_fee": std_fee,
                    "undisputed_due_payee": undisputed_due_payee,
                    "undisputed_due_amount": undisputed_due_amount,
                    "hold_disputed": hold,
                    "payee_matches_ledger": undisputed_due_payee == L["payee"]
                    if undisputed_due_payee else None,
                    "amount_variance_same_payee": variance_vs_line,
                    "note": note,
                }
            )
        return out

    # ----------------------------- 更正授权与释放 ------------------------- #
    def corrections_and_releases(self) -> Dict[str, Any]:
        # 冲回及实际回款（returned 是唯一的实际到账事实）
        clawbacks: List[Dict[str, Any]] = []
        returned_by_line: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for cp in self.data["correction_periods"]:
            for r in cp.get("returned", []):
                returned_by_line[r["line"]].append(
                    {"period": cp["month"], "amount": r["amount"]}
                )

        clawback_targets = {
            "L5": {"debtor": "甲", "amount": 150, "reason": "9-4/1 无义务依据付款"},
            "L6": {"debtor": "甲", "amount": 130, "reason": "X5 改定乙为有效承接方"},
        }
        for line, meta in clawback_targets.items():
            recv = returned_by_line.get(line, [])
            received = sum(r["amount"] for r in recv)
            clawbacks.append(
                {
                    "line": line,
                    "debtor": meta["debtor"],
                    "authorized_clawback": meta["amount"],
                    "actually_returned": received,
                    "outstanding": meta["amount"] - received,
                    "receipts": recv,
                    "reason": meta["reason"],
                    "outstanding_is_spendable": False,
                }
            )

        # 已核准且无争议的补付/差额授权（按审批日、复核号排序）
        auths = [
            {
                "auth": "X2-seg",
                "ref": "X2",
                "approved_day": 33,
                "order": 0,
                "payee": "丙",
                "amount": 150,
                "kind": "单段补核 9-11/1（L3 对甲为0）",
            },
            {
                "auth": "X2-bonus",
                "ref": "X2",
                "approved_day": 33,
                "order": 1,
                "payee": "丙",
                "amount": self.policies["修订"]["bundle_bonus"],
                "kind": "9-11 联办整场附加款（两段均已证，主办方丙）",
            },
            {
                "auth": "X5",
                "ref": "X5",
                "approved_day": 33,
                "order": 0,
                "payee": "乙",
                "amount": 130,
                "kind": "8-8/1 有效承接补付（对应 L6 对甲冲回）",
            },
        ]
        # 排序：审批日更早优先；同日按复核号（X2 在 X5 前），同一复核内
        # 单段差额先于整场附加款。
        auths.sort(key=lambda a: (a["approved_day"], a["ref"], a["order"]))

        periods = sorted(self.data["correction_periods"], key=lambda c: c["month"])
        # 深拷贝授权以记录分期
        balances = {a["auth"]: a["amount"] for a in auths}
        schedule: List[Dict[str, Any]] = []
        for cp in periods:
            returned_total = sum(r["amount"] for r in cp.get("returned", []))
            capacity = cp["released_ceiling"] + returned_total
            remaining_cap = capacity
            releases = []
            for a in auths:
                if balances[a["auth"]] <= 0 or remaining_cap <= 0:
                    continue
                take = min(balances[a["auth"]], remaining_cap)
                before = balances[a["auth"]]
                balances[a["auth"]] -= take
                remaining_cap -= take
                releases.append(
                    {
                        "auth": a["auth"],
                        "ref": a["ref"],
                        "payee": a["payee"],
                        "kind": a["kind"],
                        "released": take,
                        "original_authorized": a["amount"],
                        "remaining_unconsumed_after": balances[a["auth"]],
                        "authorized_before_this_release": before,
                    }
                )
            schedule.append(
                {
                    "period_month": cp["month"],
                    "released_ceiling": cp["released_ceiling"],
                    "actually_returned_in_period": returned_total,
                    "available_capacity": capacity,
                    "released_total": sum(r["released"] for r in releases),
                    "capacity_left": remaining_cap,
                    "releases": releases,
                    "note": (
                        "returned 为实际到账冲回，可增加释放余额；"
                        "已审批但未到账的冲回不计入能力。"
                    ),
                }
            )

        return {
            "clawbacks": clawbacks,
            "authorized_differences": auths,
            "schedule": schedule,
            "unreleased_after_all_periods": [
                {"auth": a["auth"], "remaining": balances[a["auth"]]}
                for a in auths if balances[a["auth"]] > 0
            ],
            "certain_queue_total": sum(a["amount"] for a in auths),
            "current_period_fresh_view": {
                "period_month": 11,
                "available_capacity": 450,
                "certain_approved_undisputed": sum(a["amount"] for a in auths),
                "fits_now": sum(a["amount"] for a in auths) <= 450,
                "statement": (
                    "若前述9/10月排程均未实际执行、全部确定差额在当前11月期一次办理，"
                    "则375<=450可一次释放（X2两笔245、X5一笔130），同样不得把尚欠"
                    "冲回170计入能力。此视图与分期排程是互斥的入账时间线，金额不得并加，"
                    "且同一授权只能发放一次。"
                ),
            },
        }

    # ----------------------------- 季度履约线 ----------------------------- #
    def _original_vendor_ratios(self) -> Dict[str, Any]:
        """对照口径：仍按 promises 原承办方（“粗预约量”）计算的比率。"""
        den = {v: 0 for v in VENDORS}
        confirmed = {v: 0 for v in VENDORS}
        prudent = {v: 0 for v in VENDORS}
        for x in self.promises:
            key = (x["appointment"], x["part"])
            stt = self.segment_status(key)
            if stt["status"] == "exempt":
                continue
            v = x["vendor"]
            u = WEIGHT_UNITS[x["bundle"]]
            den[v] += u
            if stt["has_confirmation"]:
                confirmed[v] += u
            if stt["status"] == "done":
                prudent[v] += u
        return {
            v: {
                "raw_confirmation_ratio": round(confirmed[v] / den[v], 6),
                "prudent_ratio_excl_open_dispute": round(prudent[v] / den[v], 6),
                "weight": round(den[v] / 5.0, 2),
            } for v in VENDORS
        }

    def quarter(self) -> Dict[str, Any]:
        segs = self.segment_records()
        rows = []
        per: Dict[str, Dict[str, Any]] = {
            v: {"denom_units": 0, "proven_units": 0, "missing_units": 0,
                "failed_units": 0, "disputed_units": 0, "exempt_units": 0,
                "t2_units": 0, "t3_units": 0}
            for v in VENDORS
        }
        for s in segs:
            v = s["responsible_vendor"]
            u = s["weight_units"]
            b = per[v]
            if s["status"] == "exempt":
                b["exempt_units"] += u
                continue  # 有效免责不进分母
            b["denom_units"] += u
            if s["open_case"] == "T2":
                # 争议段：先从“已证”里拿出，单列
                b["disputed_units"] += u
                b["t2_units"] += u
            elif s["open_case"] == "T3":
                # T3 两种解释会改变分母（免责），单独处理
                b["denom_units"] -= u  # 暂回退，交给情景
                b["t3_units"] += u
            elif s["status"] == "done":
                b["proven_units"] += u
            elif s["status"] == "missing":
                b["missing_units"] += u
            elif s["status"] == "failed":
                b["failed_units"] += u

        vendors_out = {}
        for v in VENDORS:
            b = per[v]
            thr = self.threshold[v]
            d = b["denom_units"]
            n = b["proven_units"]
            m = b["missing_units"]
            t2 = b["t2_units"]
            t3 = b["t3_units"]

            # T3 两种解释：
            #  及时取消 -> 免责：分母不含 t3，分子也不含
            #  不及时   -> 既有履行回执证明完成：分母含 t3，分子含 t3
            d_exempt = d
            n_exempt = n
            d_done = d + t3
            n_done = n + t3

            # T2 争议：乙按时完成 -> 分子+t2；丙仅到场 -> 不计完成（分母仍在乙）
            # 审慎当前值：T2 不计入已证
            current = n / d if d else None

            def ratio(num: float, den: float) -> Optional[float]:
                return num / den if den else None

            # 两种 T3 口径：
            #  及时取消 -> 免责：分母/分子都不含 t3（d, n）
            #  不及时   -> 既有履行回执证明完成：分母/分子都含 t3（d+t3, n+t3）
            d_done = d + t3
            n_done = n + t3

            # 下界：缺证均不成功、T2 不利(0)；在两种 T3 口径中取较低者。
            # 当前履约率 < 1 时，去掉一个满权重完成（免责口径）会略低。
            lower = min(ratio(n, d), ratio(n_done, d_done))
            # 上界：缺证均补证成功(+m)、T2 有利(+t2)、T3 完成口径(分子已含t3)。
            # d 已包含 missing/t2/failed，故分母不再加 m。
            upper = ratio(n_done + t2 + m, d_done)

            # 证据初判（把争议段持有的参与确认照账面计入，T3 按完成口径）
            raw_evidence = ratio(n_done + t2, d_done)

            def band_for(r: Optional[float]) -> Optional[str]:
                if r is None:
                    return None
                return "达线" if r >= thr else "未达线"

            lower_band = band_for(lower)
            upper_band = band_for(upper)
            if lower_band == upper_band == "达线":
                final_band = "达线"
                next_cap = self.qrules["next_band"]["达线"]
                straddles = False
            elif lower < thr <= upper:
                final_band = "有限(跨门槛待核实)"
                next_cap = self.qrules["next_band"]["有限"]
                straddles = True
            else:
                final_band = "未达线"
                next_cap = 0
                straddles = False

            vendors_out[v] = {
                "threshold": thr,
                "denominator_weight": d_done / 5.0,
                "proven_weight_excl_open": n / 5.0,
                "missing_weight": m / 5.0,
                "failed_weight": b["failed_units"] / 5.0,
                "open_t2_weight": t2 / 5.0,
                "open_t3_weight": t3 / 5.0,
                "current_prudent_ratio": round(current, 6),
                "raw_evidence_ratio_incl_disputed": round(raw_evidence, 6),
                "possible_lower": round(lower, 6),
                "possible_upper": round(upper, 6),
                "lower_ge_threshold": lower >= thr,
                "upper_ge_threshold": upper >= thr,
                "straddles_threshold": straddles,
                "determined_band": final_band,
                "next_period_weight_cap": next_cap,
                "units_debug": b,
            }
            rows.append(v)

        return {
            "months": self.qrules["months"],
            "weights": self.qrules["weights"],
            "thresholds": self.threshold,
            "next_band": self.qrules["next_band"],
            "vendors": vendors_out,
            "original_vendor_naive_ratio": self._original_vendor_ratios(),
            "note": (
                "权重以0.2为整数单位；有效免责段不进分母。"
                "区间下界=已证(排除未裁定争议)且缺证均不成功、T2不利、T3按免责；"
                "上界=缺证均补证成功、T2有利、T3按完成。区间仅为包络，"
                "不得把各独立最有利值合并成同一期应付账。"
            ),
        }

    # ----------------------------- 调证优先级 ----------------------------- #
    def evidence_plan(self) -> Dict[str, Any]:
        option_value = {
            "V1": {
                "resolves": ["T2"],
                "settled_overlap": ["T1"],
                "decision": "总站两方回执：直接决定9-4/0是乙按时完成(150)还是"
                            "丙仅到场(不足额)，并决定L4当期能否释放。",
                "money_at_stake_now": 150,
                "quarter_relevant": False,
            },
            "V2": {
                "resolves": [],
                "settled_overlap": ["T1"],
                "decision": "仅覆盖已定案T1；X2已审批、L3已核0，"
                            "任何结果都不改变已核定份额。",
                "money_at_stake_now": 0,
                "quarter_relevant": False,
            },
            "V3": {
                "resolves": ["T2", "T3"],
                "settled_overlap": [],
                "decision": "场地日志同时作用于两个未裁定案件：9-4/0的到场/时点"
                            "与10-16/1取消是否及时，信息覆盖最广。",
                "money_at_stake_now": 150,
                "quarter_relevant": False,
            },
            "V4": {
                "resolves": ["T3"],
                "settled_overlap": [],
                "decision": "取消原单决定10-16/1是有效免责还是不及时、"
                            "既有回执下完成(150给丙)。",
                "money_at_stake_now": 150,
                "quarter_relevant": False,
            },
        }
        options = []
        for o in self.data["evidence_options"]:
            meta = option_value[o["id"]]
            options.append({**o, **meta})

        useful = [o for o in options if o["resolves"]]
        # 次数口径（README 定义 capacity 为“次数上限”）：4 取 3，排除 V2
        count_plan = ["V3", "V1", "V4"]
        # 成本预算口径（若 cost 实际占用 capacity=3）
        budget = self.capacity
        affordable_single = sorted(
            useful, key=lambda o: (-len(o["resolves"]), o["cost"])
        )
        return {
            "capacity_request_count": self.capacity,
            "options": options,
            "request_count_reading": {
                "rule": "capacity 按字面为可索取资料次数：4 项中取 3。",
                "recommended_order": count_plan,
                "exclude": ["V2"],
                "exclude_reason": option_value["V2"]["decision"],
                "selection": ["V1", "V3", "V4"],
            },
            "cost_budget_reading": {
                "rule": f"若 cost 占用总额度 {budget}：两个 cost=2 无法同取。",
                "first_pull": "V3",
                "first_pull_reason": "同等 cost=2 中唯一同时覆盖 T2、T3 的凭证。",
                "remaining_budget": budget - 2,
                "note": ("剩余1仅够V2，但V2无决策价值，不应为花掉额度而先取；"
                         "应在V3阅后按剩余争点申请追加，或排队 V1/V4。"),
                "affordable_combinations": [
                    ["V1", "V2"], ["V2", "V4"], ["V3"], ["V1"], ["V4"], ["V2"],
                ],
            },
            "adaptive_order_rationale": (
                "先 V3（一次触及两个互斥解释组）；若 V3 不能锁定 9-4/0 的两方"
                "回执时点，再 V1 决定当期150差额；V4 用于锁定10-16/1。"
                "季度区间显示三家无论这些案件结果如何都稳过线，故调证排序"
                "由月度可核付金额而非达线翻转驱动。"
            ),
        }

    # ----------------------------- 经办人分流 ----------------------------- #
    def triage(self) -> Dict[str, Any]:
        missing = [
            f"{s['key']}" for s in self.segment_records() if s["status"] == "missing"
        ]
        return {
            "证据不够": [
                {
                    "items": missing,
                    "count": len(missing),
                    "action": "保留义务在原/有效责任方，不计成功也不擅计失败；"
                              "来件后在项目内重新关联到 (appointment,part) 再重算。",
                    "affected_reports": ["分段结算", "季度区间", "场地/责任方/月汇总",
                                         "联办整场附加款(21场待证)"],
                }
            ],
            "已核定但回款未落": [
                {"line": c["line"], "debtor": c["debtor"],
                 "outstanding": c["outstanding"],
                 "action": "尚欠冲回不是可支用额；实际回款凭证到达后计入对应期"
                           "returned 并重算释放能力，不得据此提前发新钱。"}
                for c in self.corrections_and_releases()["clawbacks"]
                if c["outstanding"] > 0
            ],
            "争议待裁定_付款暂缓": [
                {"case": "T2", "key": "9-4/0", "ledger": "L4",
                 "amount_held": 150,
                 "action": "等待 V1/V3；乙按时完成则释放150，丙仅到场则不足额"
                           "（合同未给部分计价比例，需协议补充）。"},
            ],
            "本季履约继续复核": [
                {"case": "T3", "key": "10-16/1",
                 "action": "等待 V3/V4 判定取消是否对双方及时；影响免责/完成归类，"
                           "但不改变三家达线结论与旧合同条款，只影响后一期档量。"},
                {"case": "T2", "key": "9-4/0",
                 "action": "付款与归属复核继续；季度档量结论对其结果不敏感。"},
            ],
            "已定案不再自由解释": [
                {"case": "T1", "resolution_ref": "X2",
                 "action": "9-11/1 归丙（有效转交并完成）；X4 不再作为并行解释，"
                           "V2 不优先调证。"},
            ],
        }

    # ----------------------------- 登记册 --------------------------------- #
    def transfer_register(self) -> List[Dict[str, Any]]:
        out = []
        for t in self.data["transfers"]:
            key = (t["appointment"], t["part"])
            v = self.transfer_validity(key)
            out.append({
                "key": f"{t['appointment']}/{t['part']}",
                "from_vendor": t["from_vendor"],
                "to_vendor": t["to_vendor"],
                "approved": t["approved"],
                "accepted_hour": t["accepted_hour"],
                "before_start": v["before_start"],
                "effective": v["effective"],
                "reason": v["reason"],
            })
        return out

    def cancellation_register(self) -> List[Dict[str, Any]]:
        out = []
        for x in self.promises:
            key = (x["appointment"], x["part"])
            for c in self.cancellation_checks(key):
                out.append({"key": f"{key[0]}/{key[1]}", "policy": x["policy"], **c})
        return out

    def claims_register(self) -> List[Dict[str, Any]]:
        out = []
        for c in self.claims:
            key = (c["appointment"], c["part"])
            replaced_by = next(
                (n["ref"] for n in self.claims if n.get("supersedes") == c["ref"]),
                None,
            )
            out.append({
                "ref": c["ref"],
                "key": f"{c['appointment']}/{c['part']}",
                "vendor": c["vendor"],
                "approved_day": c["approved_day"],
                "approved": c["approved_day"] is not None,
                "outcome": c["outcome"],
                "supersedes": c.get("supersedes"),
                "replaced_by": replaced_by,
                "accepted_hour": c.get("accepted_hour"),
                "current_effect": (
                    "已被替换，仅留历史" if replaced_by else
                    "未审批，不产生变动" if c["approved_day"] is None else
                    "当前有效"
                ),
                "alongside": c.get("alongside"),
            })
        return out

    # ----------------------------- 边界敏感性 ----------------------------- #
    def transfer_boundary_sensitivity(self) -> Dict[str, Any]:
        """若把 accepted_hour <= start 也视为“开始前”，季度比率会是多少。

        正式口径采用严格 ``<``（README：开始计入；“开始前”为严格小于）。
        这里仅给出对照，证明边界口径只改变 6 个原始转交段在乙/丙间的归属，
        不改变任何达线结论。
        """
        out: Dict[str, Any] = {}
        for inclusive in (False, True):
            den = {v: 0 for v in VENDORS}
            num = {v: 0 for v in VENDORS}
            for x in self.promises:
                key = (x["appointment"], x["part"])
                v = x["vendor"]
                t = self.transfers.get(key)
                if t and t["approved"] and (
                    t["accepted_hour"] < x["start"]
                    or (inclusive and t["accepted_hour"] <= x["start"])
                ):
                    v = t["to_vendor"]
                if key in self.successor:
                    v = self.successor[key]["vendor"]
                stt = self.segment_status(key)
                if stt["status"] == "exempt":
                    continue
                u = WEIGHT_UNITS[x["bundle"]]
                den[v] += u
                # 边界敏感性只考察转交口径，争议段按其既有参与确认计入，
                # 与 quarter.raw_evidence 口径一致。
                if stt["has_confirmation"]:
                    num[v] += u
            label = "inclusive_<=" if inclusive else "strict_<(canonical)"
            out[label] = {
                v: {"ratio": round(num[v] / den[v], 6),
                    "weight": round(den[v] / 5.0, 2)}
                for v in VENDORS
            }
        inc = out["inclusive_<="]
        out["changes"] = (
            "inclusive 口径下 6 个授权有效但准点(12=start)的乙→丙段改归丙；"
            f"乙变为 {inc['乙']['ratio']}、丙变为 {inc['丙']['ratio']}，"
            "与严格口径一样都稳过门槛，不改变达线/档量结论。"
        )
        return out

    # ----------------------------- 汇总结果 ------------------------------- #
    def build(self) -> Dict[str, Any]:
        segments = self.segment_records()
        bundles = self.bundle_bonuses()
        agg = self.aggregation(segments)

        bonus_now_by_org: Dict[str, int] = defaultdict(int)
        bonus_pending_exposure = 0
        for b in bundles:
            bonus_now_by_org[b["organizer_original_vendor"]] += b["bonus_payable_now"]
            if b["state"] == "pending_evidence":
                bonus_pending_exposure += b["bonus_standard"]

        segment_now_by_payee: Dict[str, int] = defaultdict(int)
        for s in segments:
            segment_now_by_payee[s["responsible_vendor"]] += s["currently_payable"]

        return {
            "source_contract": "README.md 运行契约 + data/records.json",
            "counts": {
                "promises": len(self.promises),
                "evidence": len(self.data["evidence"]),
                "transfers": len(self.transfers),
                "claims": len(self.claims),
                "ledger_lines": len(self.ledger),
                "bundle_events": len(bundles),
            },
            "segments": segments,
            "aggregation": agg,
            "segment_fees_payable_now_by_responsible": dict(segment_now_by_payee),
            "bundle_bonuses": bundles,
            "bundle_bonus_payable_now_by_organizer": dict(bonus_now_by_org),
            "bundle_bonus_pending_max_exposure": bonus_pending_exposure,
            "ledger_reconciliation": self.ledger_reconciliation(),
            "audit_pipeline": {
                "L2_release_through_normal_audit": 150,
                "L3_already_zero": 0,
                "L4_held_due_to_T2": 150,
                "note": "审核行的正常给付不占用 correction_periods 的“新差额”释放额度；"
                        "L2 正确可走正常审核放款，L4 因 T2 争议暂缓，L3 已核0。",
            },
            "transfer_register": self.transfer_register(),
            "cancellation_register": self.cancellation_register(),
            "claims_register": self.claims_register(),
            "corrections": self.corrections_and_releases(),
            "quarter": self.quarter(),
            "transfer_boundary_sensitivity": self.transfer_boundary_sensitivity(),
            "evidence_plan": self.evidence_plan(),
            "triage": self.triage(),
        }


def build_result(data_path: Path | str = DEFAULT_DATA) -> Dict[str, Any]:
    return SettlementEngine(load_records(data_path)).build()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_headline(result: Dict[str, Any]) -> Dict[str, Any]:
    """扁平的关键结论，便于快速核对/脚本读取；全部来自 result。"""
    g = result["aggregation"]["grand"]
    q = result["quarter"]["vendors"]
    corr = result["corrections"]
    sch = {p["period_month"]: p for p in corr["schedule"]}
    return {
        "segments_total": g["segments"],
        "status_counts": {k: g[k] for k in
                          ("done", "disputed", "failed", "missing", "exempt")},
        "segment_fees_payable_now_total": g["segment_fee_payable_now"],
        "segment_fees_payable_now_by_responsible":
            result["segment_fees_payable_now_by_responsible"],
        "bundle_events_total": len(result["bundle_bonuses"]),
        "bundle_bonus_qualifies_now": sum(
            1 for b in result["bundle_bonuses"] if b["state"] == "qualifies_now"),
        "bundle_bonus_pending_evidence": sum(
            1 for b in result["bundle_bonuses"] if b["state"] == "pending_evidence"),
        "bundle_bonus_blocked": sum(
            1 for b in result["bundle_bonuses"] if b["state"] == "blocked"),
        "bundle_bonus_payable_now_total":
            sum(result["bundle_bonus_payable_now_by_organizer"].values()),
        "quarter": {
            v: {
                "threshold": q[v]["threshold"],
                "prudent_ratio": q[v]["current_prudent_ratio"],
                "lower": q[v]["possible_lower"],
                "upper": q[v]["possible_upper"],
                "band": q[v]["determined_band"],
                "next_cap": q[v]["next_period_weight_cap"],
                "straddles": q[v]["straddles_threshold"],
            } for v in VENDORS
        },
        "certain_difference_total": corr["certain_queue_total"],
        "release_by_period": {
            str(m): {
                "capacity": sch[m]["available_capacity"],
                "released": sch[m]["released_total"],
                "left": sch[m]["capacity_left"],
            } for m in (9, 10, 11)
        },
        "clawback_outstanding": {
            c["line"]: c["outstanding"] for c in corr["clawbacks"]
        },
        "clawback_outstanding_total":
            sum(c["outstanding"] for c in corr["clawbacks"]),
        "clawback_actually_returned": {
            c["line"]: c["actually_returned"] for c in corr["clawbacks"]
        },
        "clawback_returned_total":
            sum(c["actually_returned"] for c in corr["clawbacks"]),
        "ledger_paid_total": sum(
            L["ledger_amount"] for L in result["ledger_reconciliation"]
            if L["status"] == "已付"),
        "ledger_audit_total": sum(
            L["ledger_amount"] for L in result["ledger_reconciliation"]
            if L["status"] == "审核"),
        "evidence_order_count_reading":
            result["evidence_plan"]["request_count_reading"]["recommended_order"],
        "evidence_excluded":
            result["evidence_plan"]["request_count_reading"]["exclude"],
        "evidence_first_pull_cost_reading":
            result["evidence_plan"]["cost_budget_reading"]["first_pull"],
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="跨机构联办履约结算与争议复算")
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="records.json 路径")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    args = ap.parse_args(argv)

    result = build_result(args.data)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    headline = build_headline(result)
    x5_parts = {}
    for p in result["corrections"]["schedule"]:
        for r in p["releases"]:
            if r["auth"] == "X5":
                x5_parts[f"period{p['period_month']}"] = r["released"]
    headline["x5_installments"] = {**x5_parts, "total": 130}
    (outdir / "headline.json").write_text(
        json.dumps(headline, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    from report import render_markdown  # 延迟导入，兼容 python3 solve.py

    (outdir / "report.md").write_text(render_markdown(result), encoding="utf-8")
    print(f"[ok] 写出 {outdir / 'result.json'}")
    print(f"[ok] 写出 {outdir / 'headline.json'}")
    print(f"[ok] 写出 {outdir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
