"""Effort estimation.

Each impacted element contributes hours from a coefficient table, scaled by
complexity, size and fan-in; elements with no test coverage add test effort.
A calibration factor fitted from past actuals (predicted vs actual person-days)
scales the total. Every line of the breakdown is returned so the viewer can show
where the number comes from."""
from __future__ import annotations

import json
import os
import statistics

from ..model import Kind

DEFAULT_COEF = {
    "hours": {  # base hours per element and change type
        Kind.FIELD: {"add": 0.5, "modify": 0.5, "delete": 0.5},
        Kind.METHOD: {"add": 2.0, "modify": 1.0, "delete": 0.5},
        Kind.CLASS: {"add": 3.0, "modify": 1.0, "delete": 1.0},
        Kind.TEMPLATE: {"add": 3.0, "modify": 1.5, "delete": 0.5},
        Kind.COLUMN: {"add": 0.5, "modify": 0.5, "delete": 0.5},
        Kind.TABLE: {"add": 1.0, "modify": 0.5, "delete": 0.5},
        Kind.SQL_FILE: {"add": 0.5, "modify": 0.5, "delete": 0.25},
        Kind.MESSAGE: {"add": 0.25, "modify": 0.25, "delete": 0.1},
        Kind.CONFIG: {"add": 0.25, "modify": 0.25, "delete": 0.1},
        Kind.TEST_METHOD: {"add": 1.0, "modify": 0.75, "delete": 0.25, "test": 0.75},
        Kind.TEST_CLASS: {"add": 2.0, "modify": 0.5, "delete": 0.5, "test": 0.5},
        Kind.DOC: {"add": 1.0, "modify": 0.5, "delete": 0.25, "doc": 0.5},
    },
    "complexity_step": 0.15,       # +15% per cyclomatic point above 1
    "loc_step": 0.01,              # +1% per line above 20
    "fanin_step": 0.03,            # +3% per caller above 3 (regression risk)
    "missing_test_hours": 1.5,     # writing a new test when nothing covers the element
    "overhead": 1.3,               # review, integration, QA
    "hours_per_day": 8.0,
    "calibration": 1.0,
}


class Estimator:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "estimate_coefficients.json")
        self.coef = json.loads(json.dumps(DEFAULT_COEF))
        if os.path.exists(self.path):
            saved = json.load(open(self.path, encoding="utf-8"))
            self.coef.update(saved)

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        json.dump(self.coef, open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    def calibrate(self, history: list[dict]) -> dict:
        """history: [{"predicted_days": float, "actual_days": float, "ref": str}]"""
        ratios = [h["actual_days"] / h["predicted_days"] for h in history
                  if h.get("predicted_days") and h.get("actual_days")]
        if not ratios:
            return {"calibration": self.coef["calibration"], "samples": 0}
        self.coef["calibration"] = round(statistics.median(ratios), 3)
        self.coef["calibration_samples"] = len(ratios)
        self.save()
        return {"calibration": self.coef["calibration"], "samples": len(ratios)}

    def element_hours(self, cand: dict, node_props: dict, fan_in: int, tested: bool) -> tuple[float, list[str]]:
        kind = cand["kind"]
        table = self.coef["hours"].get(kind)
        if not table:
            return 0.0, []
        ct = cand.get("change_type") or "modify"
        base = table.get(ct, table.get("modify", 0.5))
        notes = [f"基準 {base}h（{kind}・{ct}）"]
        factor = 1.0
        cc = node_props.get("complexity", 1) or 1
        if cc > 1:
            factor += (cc - 1) * self.coef["complexity_step"]
            notes.append(f"複雑度 {cc}")
        loc = node_props.get("loc", 0) or 0
        if loc > 20:
            factor += (loc - 20) * self.coef["loc_step"]
            notes.append(f"{loc}行")
        if fan_in > 3:
            factor += (fan_in - 3) * self.coef["fanin_step"]
            notes.append(f"呼び出し元 {fan_in}")
        diff = cand.get("difficulty")
        if diff in ("low", "high"):
            factor *= 0.7 if diff == "low" else 1.5
            notes.append(f"難易度補正 {diff}")
        if kind == Kind.MESSAGE:
            files = len(node_props.get("files", {})) or 1
            factor *= files
            notes.append(f"{files}ロケール")
        hours = base * factor
        if not tested and kind in (Kind.METHOD, Kind.CLASS, Kind.TEMPLATE):
            hours += self.coef["missing_test_hours"]
            notes.append("テスト未整備 +" + str(self.coef["missing_test_hours"]) + "h")
        return round(hours, 2), notes

    def summarize(self, lines: list[dict], pending: list[dict]) -> dict:
        hpd = self.coef["hours_per_day"]
        k = self.coef["overhead"] * self.coef["calibration"]
        confirmed_h = sum(l["hours"] for l in lines)
        pending_h = sum(p["hours"] * p["probability"] for p in pending)
        pending_max = sum(p["hours"] for p in pending)
        days = (confirmed_h + pending_h) * k / hpd
        return {
            "days": round(days, 1),
            "low": round(confirmed_h * k / hpd, 1),
            "high": round((confirmed_h + pending_max) * k / hpd, 1),
            "confirmed_hours": round(confirmed_h, 1),
            "pending_expected_hours": round(pending_h, 1),
            "overhead": self.coef["overhead"],
            "calibration": self.coef["calibration"],
            "calibration_samples": self.coef.get("calibration_samples", 0),
            "lines": sorted(lines, key=lambda l: -l["hours"]),
            "pending": sorted(pending, key=lambda p: -p["hours"] * p["probability"])[:50],
        }
