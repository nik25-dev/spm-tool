from unittest.mock import patch, MagicMock
import ingest


def fake_get(url, headers=None, params=None, timeout=None):
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.links = {}
    if url.endswith("/issues"):
        r.json.return_value = [
            {"number": 1, "title": "Login page", "state": "closed", "labels": [{"name": "size:M"}],
             "milestone": {"title": "v1"}, "assignee": {"login": "asha"},
             "created_at": "2026-10-01T09:00:00Z", "closed_at": "2026-10-05T09:00:00Z"},
            {"number": 2, "title": "Payments", "state": "open", "labels": [], "milestone": None, "assignee": None,
             "created_at": "2026-10-02T09:00:00Z", "closed_at": None},
            {"number": 3, "title": "A PR listed as issue", "state": "closed", "labels": [], "pull_request": {},
             "created_at": "2026-10-03T09:00:00Z", "closed_at": None},
        ]
    else:
        r.json.return_value = [
            {"number": 3, "title": "Add login. Closes #1", "body": "", "state": "closed", "user": {"login": "asha"},
             "merged_at": "2026-10-05T08:00:00Z", "created_at": "2026-10-03T09:00:00Z"},
            {"number": 4, "title": "WIP payments", "body": "fixes #2", "state": "open", "user": {"login": "ravi"},
             "merged_at": None, "created_at": "2026-10-06T09:00:00Z"},
        ]
    return r


def test_fetch_and_link():
    with patch("ingest.requests.get", side_effect=fake_get):
        d = ingest.fetch_repo("o", "r")
    assert [i["number"] for i in d["issues"]] == [1, 2]           # PR filtered out of issues
    assert d["issues"][0]["labels"] == ["size:M"] and d["issues"][0]["milestone"] == "v1"
    assert d["prs"][0]["closes"] == [1] and d["prs"][1]["closes"] == [2]
    assert ingest.issues_closed_via_merged_pr(d) == {1}             # PR 4 is unmerged -> issue 2 NOT done
