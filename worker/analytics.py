"""Rule-based risk scoring, bus factor, cycle-time estimation, plain-language summary."""
from collections import defaultdict
from statistics import median

# ---- Delay risk (0-100): weighted, explainable, NO training ----------------------------------
WEIGHTS = {
    "schedule_slip": 0.30,   # 1 - SPI
    "projected_late": 0.25,  # projected finish after due date
    "stale_prs": 0.15,       # open PRs with no review for > 3 days
    "reopen_rate": 0.10,     # reopened / closed issues
    "scope_growth": 0.10,    # issues added after kickoff / original scope
    "overdue_open": 0.10,    # open issues past their due date / open issues
}


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def delay_risk(signals: dict) -> dict:
    """signals: each value already normalised to 0..1 (0 = healthy, 1 = bad)."""
    parts = {k: clamp01(signals.get(k, 0.0)) * w * 100 for k, w in WEIGHTS.items()}
    score = round(sum(parts.values()), 1)
    reasons = [k for k, _ in sorted(parts.items(), key=lambda kv: kv[1], reverse=True) if parts[k] > 0][:3]
    status = "green" if score < 30 else "yellow" if score <= 60 else "red"
    return {"score": score, "status": status, "top_reasons": reasons}


# ---- Bus factor (simplified authorship-share version) ---------------------------------------
def bus_factor(file_authors: dict[str, dict[str, int]], threshold: float = 0.5) -> dict:
    """file_authors: {path: {author: n_changes}}. A file's 'owner' = author with the most changes.
    Greedy: remove the author owning the most files until more than `threshold` of files are orphaned."""
    owner = {p: max(a, key=a.get) for p, a in file_authors.items() if a}
    count = defaultdict(int)
    for o in owner.values():
        count[o] += 1
    total, removed, orphaned = len(owner), [], 0
    for author, n in sorted(count.items(), key=lambda kv: kv[1], reverse=True):
        removed.append(author)
        orphaned += n
        if total and orphaned / total > threshold:
            break
    return {"bus_factor": len(removed) if total else 0, "key_people": removed, "files": total}


# ---- Cycle-time estimate per size label: Tier 0 (median) and Tier 1 (tiny regression) -------
def median_by_size(closed: list[dict]) -> dict:
    """closed: [{'size': 'S'|'M'|'L', 'cycle_days': float}] -> median cycle time per size."""
    buckets = defaultdict(list)
    for c in closed:
        buckets[c["size"]].append(c["cycle_days"])
    return {k: median(v) for k, v in buckets.items()}


def fit_linear(closed: list[dict], size_points={"S": 1, "M": 3, "L": 5}):
    """Tier 1: 'train' a straight line (cycle_days ~ size points + lines changed). Refit every run;
    nothing to save or host. Returns a predict(size, lines) function, or None if too little data."""
    if len(closed) < 8:
        return None
    from sklearn.linear_model import LinearRegression
    X = [[size_points[c["size"]], c.get("lines", 0)] for c in closed]
    y = [c["cycle_days"] for c in closed]
    model = LinearRegression().fit(X, y)
    return lambda size, lines=0: float(max(0.5, model.predict([[size_points[size], lines]])[0]))


# ---- Plain-language summary: template first (free, deterministic) ---------------------------
REASON_TEXT = {
    "schedule_slip": "work is behind the planned schedule",
    "projected_late": "at the current pace the milestone will finish after its due date",
    "stale_prs": "several pull requests are waiting for review",
    "reopen_rate": "many finished tasks had to be reopened",
    "scope_growth": "new tasks were added after the plan was set",
    "overdue_open": "some open tasks are already overdue",
}


def template_summary(milestone: str, pct_done: float, risk: dict) -> str:
    colour = {"green": "on track", "yellow": "at risk", "red": "likely to be late"}[risk["status"]]
    why = "; ".join(REASON_TEXT[r] for r in risk["top_reasons"]) or "no issues detected"
    return f"{milestone} is {pct_done:.0f}% complete and {colour} (risk {risk['score']}/100). Main reasons: {why}."
