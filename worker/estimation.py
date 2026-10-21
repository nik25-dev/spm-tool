"""Traditional estimation + earned value + blending. Pure formulas: no training, no data needed."""
from datetime import timedelta
from math import sqrt

# ---- COCOMO 81 coefficients (a, b, c, d) -------------------------------------------------
COCOMO_MODES = {
    "organic":      {"a": 2.4, "b": 1.05, "c": 2.5, "d": 0.38, "a_int": 3.2},
    "semidetached": {"a": 3.0, "b": 1.12, "c": 2.5, "d": 0.35, "a_int": 3.0},
    "embedded":     {"a": 3.6, "b": 1.20, "c": 2.5, "d": 0.32, "a_int": 2.8},
}


def cocomo_basic(kloc: float, mode: str = "organic") -> dict:
    """Basic COCOMO: effort (person-months), schedule (months), average team size."""
    m = COCOMO_MODES[mode]
    effort = m["a"] * kloc ** m["b"]
    months = m["c"] * effort ** m["d"]
    return {"effort_pm": effort, "months": months, "avg_team": effort / months}


def cocomo_intermediate(kloc: float, mode: str = "organic", eaf: float = 1.0) -> dict:
    """Intermediate COCOMO: same as basic but with a different 'a' and an Effort Adjustment Factor.
    EAF = product of the cost-driver multipliers (1.0 = nominal)."""
    m = COCOMO_MODES[mode]
    effort = m["a_int"] * kloc ** m["b"] * eaf
    months = m["c"] * effort ** m["d"]
    return {"effort_pm": effort, "months": months, "avg_team": effort / months}


# ---- PERT three-point ---------------------------------------------------------------------
def pert(optimistic: float, most_likely: float, pessimistic: float) -> dict:
    expected = (optimistic + 4 * most_likely + pessimistic) / 6
    sigma = (pessimistic - optimistic) / 6
    return {"expected": expected, "sigma": sigma}


def pert_rollup(items: list[dict]) -> dict:
    """Sum expected values; combine uncertainty as sqrt(sum of variances) (assumes independence)."""
    return {
        "expected": sum(i["expected"] for i in items),
        "sigma": sqrt(sum(i["sigma"] ** 2 for i in items)),
    }


# ---- Earned value (schedule side only; we have no cost data) -------------------------------
def workdays_between(start, end, per_week: int = 5) -> int:
    """Working days in (start, end]: Mon-Fri when per_week=5, every day when per_week=7."""
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if per_week >= 7 or d.weekday() < 5:
            n += 1
    return n


def planned_value(today, start, end, per_week: int = 7) -> float:
    """Planned % complete (0..1): share of the milestone's working days that have elapsed."""
    total = workdays_between(start, end, per_week)
    if total <= 0:
        return 1.0
    if today <= start:
        return 0.0
    return min(1.0, workdays_between(start, min(today, end), per_week) / total)


def spi(earned: float, planned: float) -> float:
    """Schedule Performance Index = EV / PV. <1 behind, 1 on plan, >1 ahead."""
    return 1.0 if planned == 0 else earned / planned


# ---- Blend traditional and data-driven estimates --------------------------------------------
def blend(traditional: float, learned: float | None, n_closed: int, n0: int = 30) -> dict:
    """Trust formulas early, learned model later. w grows linearly to 1 at n0 closed issues."""
    if learned is None:
        return {"final": traditional, "weight_learned": 0.0, "disagree": False}
    w = min(1.0, n_closed / n0)
    final = (1 - w) * traditional + w * learned
    gap = abs(traditional - learned) / max(traditional, learned, 1e-9)
    return {"final": final, "weight_learned": w, "disagree": gap > 0.30}
