"""Analytics job: GitHub -> metrics -> Supabase. Run by GitHub Actions on a schedule (or by hand).

compute_milestone_report() is pure and unit-tested.
main() talks to Supabase and the network; it is NOT tested yet - run it on day 1 of Phase 4 and fix what breaks.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from statistics import median

from analytics import clamp01, confidence, delay_risk, template_summary
from estimation import cocomo_basic, planned_value, spi, workdays_between
from ingest import fetch_repo, issues_closed_via_merged_pr

SIZE_POINTS = {"S": 1, "M": 3, "L": 5}
DEFAULT_DAYS = {"S": 1.0, "M": 3.0, "L": 5.0}   # used until we have real closed issues (cold start)


def size_of(issue: dict) -> str:
    for label in issue["labels"]:
        if label.lower().startswith("size:") and label[5:].strip().upper() in SIZE_POINTS:
            return label[5:].strip().upper()
    return "M"


def estimate_days(data: dict) -> dict:
    """Median cycle time (days) per size from ALL issues closed via a merged PR; defaults until 3+ samples."""
    done = issues_closed_via_merged_pr(data)
    cycle = {"S": [], "M": [], "L": []}
    for i in data["issues"]:
        if i["number"] in done and i["closed_at"]:
            cycle[size_of(i)].append((i["closed_at"] - i["created_at"]).total_seconds() / 86400)
    return {k: (median(v) if len(v) >= 3 else DEFAULT_DAYS[k]) for k, v in cycle.items()}


def compute_workload(data: dict) -> list[dict]:
    """Per developer: open issues, done issues (merged PR), and estimated days of work left."""
    done, est, out = issues_closed_via_merged_pr(data), estimate_days(data), {}
    for i in data["issues"]:
        dev = i["assignee"]
        if not dev:
            continue
        w = out.setdefault(dev, {"developer": dev, "open_issues": 0, "done_issues": 0, "remaining_days": 0.0})
        if i["number"] in done:
            w["done_issues"] += 1
        else:
            w["open_issues"] += 1
            w["remaining_days"] += est[size_of(i)]
    for w in out.values():
        w["remaining_days"] = round(w["remaining_days"], 1)
    return list(out.values())


def compute_milestone_report(data: dict, milestone: dict, today: date,
                             baseline_weight: float | None = None, per_week: int = 5) -> dict:
    start, end = date.fromisoformat(milestone["start"]), date.fromisoformat(milestone["end"])
    issues = [i for i in data["issues"] if i["milestone"] == milestone["name"]]
    if not issues:
        return {"milestone": milestone["name"], "empty": True}

    done_numbers = issues_closed_via_merged_pr(data)
    weight = {i["number"]: SIZE_POINTS[size_of(i)] for i in issues}
    total_w = sum(weight.values())
    done_w = sum(w for n, w in weight.items() if n in done_numbers)
    pct_done = done_w / total_w

    pv = planned_value(today, start, end, per_week)
    conf = confidence(pv)          # 0 at the very start, 1 once 25% of the milestone's time has passed
    index = spi(pct_done, pv)

    est_days = estimate_days(data)

    open_issues = [i for i in issues if i["number"] not in done_numbers]
    remaining_days = sum(est_days[size_of(i)] for i in open_issues)

    # pace = size points finished per WORKING day, over the last 14 days but never before the milestone started
    window_start = max(today - timedelta(days=14), start)
    cutoff = datetime.combine(window_start, datetime.min.time(), tzinfo=timezone.utc)
    recent_done = sum(weight[i["number"]] for i in issues
                      if i["number"] in done_numbers and i["closed_at"] and i["closed_at"] >= cutoff)
    velocity = recent_done / max(1, workdays_between(window_start, today, per_week))
    remaining_w = total_w - done_w
    days_left = workdays_between(today, end, per_week)
    if remaining_w == 0:
        projected_late = 0.0
    elif velocity == 0:
        projected_late = 1.0
    else:
        projected_late = clamp01((remaining_w / velocity - days_left) / max(days_left, 1))

    # only PRs that reference THIS milestone's issues ("Closes #N") count toward its stale-PR signal
    ms_numbers = {i["number"] for i in issues}
    open_prs = [p for p in data["prs"]
                if p.get("state", "open") == "open" and not p["merged"] and set(p["closes"]) & ms_numbers]
    stale = [p for p in open_prs if (datetime.now(timezone.utc) - p["created_at"]).days > 3]

    base = baseline_weight or total_w
    signals = {
        "schedule_slip": clamp01(1 - index) * conf,
        "projected_late": projected_late * conf,
        "stale_prs": len(stale) / max(len(open_prs), 1) if open_prs else 0.0,
        "reopen_rate": 0.0,  # needs the issue-events API; add in a later phase
        "scope_growth": clamp01((total_w - base) / base),   # growth vs the first scope we ever saw
        "overdue_open": (len(open_issues) / len(issues)) if today > end and open_issues else 0.0,
    }
    risk = delay_risk(signals)
    return {
        "milestone": milestone["name"], "empty": False,
        "pct_done": round(pct_done * 100, 1), "pv": round(pv, 3), "spi": round(index, 2),
        "risk_score": risk["score"], "status": risk["status"], "reasons": risk["top_reasons"],
        "remaining_days_est": round(remaining_days, 1),
        "summary": template_summary(milestone["name"], pct_done * 100, risk, conf),
        "confidence": round(conf, 2),
        # extra numbers so the dashboard can draw burnup / risk-breakdown charts
        "total_weight": total_w, "done_weight": done_w, "planned_weight": round(pv * total_w, 1),
        "done_count": len(issues) - len(open_issues), "open_count": len(open_issues),
        "days_left": days_left, "contributions": risk["parts"],
        "start_date": start.isoformat(), "due_date": end.isoformat(),
    }


def main():
    from supabase import create_client  # imported here so tests do not need the package

    config = json.load(open(os.path.join(os.path.dirname(__file__), "project.json")))
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    today = date.today()           # NOTE: the runner uses the UTC date (05:30 IST = start of a new tool-day)
    per_week = int(config.get('work_days_per_week', 5))

    for repo in config["repos"]:
        owner, name = repo.split("/")
        data = fetch_repo(owner, name)

        cc = cocomo_basic(config["kloc"], config.get("cocomo_mode", "organic"))
        sb.table("project_reports").upsert({
            "repo": repo, "report_date": today.isoformat(),
            "cocomo_effort_pm": round(cc["effort_pm"], 1), "cocomo_months": round(cc["months"], 1),
            "cocomo_team": round(cc["avg_team"], 1),
            "start_date": min(m["start"] for m in config["milestones"]),
            "due_date": config.get("project_end") or max(m["end"] for m in config["milestones"]),
        }, on_conflict="repo,report_date").execute()

        for m in config["milestones"]:
            first = (sb.table("milestone_reports").select("report_date,total_weight").eq("repo", repo)
                     .eq("milestone", m["name"]).order("report_date").execute().data)
            base = next((float(r["total_weight"]) for r in first if r["total_weight"] is not None), None)
            rep = compute_milestone_report(data, m, today, base, per_week)
            if rep["empty"]:
                continue
            sb.table("milestone_reports").upsert({
                "repo": repo, "milestone": rep["milestone"], "report_date": today.isoformat(),
                "pct_done": rep["pct_done"], "pv": rep["pv"], "spi": rep["spi"],
                "risk_score": rep["risk_score"], "status": rep["status"], "reasons": rep["reasons"],
                "remaining_days_est": rep["remaining_days_est"], "summary": rep["summary"],
                "total_weight": rep["total_weight"], "done_weight": rep["done_weight"],
                "planned_weight": rep["planned_weight"], "done_count": rep["done_count"],
                "open_count": rep["open_count"], "days_left": rep["days_left"],
                "contributions": rep["contributions"],
                "start_date": rep["start_date"], "due_date": rep["due_date"],
            }, on_conflict="repo,milestone,report_date").execute()
            print(f"{repo} / {rep['milestone']}: {rep['pct_done']}% done, SPI {rep['spi']}, risk {rep['risk_score']} ({rep['status']}), confidence {rep['confidence']}")

        for w in compute_workload(data):
            sb.table("workload").upsert({"repo": repo, "report_date": today.isoformat(), **w},
                                        on_conflict="repo,developer,report_date").execute()
        print(f"{repo}: workload saved for {len(compute_workload(data))} developer(s)")


if __name__ == "__main__":
    sys.exit(main())
