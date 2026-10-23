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
from planning import add_workdays, build_findings, critical_path, project_rollup, waiting_on

SIZE_POINTS = {"S": 1, "M": 3, "L": 5}
DEFAULT_DAYS = {"S": 1.0, "M": 3.0, "L": 5.0}   # used until we have real closed issues (cold start)


def size_of(issue: dict) -> str:
    for label in issue["labels"]:
        if label.lower().startswith("size:") and label[5:].strip().upper() in SIZE_POINTS:
            return label[5:].strip().upper()
    return "M"


def estimate_days(data: dict, defaults: dict | None = None, k: int = 3) -> dict:
    """Days per task size, learned from how long PRs took (opened -> merged), NOT from how long the issue sat in the
    backlog. Starts at the team's size_days and moves towards the observed median as tasks finish:
    estimate = (n * median + k * default) / (n + k)."""
    merged = {}
    for p in data["prs"]:
        if p["merged"] and p["merged_at"]:
            for n in p["closes"]:
                merged.setdefault(n, p)
    lead = {"S": [], "M": [], "L": []}
    for i in data["issues"]:
        p = merged.get(i["number"])
        if p:
            lead[size_of(i)].append(max((p["merged_at"] - p["created_at"]).total_seconds() / 86400, 0.0))
    base = {**DEFAULT_DAYS, **(defaults or {})}
    return {s: ((len(v) * median(v) + k * base[s]) / (len(v) + k) if v else base[s]) for s, v in lead.items()}


def compute_workload(data: dict, size_days: dict | None = None) -> list[dict]:
    """Per developer: open issues, done issues (merged PR), and estimated days of work left."""
    done, est, out = issues_closed_via_merged_pr(data), estimate_days(data, size_days), {}
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


def compute_dev_tasks(data: dict, names: list, today: date, per_week: int = 5, size_days: dict | None = None) -> list:
    """One row per task in the tracked milestones: status, what blocks it, and how many WORKING days it took (PR opened -> merged).
    GitHub records no work hours, and we deliberately do not analyse commit times, so we count working days only."""
    done, est, now = issues_closed_via_merged_pr(data), estimate_days(data, size_days), datetime.now(timezone.utc)
    titles = {i["number"]: i["title"] for i in data["issues"]}
    open_nums = {i["number"] for i in data["issues"] if i["number"] not in done}
    pr_for = {}
    for p in data["prs"]:
        for n in p["closes"]:
            if n not in pr_for or (p["merged"] and not pr_for[n]["merged"]):
                pr_for[n] = p
    out = []
    for i in data["issues"]:
        if i["milestone"] not in names or not i["assignee"]:
            continue
        n, pr = i["number"], pr_for.get(i["number"])
        blockers = [{"n": b, "title": titles.get(b, "")} for b in (i.get("blocked_by") or []) if b in open_nums]
        started = pr["created_at"] if pr else None
        row = {"number": n, "title": i["title"], "size": size_of(i), "milestone": i["milestone"], "developer": i["assignee"],
               "blocked_by": blockers, "pr_number": pr["number"] if pr else None,
               "started": started.date().isoformat() if started else None, "finished": None,
               "days_taken": None, "days_open": None, "est_days": round(est[size_of(i)], 2)}
        if n in done:
            end = pr["merged_at"] if pr and pr["merged"] else i["closed_at"]
            row.update(status="done", finished=end.date().isoformat(),
                       days_taken=workdays_between((started or i["created_at"]).date(), end.date(), per_week))
        else:
            row["status"] = "blocked" if blockers else ("in_review" if pr and pr.get("state", "open") == "open" else "todo")
            if started:
                row["days_open"] = workdays_between(started.date(), today, per_week)
        out.append(row)
    return out


