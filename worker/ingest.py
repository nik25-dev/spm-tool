"""Fetch issues + PRs from GitHub and normalise them. Works with a read-only token, or unauthenticated
on a public repo (60 requests/hour) for quick testing."""
import os
import re
from datetime import datetime

import requests

API = "https://api.github.com"
CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)


def _headers():
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("GH_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get_all(path: str, params: dict | None = None, max_pages: int = 5) -> list[dict]:
    out, url, params = [], f"{API}{path}", {**(params or {}), "per_page": 100}
    for _ in range(max_pages):
        r = requests.get(url, headers=_headers(), params=params, timeout=30)
        r.raise_for_status()
        out.extend(r.json())
        url = r.links.get("next", {}).get("url")
        if not url:
            break
        params = None  # the 'next' url already carries the query string
    return out


TEXT_DEP = re.compile(r"\b(?:blocked by|depends on)\s+#(\d+)", re.I)


def fetch_dependencies(owner: str, repo: str, numbers: list) -> dict:
    """GitHub's native 'blocked by' relationships, one request per issue. Returns {issue: [blocker numbers]}.
    If the endpoint is unavailable for this token, returns {} and the caller falls back to 'Blocked by #N' in the text."""
    out = {}
    for n in numbers:
        r = requests.get(f"{API}/repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by",
                         headers=_headers(), params={"per_page": 100}, timeout=30)
        if r.status_code >= 400:
            print(f"note: dependencies API returned {r.status_code} for #{n}; using text fallback only")
            return {}
        r.raise_for_status()
        out[n] = [b["number"] for b in r.json()]
    return out


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def fetch_repo(owner: str, repo: str, milestones: list | None = None) -> dict:
    issues_raw = _get_all(f"/repos/{owner}/{repo}/issues", {"state": "all"})
    prs_raw = _get_all(f"/repos/{owner}/{repo}/pulls", {"state": "all"})

    # the /issues endpoint also returns PRs - keep real issues only
    issues = [{
        "number": i["number"], "title": i["title"], "state": i["state"],
        "labels": [l["name"] for l in i["labels"]],
        "milestone": (i.get("milestone") or {}).get("title"),
        "assignee": (i.get("assignee") or {}).get("login"), "body": i.get("body") or "",
        "created_at": _dt(i["created_at"]), "closed_at": _dt(i["closed_at"]),
    } for i in issues_raw if "pull_request" not in i]

    prs = [{
        "number": p["number"], "title": p["title"], "author": p["user"]["login"], "state": p["state"],
        "merged": bool(p.get("merged_at")), "merged_at": _dt(p.get("merged_at")),
        "created_at": _dt(p["created_at"]),
        # PRs that say "Closes #12" in title/body link to those issues
        "closes": sorted({int(n) for n in CLOSES.findall((p["title"] or "") + " " + (p["body"] or ""))}),
    } for p in prs_raw]
    # dependencies: native relationships for OPEN issues in the tracked milestones, plus 'Blocked by #N' written in the text
    wanted = [x["number"] for x in issues if x["state"] == "open" and (milestones is None or x["milestone"] in milestones)]
    native = fetch_dependencies(owner, repo, wanted) if wanted else {}
    for x in issues:
        text_deps = [int(n) for n in TEXT_DEP.findall(x["body"])]
        x["blocked_by"] = sorted(set(native.get(x["number"], [])) | set(text_deps))
    return {"issues": issues, "prs": prs}


def issues_closed_via_merged_pr(data: dict) -> set[int]:
    """Our anti-gaming rule: an issue only counts as DONE if a merged PR references it."""
    return {n for p in data["prs"] if p["merged"] for n in p["closes"]}


if __name__ == "__main__":
    import sys
    owner, repo = (sys.argv[1], sys.argv[2]) if len(sys.argv) == 3 else ("octocat", "Hello-World")
    d = fetch_repo(owner, repo)
    done = issues_closed_via_merged_pr(d)
    print(f"{owner}/{repo}: {len(d['issues'])} issues, {len(d['prs'])} PRs, {len(done)} issues closed via merged PR")
