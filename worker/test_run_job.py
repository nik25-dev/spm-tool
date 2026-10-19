from datetime import date, datetime, timedelta, timezone
import run_job

NOW = datetime.now(timezone.utc)


def mk_issue(n, size, created_days_ago, closed_days_ago=None, milestone="v1"):
    return {"number": n, "title": f"t{n}", "state": "closed" if closed_days_ago is not None else "open",
            "labels": [f"size:{size}"], "milestone": milestone, "assignee": "asha",
            "created_at": NOW - timedelta(days=created_days_ago),
            "closed_at": (NOW - timedelta(days=closed_days_ago)) if closed_days_ago is not None else None}


def mk_pr(n, closes, merged):
    return {"number": 100 + n, "title": "x", "author": "asha", "merged": merged,
            "merged_at": NOW if merged else None, "created_at": NOW - timedelta(days=6), "closes": closes}


def scenario(done_ids):
    issues = [mk_issue(i, "M", 20, 2 if i in done_ids else None) for i in range(1, 11)]
    prs = [mk_pr(i, [i], True) for i in done_ids] + [mk_pr(50, [9], False)]
    return {"issues": issues, "prs": prs}


M = {"name": "v1", "start": (date.today() - timedelta(days=20)).isoformat(), "end": (date.today() + timedelta(days=10)).isoformat()}


def test_healthy_vs_delayed():
    good = run_job.compute_milestone_report(scenario({1, 2, 3, 4, 5, 6, 7}), M, date.today())
    bad = run_job.compute_milestone_report(scenario({1}), M, date.today())
    assert good["pct_done"] == 70.0 and bad["pct_done"] == 10.0
    assert bad["risk_score"] > good["risk_score"]
    assert bad["status"] in ("yellow", "red") and bad["spi"] < good["spi"]
    assert "schedule_slip" in bad["reasons"]


def test_unmerged_pr_does_not_count_as_done():
    d = scenario({1})                      # PR 150 references issue 9 but is NOT merged
    r = run_job.compute_milestone_report(d, M, date.today())
    assert r["pct_done"] == 10.0


def test_empty_milestone():
    assert run_job.compute_milestone_report({"issues": [], "prs": []}, M, date.today())["empty"] is True


def test_stale_prs_are_scoped_to_their_own_milestone():
    # Payments-only stale PRs must not raise risk on the healthy Auth milestone
    issues = [mk_issue(i, "M", 20, 2, milestone="v1") for i in range(1, 5)] + \
             [mk_issue(i, "M", 20, None, milestone="v2") for i in range(5, 9)]
    stale = [dict(mk_pr(i, [i], False), state="open") for i in (5, 6)]
    prs = [mk_pr(i, [i], True) for i in range(1, 5)] + stale
    d = {"issues": issues, "prs": prs}
    v1 = run_job.compute_milestone_report(d, dict(M, name="v1"), date.today())
    v2 = run_job.compute_milestone_report(d, dict(M, name="v2"), date.today())
    assert "stale_prs" not in v1["reasons"] and "stale_prs" in v2["reasons"]