def compute_milestone_report(data: dict, milestone: dict, today: date, baseline_weight: float | None = None,
                             per_week: int = 5, team_cfg: dict | None = None, idle: str | None = None,
                             baseline_date: date | None = None, size_days: dict | None = None) -> dict:
    team_cfg = team_cfg or {}
    start, end = date.fromisoformat(milestone["start"]), date.fromisoformat(milestone["end"])
    issues = [i for i in data["issues"] if i["milestone"] == milestone["name"]]
    if not issues:
        return {"milestone": milestone["name"], "empty": True}

    done = issues_closed_via_merged_pr(data)
    by_num = {i["number"]: i for i in issues}
    weight = {n: SIZE_POINTS[size_of(i)] for n, i in by_num.items()}
    total_w = sum(weight.values())
    done_w = sum(w for n, w in weight.items() if n in done)
    pct_done = done_w / total_w

    pv = planned_value(today, start, end, per_week)
    conf = confidence(pv)                     # 0 at the very start, 1 once 25% of the milestone's time has passed
    index = spi(pct_done, pv)

    # ---- remaining work, dependencies, critical path
    est = estimate_days(data, size_days)
    open_issues = [i for i in issues if i["number"] not in done]
    open_nums = {i["number"] for i in open_issues}
    days_of = {i["number"]: est[size_of(i)] for i in open_issues}
    needs = sum(days_of.values())                                   # person-days, one task at a time
    blockers = {n: [b for b in (by_num[n].get("blocked_by") or []) if b in open_nums] for n in open_nums}
    tasks = {n: {"days": days_of[n], "blocked_by": blockers[n]} for n in open_nums}
    crit_len, chain = critical_path(tasks)
    if crit_len is None:                                            # circular dependencies: ignore them
        tasks = {n: {"days": days_of[n], "blocked_by": []} for n in open_nums}
        crit_len, chain = critical_path(tasks)
    blocked_nums = [n for n, b in blockers.items() if b] if chain is not None else []

    # ---- capacity and forecast (working days)
    devs = sorted({i["assignee"] for i in issues if i["assignee"]})
    team = max(sum(float(team_cfg.get(d, {}).get("availability", 1.0)) for d in devs), 1.0)
    days_left = workdays_between(today, end, per_week)
    planned_days = workdays_between(start, end, per_week)
    elapsed = workdays_between(start, min(today, end), per_week) if today > start else 0
    if open_issues:
        parts = {"capacity": needs / team, "critical": crit_len or 0.0}
        if conf >= 1.0:
            parts["evm"] = max(0.0, planned_days / max(index, 0.25) - elapsed)  # classic: planned duration / SPI, SPI floored at 0.25
        forecast_days = max(parts.values())
        driver = max(parts, key=parts.get)
    else:
        forecast_days, driver = 0.0, None
    forecast = add_workdays(today, forecast_days, per_week)
    slip = max(0.0, forecast_days - days_left)
    projected_late = clamp01(slip / max(days_left, 1)) if open_issues else 0.0

    # ---- PRs tied to this milestone
    now = datetime.now(timezone.utc)
    pr_for = {}
    for p in data["prs"]:
        if p.get("state", "open") == "open" and not p["merged"]:
            for n in p["closes"]:
                if n in by_num:
                    pr_for.setdefault(n, p)
    ms_prs = {p["number"]: p for p in pr_for.values()}
    stale = [{"number": p["number"], "author": p["author"], "age": (now - p["created_at"]).total_seconds() / 86400}
             for p in ms_prs.values() if (now - p["created_at"]).days > 3]

    # ---- critical-path head
    head = None
    if chain:
        h = chain[0]
        p = pr_for.get(h)
        head = {"number": h, "title": by_num[h]["title"], "assignee": by_num[h]["assignee"],
                "pr": {"number": p["number"], "age": (now - p["created_at"]).total_seconds() / 86400, "author": p["author"]} if p else None,
                "unblocks": waiting_on(h, tasks)}
    if head and head["pr"]:
        critical_delay = 1.0 if head["pr"]["age"] > 3 else 0.0
    elif head:
        critical_delay = clamp01((pv - 0.5) / 0.25)                 # an unstarted head only matters once half the time is gone
    else:
        critical_delay = 0.0

    # ---- testing window
    test_days = int(milestone.get("test_days", 0))
    test_left = workdays_between(forecast, end, per_week) if forecast <= end else 0
    testing = clamp01((test_days - test_left) / test_days) if test_days > 0 and open_issues else 0.0

    baseline = baseline_weight or total_w
    signals = {
        "schedule_slip": clamp01(1 - index) * conf,
        "projected_late": projected_late * conf,
        "critical_delay": critical_delay,
        "blocked_work": (len(blocked_nums) / len(open_issues) * conf) if open_issues else 0.0,
        "stale_prs": len(stale) / max(len(ms_prs), 1) if ms_prs else 0.0,
        "scope_growth": clamp01((total_w - baseline) / baseline),
        "testing_squeeze": testing * conf,
        "overdue_open": (len(open_issues) / len(issues)) if today > end and open_issues else 0.0,
    }
    risk = delay_risk(signals)

    # ---- specific findings (public: no names; lead: with names)
    busiest = max(devs, key=lambda d: sum(days_of[n] for n in open_nums if by_num[n]["assignee"] == d), default=None)
    cands = [n for n in open_nums if by_num[n]["assignee"] == busiest and not blockers[n] and n not in pr_for]
    cand = None
    if cands:
        c = max(cands, key=lambda n: days_of[n])
        cand = {"number": c, "title": by_num[c]["title"], "from": busiest}
    added = [{"number": n, "title": by_num[n]["title"]} for n in sorted(by_num)
             if baseline_date and by_num[n]["created_at"].date() > baseline_date]
    pub, lead = build_findings({
        "milestone": milestone["name"], "pv": pv, "driver": driver, "head": head, "slip": slip, "forecast": forecast, "due": end,
        "candidate": cand, "idle": idle if idle and idle != busiest else None,
        "test_days": test_days, "test_left": test_left, "stale": stale, "scope_added": added,
        "scope_pct": max(0.0, (total_w - baseline) / baseline * 100),
        "overdue": len(open_issues) if today > end else 0,
    })

    graph = {"nodes": [{"n": n, "title": by_num[n]["title"][:40], "size": size_of(by_num[n]),
                        "state": "done" if n in done else ("blocked" if n in blocked_nums else "open"),
                        "critical": n in (chain or [])} for n in sorted(by_num)],
             "edges": [[b, n] for n in sorted(by_num) for b in (by_num[n].get("blocked_by") or []) if b in by_num]}

    return {
        "milestone": milestone["name"], "empty": False,
        "pct_done": round(pct_done * 100, 1), "pv": round(pv, 3), "spi": round(index, 2),
        "risk_score": risk["score"], "status": risk["status"], "reasons": risk["top_reasons"],
        "remaining_days_est": round(needs, 1),
        "summary": template_summary(milestone["name"], pct_done * 100, risk, conf),
        "confidence": round(conf, 2),
        "total_weight": total_w, "done_weight": done_w, "planned_weight": round(pv * total_w, 1),
        "done_count": len(issues) - len(open_issues), "open_count": len(open_issues),
        "days_left": days_left, "contributions": risk["parts"],
        "start_date": start.isoformat(), "due_date": end.isoformat(),
        "work_days_total": planned_days, "work_days_elapsed": elapsed,
        "capacity_person_days": round(team * days_left, 1), "forecast_date": forecast.isoformat(),
        "slip_days": round(slip, 1), "blocked_count": len(blocked_nums), "critical_days": round(crit_len or 0, 1),
        "critical_path": chain or [], "test_days": test_days, "test_days_left": test_left,
        "graph": graph, "findings": pub, "findings_lead": lead,
    }


