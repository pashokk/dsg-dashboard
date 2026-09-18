#!/usr/bin/env python3
"""
Pulls design-ops metrics for the DSG Jira project and writes docs/data.json,
which the static dashboard (docs/index.html) reads.

Run locally:
    export JIRA_BASE_URL="https://appodeal.atlassian.net"
    export JIRA_EMAIL="pavel.savinskiy@appodeal.com"
    export JIRA_API_TOKEN="..."          # https://id.atlassian.com/manage-profile/security/api-tokens
    export JIRA_PROJECT_KEY="DSG"        # optional, defaults to DSG
    python scripts/fetch_data.py

In production (Coolify) the same variables are set as the app's Environment
Variables, and this script runs on a schedule via Coolify's Scheduled Tasks
feature, writing straight into the served docs/ folder — no separate CI system
needed.
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta

BASE_URL = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("JIRA_EMAIL", "")
TOKEN = os.environ.get("JIRA_API_TOKEN", "")
PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "DSG")

# How old a never-started ticket has to be to count as "stale backlog".
STALE_DAYS = 180
# Window used for the recent-turnaround and recent-throughput numbers.
RECENT_DAYS = 90
# Number of weekly buckets in the opened-vs-completed trend chart.
TREND_WEEKS = 12

# Backlog isn't used by this team (new tickets start in "To Do"), so it's
# excluded everywhere: counts, workload, stale detection, the trend chart.
EXCLUDED_STATUSES = ["Backlog"]

# People whose tickets shouldn't appear anywhere on this dashboard (not open
# counts, not the trend chart, nothing) — e.g. people outside the core team
# who show up as an assignee on a handful of tickets.
EXCLUDED_ASSIGNEES = ["Alexander Pleshkan", "Marc Llobet Rodríguez"]

# Heuristic tags for "what kind of request is this" — checked in order,
# first match wins. Matched against the ticket summary + labels, lowercased.
# Short/ambiguous keywords use word boundaries so e.g. "ui" doesn't match
# "build"; adjust freely, this is just keyword matching, not ML.
TAG_RULES = [
    ("UI / Dashboard", ["dashboard", r"\bui\b", r"\bux\b", "interface", "design system", "component"]),
    ("Presentations / Decks", ["presentation", "deck", "one-pager", "1-pager", "onepager", "media kit", "slide"]),
    ("Website / Landing", ["website", "landing", "webpage", r"\bweb\b", r"\bsite\b"]),
    ("Illustration / Graphics", ["illustration", "icon", "graphic", "artwork", "banner", "asset"]),
    ("Branding / Print", ["logo", "brand", "print", "poster", "brochure", "packaging"]),
    ("Video / Motion", ["video", "animation", "motion", "gif", "reel"]),
]


def classify_type(summary, labels):
    text = (summary or "").lower() + " " + " ".join(labels or []).lower()
    for tag, patterns in TAG_RULES:
        for pat in patterns:
            if pat.startswith(r"\b"):
                if re.search(pat, text):
                    return tag
            elif pat in text:
                return tag
    return "Other"

# Override with DATA_OUTPUT_PATH inside the container (Coolify sets this to
# the nginx-served docs/ folder); defaults to the repo's own docs/ for local runs.
OUTPUT_PATH = os.environ.get(
    "DATA_OUTPUT_PATH",
    os.path.join(os.path.dirname(__file__), "..", "docs", "data.json"),
)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def auth_header():
    if not (EMAIL and TOKEN):
        die("Set JIRA_EMAIL and JIRA_API_TOKEN (and JIRA_BASE_URL) as environment variables.")
    raw = f"{EMAIL}:{TOKEN}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")


def jira_get(path, params=None):
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": auth_header(),
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        die(f"Jira API error {e.code} on {path}: {e.read().decode('utf-8', 'ignore')[:500]}")
    except urllib.error.URLError as e:
        die(f"Could not reach {BASE_URL}: {e}")


def search_all_issues(jql, fields, expand=None, batch_size=100):
    """Paginate through /rest/api/3/search using nextPageToken (new Jira Cloud search)."""
    issues = []
    next_token = None
    while True:
        body = {
            "jql": jql,
            "maxResults": batch_size,
            "fields": fields,
        }
        if expand:
            body["expand"] = expand
        if next_token:
            body["nextPageToken"] = next_token

        url = f"{BASE_URL}/rest/api/3/search/jql"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": auth_header(),
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            die(f"Jira search error {e.code}: {e.read().decode('utf-8', 'ignore')[:500]}")

        issues.extend(data.get("issues", []))
        next_token = data.get("nextPageToken")
        if not next_token or data.get("isLast"):
            break
        time.sleep(0.2)  # be polite to the API
    return issues


def parse_dt(s):
    if not s:
        return None
    # Jira timestamps look like 2026-09-14T15:21:33.687+0000
    return datetime.strptime(s[:19] + s[23:], "%Y-%m-%dT%H:%M:%S%z") if len(s) > 19 else None


def parse_dt_safe(s):
    if not s:
        return None
    try:
        # normalise "+0000" / "+03:00" style offsets
        s2 = s.replace("Z", "+0000")
        return datetime.strptime(s2, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        try:
            return datetime.strptime(s2, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None


def main():
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=STALE_DAYS)
    recent_cutoff = now - timedelta(days=RECENT_DAYS)

    excluded_clause = "".join(f' AND status != "{s}"' for s in EXCLUDED_STATUSES)

    open_fields = ["summary", "status", "assignee", "created", "priority", "labels"]
    open_issues = search_all_issues(
        f"project = {PROJECT_KEY} AND statusCategory != Done{excluded_clause} ORDER BY created ASC",
        open_fields,
    )

    done_fields = ["summary", "status", "assignee", "created", "resolutiondate", "labels"]
    done_issues = search_all_issues(
        f"project = {PROJECT_KEY} AND statusCategory = Done "
        f"AND resolutiondate >= -{RECENT_DAYS}d ORDER BY resolutiondate DESC",
        done_fields,
    )

    def is_excluded_assignee(issue_fields):
        assignee = issue_fields.get("assignee")
        name = assignee["displayName"] if assignee else "Unassigned"
        return name in EXCLUDED_ASSIGNEES

    open_issues = [i for i in open_issues if not is_excluded_assignee(i["fields"])]
    done_issues = [i for i in done_issues if not is_excluded_assignee(i["fields"])]

    # ---- open backlog breakdown ----
    status_counts = defaultdict(int)
    assignee_open = defaultdict(int)
    type_counts = defaultdict(int)
    # detail[assignee][status][tag] = count
    detail = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    # assignee_type_counts[assignee][tag] = count, aggregated across all statuses
    assignee_type_counts = defaultdict(lambda: defaultdict(int))
    stale_tickets = []

    for issue in open_issues:
        f = issue["fields"]
        status_name = f["status"]["name"]
        status_counts[status_name] += 1

        assignee = f.get("assignee")
        aname = assignee["displayName"] if assignee else "Unassigned"
        assignee_open[aname] += 1

        tag = classify_type(f["summary"], f.get("labels"))
        type_counts[tag] += 1
        detail[aname][status_name][tag] += 1
        assignee_type_counts[aname][tag] += 1

        created = parse_dt_safe(f["created"])
        if status_name.lower() == "to do" and created and created < stale_cutoff:
            age_days = (now - created).days
            stale_tickets.append({
                "key": issue["key"],
                "summary": f["summary"],
                "assignee": aname,
                "tag": tag,
                "age_days": age_days,
                "url": f"{BASE_URL}/browse/{issue['key']}",
            })

    stale_tickets.sort(key=lambda t: -t["age_days"])

    # ---- turnaround time (created -> resolved) for recently completed work ----
    turnaround_days = []
    assignee_done = defaultdict(int)
    for issue in done_issues:
        f = issue["fields"]
        created = parse_dt_safe(f["created"])
        resolved = parse_dt_safe(f.get("resolutiondate"))
        assignee = f.get("assignee")
        aname = assignee["displayName"] if assignee else "Unassigned"
        assignee_done[aname] += 1
        if created and resolved:
            turnaround_days.append((resolved - created).total_seconds() / 86400.0)

    avg_turnaround = round(sum(turnaround_days) / len(turnaround_days), 1) if turnaround_days else None
    median_turnaround = None
    if turnaround_days:
        s = sorted(turnaround_days)
        mid = len(s) // 2
        median_turnaround = round(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2, 1)

    people = sorted(set(list(assignee_open.keys()) + list(assignee_done.keys())))
    workload = [
        {
            "name": p,
            "open": assignee_open.get(p, 0),
            "done_recent": assignee_done.get(p, 0),
            "by_type": [
                {"tag": t, "count": c}
                for t, c in sorted(assignee_type_counts[p].items(), key=lambda kv: -kv[1])
            ],
        }
        for p in people
    ]
    workload.sort(key=lambda r: -r["open"])

    # ---- weekly opened vs completed trend (last TREND_WEEKS weeks) ----
    week_edges = [now - timedelta(weeks=TREND_WEEKS - i) for i in range(TREND_WEEKS + 1)]
    opened_counts = [0] * TREND_WEEKS
    completed_counts = [0] * TREND_WEEKS

    def bucket_of(dt):
        for i in range(TREND_WEEKS):
            if week_edges[i] <= dt < week_edges[i + 1]:
                return i
        return None

    for issue in open_issues + done_issues:
        created = parse_dt_safe(issue["fields"]["created"])
        if created:
            i = bucket_of(created)
            if i is not None:
                opened_counts[i] += 1

    for issue in done_issues:
        resolved = parse_dt_safe(issue["fields"].get("resolutiondate"))
        if resolved:
            i = bucket_of(resolved)
            if i is not None:
                completed_counts[i] += 1

    trend = {
        # "%-d" isn't portable to Windows, so strip the zero-pad by hand.
        "weeks": [
            week_edges[i].strftime("%b %d").replace(" 0", " ")
            for i in range(TREND_WEEKS)
        ],
        "opened": opened_counts,
        "completed": completed_counts,
    }

    workload_detail = []
    for p in [w["name"] for w in workload]:
        statuses = []
        for status_name, tagmap in detail[p].items():
            types = [{"tag": t, "count": c} for t, c in sorted(tagmap.items(), key=lambda kv: -kv[1])]
            statuses.append({"status": status_name, "total": sum(tagmap.values()), "types": types})
        statuses.sort(key=lambda s: -s["total"])
        workload_detail.append({"name": p, "statuses": statuses})

    type_breakdown = [{"tag": t, "count": c} for t, c in sorted(type_counts.items(), key=lambda kv: -kv[1])]

    data = {
        "generated_at": now.isoformat(),
        "project_key": PROJECT_KEY,
        "recent_window_days": RECENT_DAYS,
        "stale_threshold_days": STALE_DAYS,
        "summary": {
            "open_total": len(open_issues),
            "done_recent_total": len(done_issues),
            "stale_backlog_total": len(stale_tickets),
            "avg_turnaround_days": avg_turnaround,
            "median_turnaround_days": median_turnaround,
        },
        "status_breakdown": [
            {"status": k, "count": v} for k, v in sorted(status_counts.items(), key=lambda kv: -kv[1])
        ],
        "workload": workload,
        "workload_detail": workload_detail,
        "type_breakdown": type_breakdown,
        "trend": trend,
        "stale_tickets": stale_tickets[:25],  # cap for a readable dashboard
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as fh:
        json.dump(data, fh, indent=2)

    print(f"Wrote {OUTPUT_PATH}")
    print(f"  open: {data['summary']['open_total']}, "
          f"done last {RECENT_DAYS}d: {data['summary']['done_recent_total']}, "
          f"stale backlog: {data['summary']['stale_backlog_total']}")


if __name__ == "__main__":
    main()
