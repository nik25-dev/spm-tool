"""Dependency graph, critical path, forecast helpers, project roll-up and specific findings. Pure functions."""
import math
from datetime import date, timedelta

from estimation import workdays_between


def add_workdays(start: date, n: float, per_week: int = 5) -> date:
    """Date reached after n working days (rounded up)."""
    n, d = math.ceil(max(n, 0)), start
    while n > 0:
        d += timedelta(days=1)
        if per_week >= 7 or d.weekday() < 5:
            n -= 1
    return d


def critical_path(tasks: dict) -> tuple:
    """tasks = {number: {"days": float, "blocked_by": [numbers]}} for UNFINISHED tasks only.
    Returns (length_in_days, chain) where chain runs from the first task to the last.
    Returns (None, []) if the dependencies contain a cycle."""
    memo, state = {}, {}

    def longest(n):
        if n in memo:
            return memo[n]
        if state.get(n) == 1:
            raise ValueError("cycle")
        state[n] = 1
        best, chain = 0.0, []
        for b in tasks[n]["blocked_by"]:
            if b in tasks:
                length, c = longest(b)
                if length > best:
                    best, chain = length, c
        state[n] = 2
        memo[n] = (best + tasks[n]["days"], chain + [n])
        return memo[n]

    try:
        results = [longest(n) for n in tasks]
    except ValueError:
        return None, []
    return max(results, key=lambda r: r[0]) if results else (0.0, [])


def waiting_on(task: int, tasks: dict) -> list:
    """All unfinished tasks that (directly or indirectly) wait for `task`."""
    out, frontier = set(), {task}
    while frontier:
        nxt = {n for n, t in tasks.items() if n not in out and frontier & set(t["blocked_by"])}
        out |= nxt
        frontier = nxt
    return sorted(out)


def project_rollup(reports: list, config: dict, today: date, per_week: int = 5) -> dict:
    """Whole-project earned value (schedule) and, if a budget and team rates are configured, cost."""
    total = sum(r["total_weight"] for r in reports) or 1
    ev = sum(r["done_weight"] for r in reports) / total
    pv = sum(r["planned_weight"] for r in reports) / total
    # a slip in any milestone moves the project end by the same amount (assumes milestones run one after another)
    due = config.get("project_end") or max(m["end"] for m in config["milestones"])
    slip = max((r.get("slip_days", 0) for r in reports), default=0)
    out = {"ev_pct": round(ev * 100, 1), "pv_pct": round(pv * 100, 1), "spi": round(ev / pv, 2) if pv > 0 else 1.0,
           "forecast_date": add_workdays(date.fromisoformat(due), slip, per_week).isoformat(), "kloc": config.get("kloc")}
    pstart = date.fromisoformat(min(m["start"] for m in config["milestones"]))
    out["work_days_total"] = workdays_between(pstart, date.fromisoformat(due), per_week)
    out["work_days_elapsed"] = workdays_between(pstart, min(today, date.fromisoformat(due)), per_week) if today > pstart else 0
    budget, team = config.get("budget"), config.get("team") or []
    if budget and team:
        start = date.fromisoformat(min(m["start"] for m in config["milestones"]))
        elapsed = workdays_between(start, today, per_week)
        ac = sum(float(t.get("daily_rate", 0)) * float(t.get("availability", 1.0)) for t in team) * elapsed
        cpi = (ev * budget) / ac if ac > 0 else None
        out.update({"bac": budget, "ac": round(ac), "cpi": round(cpi, 2) if cpi else None,
                    "eac": round(budget / cpi) if cpi else None})
    return out


def _nums(items, limit=4):
    return ", ".join(f"#{x}" for x in items[:limit]) + (" and more" if len(items) > limit else "")


def build_findings(c: dict) -> tuple:
    """Specific, plain-language findings. Returns (public, lead): the public list names tasks and PRs but never people;
    the lead list adds names. Each finding = {severity, text, action}."""
    pub, lead = [], []

    def add(sev, text, action, ltext=None, laction=None):
        pub.append({"severity": sev, "text": text, "action": action})
        lead.append({"severity": sev, "text": ltext or text, "action": laction or action})

    head = c.get("head")
    if head and head["pr"] and head["pr"]["age"] > 3:
        pr, n = head["pr"], len(head["unblocks"])
        add("high",
            f"Pull request #{pr['number']} ('{head['title']}') has waited {pr['age']:.0f} days for review and holds up {n} other task(s).",
            "Get this pull request reviewed and merged first.",
            ltext=f"Pull request #{pr['number']} ('{head['title']}', opened by {pr['author']}) has waited {pr['age']:.0f} days for review and holds up {n} other task(s): {_nums(head['unblocks'])}.",
            laction=f"Review and merge #{pr['number']} first. It unblocks {_nums(head['unblocks'])}.")
    elif head and not head["pr"] and c.get("pv", 0) >= 0.5:
        add("high", f"Nobody has started '{head['title']}' (#{head['number']}), the first task on the critical path.",
            "Start this task now: everything after it is waiting.",
            ltext=f"Nobody has started #{head['number']} '{head['title']}' (assigned to {head['assignee'] or 'nobody'}), the first task on the critical path.",
            laction=f"Ask {head['assignee'] or 'someone'} to start #{head['number']} today.")

    if c.get("slip", 0) > 0:
        late = f"{c['slip']:.0f}" if c["slip"] <= 20 else "more than 20"
        lead_in = {"evm": "At the current pace", "critical": "Because the remaining tasks must be done in order",
                   "capacity": "With the current team capacity"}.get(c.get("driver"), "At the current pace")
        base = (f"{lead_in}, {c['milestone']} is forecast to finish {c['forecast']:%a %d %b}, "
                f"{late} working day(s) after its due date ({c['due']:%a %d %b}).")
        cand, idle = c.get("candidate"), c.get("idle")
        if cand and idle:
            add("high", base, "Reduce scope, add capacity, or move the due date.", ltext=base,
                laction=f"{idle} has no open tasks and could take #{cand['number']} '{cand['title']}' from {cand['from']}.")
        else:
            add("high", base, "Reduce scope, add capacity, or move the due date.")

    if c.get("test_days", 0) > 0 and c.get("test_left", 99) < c["test_days"]:
        left = (f"No working days remain for testing" if c["test_left"] == 0 else f"Only {c['test_left']} working day(s) remain for testing")
        add("medium", f"{left}; {c['test_days']} were planned.",
            "Protect the testing window: finish blocked work first or move scope out.")

    others = [p for p in c.get("stale", []) if not (head and head["pr"] and p["number"] == head["pr"]["number"])]
    if others:
        add("medium", f"{len(others)} other pull request(s) are waiting for review: {_nums([p['number'] for p in others])}.",
            "Review them today.",
            ltext=f"{len(others)} other pull request(s) are waiting for review: " + ", ".join(f"#{p['number']} ({p['author']})" for p in others[:4]) + ".")

    if c.get("scope_pct", 0) > 0:
        added = c.get("scope_added") or []
        which = f" ({_nums([a['number'] for a in added])})" if added else ""
        add("medium", f"The milestone's scope has grown {c['scope_pct']:.0f}% since the plan was set{which}.",
            "Freeze new scope, or formally re-baseline the plan.")

    if c.get("overdue"):
        add("high", f"{c['overdue']} task(s) are past the milestone's due date.", "Re-plan or re-date the overdue work.")

    order = {"high": 0, "medium": 1, "info": 2}
    pub.sort(key=lambda f: order[f["severity"]])
    lead.sort(key=lambda f: order[f["severity"]])
    return pub[:5], lead[:5]