def main():
    from supabase import create_client  # imported here so tests do not need the package

    config = json.load(open(os.path.join(os.path.dirname(__file__), "project.json")))
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    today = date.today()           # NOTE: the runner uses the UTC date (05:30 IST = start of a new tool-day)
    per_week = int(config.get("work_days_per_week", 5))
    team_cfg = {t["github"]: t for t in config.get("team", [])}
    names = [m["name"] for m in config["milestones"]]
    size_days = config.get("size_days")

    for repo in config["repos"]:
        owner, name = repo.split("/")
        data = fetch_repo(owner, name, names)
        workload = compute_workload(data, size_days)
        busy = {w["developer"]: w["open_issues"] for w in workload}
        idle = next((d for d in list(team_cfg) + list(busy) if busy.get(d, 0) == 0), None)

        reports = []
        for m in config["milestones"]:
            first = (sb.table("milestone_reports").select("report_date,total_weight").eq("repo", repo)
                     .eq("milestone", m["name"]).order("report_date").execute().data)
            row0 = next((r for r in first if r["total_weight"] is not None), None)
            base = float(row0["total_weight"]) if row0 else None
            bdate = date.fromisoformat(row0["report_date"]) if row0 else None
            rep = compute_milestone_report(data, m, today, base, per_week, team_cfg, idle, bdate, size_days)
            if rep["empty"]:
                continue
            reports.append(rep)
            sb.table("milestone_reports").upsert({
                "repo": repo, "milestone": rep["milestone"], "report_date": today.isoformat(),
                **{k: rep[k] for k in ("pct_done", "pv", "spi", "risk_score", "status", "reasons", "remaining_days_est",
                                       "summary", "total_weight", "done_weight", "planned_weight", "done_count", "open_count",
                                       "days_left", "contributions", "start_date", "due_date", "work_days_total", "work_days_elapsed", "capacity_person_days",
                                       "forecast_date", "slip_days", "blocked_count", "critical_days", "critical_path",
                                       "test_days", "test_days_left", "graph")},
                "findings": rep["findings"],
            }, on_conflict="repo,milestone,report_date").execute()
            sb.table("lead_notes").upsert({"repo": repo, "milestone": rep["milestone"], "report_date": today.isoformat(),
                                           "findings": rep["findings_lead"]}, on_conflict="repo,milestone,report_date").execute()
            print(f"{repo} / {rep['milestone']}: {rep['pct_done']}% done, SPI {rep['spi']}, risk {rep['risk_score']} "
                  f"({rep['status']}), forecast {rep['forecast_date']} vs due {rep['due_date']}, blocked {rep['blocked_count']}")

        cc = cocomo_basic(config["kloc"], config.get("cocomo_mode", "organic"))
        roll = project_rollup(reports, config, today, per_week) if reports else {}
        sb.table("project_reports").upsert({
            "repo": repo, "report_date": today.isoformat(),
            "cocomo_effort_pm": round(cc["effort_pm"], 1), "cocomo_months": round(cc["months"], 1),
            "cocomo_team": round(cc["avg_team"], 1),
            "start_date": min(m["start"] for m in config["milestones"]),
            "due_date": config.get("project_end") or max(m["end"] for m in config["milestones"]),
            **roll,
        }, on_conflict="repo,report_date").execute()

        for w in workload:
            sb.table("workload").upsert({"repo": repo, "report_date": today.isoformat(), **w},
                                        on_conflict="repo,developer,report_date").execute()
        tasks = compute_dev_tasks(data, names, today, per_week, size_days)
        if tasks:
            sb.table("dev_tasks").upsert([{"repo": repo, "report_date": today.isoformat(), **t} for t in tasks],
                                         on_conflict="repo,report_date,number").execute()
        print(f"{repo}: workload saved for {len(workload)} developer(s), {len(tasks)} task rows")


if __name__ == "__main__":
    sys.exit(main())
