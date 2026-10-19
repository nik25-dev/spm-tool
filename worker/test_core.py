from datetime import date
import random
from estimation import *
from analytics import *


def test_cocomo_basic_10kloc():
    r = cocomo_basic(10, "organic")
    assert round(r["effort_pm"], 1) == 26.9
    assert round(r["months"], 1) == 8.7
    assert round(r["avg_team"], 1) == 3.1


def test_cocomo_intermediate_scales_with_eaf():
    base = cocomo_intermediate(10, "organic", 1.0)["effort_pm"]
    assert abs(cocomo_intermediate(10, "organic", 1.2)["effort_pm"] - 1.2 * base) < 1e-9


def test_pert():
    r = pert(2, 4, 10)
    assert r["expected"] == 4.666666666666667 and round(r["sigma"], 2) == 1.33
    roll = pert_rollup([pert(2, 4, 10), pert(1, 2, 3)])
    assert round(roll["expected"], 2) == 6.67


def test_evm():
    pv = planned_value(date(2026, 10, 15), date(2026, 10, 1), date(2026, 10, 29))
    assert pv == 0.5
    assert spi(0.4, pv) == 0.8


def test_blend_cold_start_uses_formula():
    assert blend(20, 10, n_closed=0)["final"] == 20
    b = blend(20, 10, n_closed=30)
    assert b["final"] == 10 and b["disagree"] is True
    assert blend(20, None, 5)["weight_learned"] == 0.0


def test_delay_risk_levels_and_reasons():
    healthy = delay_risk({})
    assert healthy["status"] == "green" and healthy["top_reasons"] == []
    bad = delay_risk({"schedule_slip": 1, "projected_late": 1, "stale_prs": 1})
    assert bad["status"] == "red" and bad["top_reasons"][0] == "schedule_slip"
    assert delay_risk({"schedule_slip": 5})["score"] == 30.0  # clamped to 1


def test_bus_factor():
    files = {f"a{i}": {"alice": 9, "bob": 1} for i in range(8)}
    files.update({"b1": {"bob": 5}, "b2": {"carol": 5}})
    r = bus_factor(files)
    assert r["bus_factor"] == 1 and r["key_people"] == ["alice"]  # alice owns 80%


def test_median_and_regression():
    random.seed(1)
    closed = []
    for _ in range(40):
        s = random.choice(["S", "M", "L"])
        pts = {"S": 1, "M": 3, "L": 5}[s]
        closed.append({"size": s, "cycle_days": pts * 1.5 + random.uniform(-0.5, 0.5), "lines": random.randint(10, 200)})
    med = median_by_size(closed)
    assert med["S"] < med["M"] < med["L"]
    predict = fit_linear(closed)
    assert predict("L") > predict("S")
    assert fit_linear(closed[:3]) is None  # too little data -> fall back to medians


def test_template_summary():
    r = delay_risk({"schedule_slip": 0.8, "stale_prs": 1})
    text = template_summary("Payments", 42, r)
    assert "42% complete" in text and "behind the planned schedule" in text
