from datetime import date, datetime, timedelta, timezone
import run_job
from planning import add_workdays, critical_path, waiting_on, project_rollup

NOW = datetime.now(timezone.utc)
START = date.today() - timedelta(days=4)                       # "today" is day 4 of an 8-day milestone
MS = {"name": "v2 Payments", "start": START.isoformat(), "end": (START + timedelta(days=8)).isoformat(), "test_days": 2}
SIZES = {7: "M", 8: "M", 9: "L", 10: "S", 11: "L", 12: "S"}
DEPS = {8: [7], 9: [8], 11: [8, 9, 10], 12: [11]}


def payments(open_prs=(7, 8)):
    issues = [{"number": n, "title": f"task {n}", "state": "open", "labels": [f"size:{s}"], "milestone": "v2 Payments",
               "assignee": "meena", "created_at": NOW - timedelta(days=4), "closed_at": None, "blocked_by": DEPS.get(n, [])}
              for n, s in SIZES.items()]
    prs = [{"number": 100 + n, "title": "x", "author": "meena", "state": "open", "merged": False, "merged_at": None,
            "created_at": NOW - timedelta(days=4), "closes": [n]} for n in open_prs]
    return {"issues": issues, "prs": prs}


def test_critical_path_and_cycles():
    tasks = {1: {"days": 2, "blocked_by": []}, 2: {"days": 3, "blocked_by": [1]}, 3: {"days": 1, "blocked_by": [1]}}
    assert critical_path(tasks) == (5.0, [1, 2])
    assert critical_path({1: {"days": 1, "blocked_by": [2]}, 2: {"days": 1, "blocked_by": [1]}}) == (None, [])
    assert waiting_on(1, tasks) == [2, 3]


def test_add_workdays_skips_weekends():
    assert add_workdays(date(2026, 10, 23), 1, 5) == date(2026, 10, 26)      # Fri + 1 working day = Mon
    assert add_workdays(date(2026, 10, 23), 1, 7) == date(2026, 10, 24)


def test_payments_chain_is_stuck_behind_its_first_task():
    r = run_job.compute_milestone_report(payments(), MS, date.today(), per_week=7, idle="ravi")
    assert r["critical_path"] == [7, 8, 9, 11, 12] and r["critical_days"] == 17.0
    assert r["blocked_count"] == 4                                           # #8, #9, #11, #12
    assert r["contributions"]["critical_delay"] == 15.0                      # head PR open > 3 days
    assert r["contributions"]["testing_squeeze"] > 0 and r["slip_days"] > 0
    assert r["status"] == "red" and r["forecast_date"] > r["due_date"]
    node = {n["n"]: n for n in r["graph"]["nodes"]}
    assert node[7]["critical"] and node[8]["state"] == "blocked" and node[10]["state"] == "open"
    assert [7, 8] in r["graph"]["edges"]


def test_findings_are_specific_and_public_ones_have_no_names():
    r = run_job.compute_milestone_report(payments(), MS, date.today(), per_week=7, idle="ravi")
    first_public, first_lead = r["findings"][0], r["findings_lead"][0]
    assert "#107" in first_public["text"] and "holds up 4" in first_public["text"]
    assert "meena" in first_lead["text"]
    public_text = " ".join(f["text"] + " " + f["action"] for f in r["findings"])
    assert "meena" not in public_text and "ravi" not in public_text
    assert any("ravi has no open tasks and could take #10" in f["action"] for f in r["findings_lead"])
    assert any("testing" in f["text"] for f in r["findings"])


def test_finished_milestone_has_no_forecast_slip():
    d = payments()
    for i in d["issues"]:
        i["state"], i["closed_at"] = "closed", NOW
    d["prs"] = [{"number": 100 + n, "title": "x", "author": "m", "state": "closed", "merged": True, "merged_at": NOW,
                 "created_at": NOW - timedelta(days=1), "closes": [n]} for n in SIZES]
    r = run_job.compute_milestone_report(d, MS, date.today(), per_week=7)
    assert r["pct_done"] == 100.0 and r["slip_days"] == 0 and r["forecast_date"] == date.today().isoformat()


def test_project_rollup_with_budget():
    reps = [{"total_weight": 10, "done_weight": 5, "planned_weight": 5, "slip_days": 0.0},
            {"total_weight": 10, "done_weight": 1, "planned_weight": 5, "slip_days": 5.0}]
    cfg = {"kloc": 20, "budget": 100000, "project_end": "2026-10-23", "milestones": [{"start": START.isoformat(), "end": "2026-10-23"}],
           "team": [{"github": "a", "daily_rate": 1000}, {"github": "b", "daily_rate": 1000, "availability": 0.5}]}
    out = project_rollup(reps, cfg, date.today(), 7)
    assert out["ev_pct"] == 30.0 and out["pv_pct"] == 50.0 and out["spi"] == 0.6 and out["forecast_date"] == "2026-10-28"
    assert out["ac"] == 1500 * 4 and out["cpi"] == round(30000 / 6000, 2) and out["eac"] == 20000


def test_dev_tasks_status_blockers_and_days():
    d = payments()
    d["issues"][3]["state"] = "closed"; d["issues"][3]["closed_at"] = NOW              # #10 (tax) merged
    d["prs"].append({"number": 110, "title": "x", "author": "meena", "state": "closed", "merged": True, "merged_at": NOW,
                     "created_at": NOW - timedelta(days=3), "closes": [10]})
    rows = {r["number"]: r for r in run_job.compute_dev_tasks(d, ["v2 Payments"], date.today(), 7)}
    assert rows[7]["status"] == "in_review" and rows[7]["days_open"] == 4              # PR open for 4 days, no blockers
    assert rows[8]["status"] == "blocked" and rows[8]["blocked_by"] == [{"n": 7, "title": "task 7"}]
    assert rows[10]["status"] == "done" and rows[10]["days_taken"] == 3                # PR opened 3 days before merge
    assert rows[9]["pr_number"] is None and rows[9]["days_open"] is None               # not started
    assert rows[11]["blocked_by"] == [{"n": 8, "title": "task 8"}, {"n": 9, "title": "task 9"}]   # #10 is done, so it no longer blocks


def test_working_days_are_reported():
    r = run_job.compute_milestone_report(payments(), MS, date.today(), per_week=7)
    assert r["work_days_total"] == 8 and r["work_days_elapsed"] == 4 and r["days_left"] == 4
    reps = [{"total_weight": 10, "done_weight": 5, "planned_weight": 5, "slip_days": 0.0}]
    cfg = {"milestones": [{"start": START.isoformat(), "end": (START + timedelta(days=8)).isoformat()}], "project_end": (START + timedelta(days=8)).isoformat()}
    out = project_rollup(reps, cfg, date.today(), 7)
    assert out["work_days_total"] == 8 and out["work_days_elapsed"] == 4
