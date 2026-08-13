#flag : customfield_10014 (epic link), customfield_10020 (sprint), customfield_10021 (flagged) are common Jira Cloud defaults
#StateChangeDate is left bank for Jira by default


#!/usr/bin/env python3
"""
DarkSparkConsulting — Jira -> Standardised Projects Payload exporter
=======================================================================
DELTA EDITION — only exports issues created or updated since the last
successful run (or an explicit --since / --since-days override), instead of
re-pulling the entire backlog every time.

Pulls issues from a client's Jira project via the REST API (v3) and
writes a CSV matching the DarkSpark Standardised Projects Payload spec,
Section 3 canonical schema, exactly.

USAGE
-----
    # Normal day-to-day run - picks up automatically from the last run:
    python jira_export_to_payload.py \
        --site darksparkconsulting.atlassian.net \
        --project POOK \
        --project-code POOK-OSDM \
        --story-points-field customfield_10016 \
        --effort-unit StoryPoints \
        --output-dir ./exports

    # First-ever run for a project (no watermark yet) does a full export
    # automatically and prints a warning. To do that explicitly instead:
    python jira_export_to_payload.py ... --full

    # Manual overrides:
    python jira_export_to_payload.py ... --since 2026-08-01
    python jira_export_to_payload.py ... --since-days 3

CREDENTIALS
-----------
    Your Jira email and API token are never passed on the command line.
    Either set both once per terminal session:
        export JIRA_EMAIL="you@darksparkconsulting.com"
        export JIRA_API_TOKEN="xxxx"
    or run without them - you'll be prompted (token input is hidden).

    API tokens are created at https://id.atlassian.com/manage-profile/security/api-tokens

STORY POINTS FIELD
------------------
    Jira's Story Points field is a CUSTOM field with an ID that varies
    per Jira instance (e.g. customfield_10016). You must look this up
    once per client site (Project Settings > Fields, or via
    /rest/api/3/field) and pass it with --story-points-field. If not
    supplied, EffortValue is left BLANK for this project - it no longer
    falls back to Original Time Estimate, since that value now has its
    own dedicated column (see ORIGINAL ESTIMATE below). Mixing size
    estimates (story points) and time estimates (hours) into the same
    EffortValue column made EfforUnit ambiguous and duplicated data
    once OriginalEstimateHours was added.

ORIGINAL ESTIMATE
------------------
    OriginalEstimateHours (timetracking.originalEstimateSeconds,
    converted to hours) is the baseline hours estimate for an issue,
    captured regardless of whether --story-points-field is set. Paired
    with RemainingWorkHours to compute time-based progress (e.g.
    flagging tickets past 50% of estimated time consumed). Unlike
    RemainingWorkHours, this is current-state and upserted like every
    other column - it is NOT snapshotted/appended to a history table,
    since an original estimate isn't expected to change day to day.

REMAINING WORK / SNAPSHOT
--------------------------
    RemainingWorkHours (timetracking.remainingEstimateSeconds, converted
    to hours) is manually maintained per-issue with no history retained
    by Jira itself. As with the DevOps exporter, every row also carries
    SnapshotDate: the date THIS export ran. Downstream, this pair is
    APPENDED to a history table, never upserted like other columns.

DELTA FILTERING / WATERMARK
-----------------------------
    Because core.SharePointWorkItem is upserted (current-state-only, keyed
    on WorkItemId), it's always safe to re-send an issue that hasn't
    actually changed - it just won't do anything downstream. So this delta
    filter is a courtesy to cut export/upload time and payload size, not a
    correctness requirement.

    On every successful run, a small watermark file is written next to
    your exports:
        .last_export_state_{ProjectCode}.json
    containing the UTC timestamp the run STARTED at (not when it finished
    - this way, anything updated while the export was running gets swept
    up on the *next* run instead of silently skipped).

    Resolution order for "since when":
        1. --since <date/datetime>   (explicit override, highest priority)
        2. --since-days <N>          (relative override)
        3. watermark file from a previous run in --output-dir
        4. none of the above -> FULL EXPORT (first run), with a warning

    --full forces a full export and ignores the watermark (it still
    updates the watermark afterwards unless you also pass
    --no-state-update).

    NOTE ON TIMEZONES: JQL's `updated >=` comparison is evaluated in the
    Jira SITE's configured timezone, not necessarily UTC. The watermark
    is stored and compared in UTC for consistency across projects/sites,
    which means a Jira site running several hours off UTC could in
    theory re-pull an issue right at the boundary of the previous run -
    harmless given the upsert model above, just noting it so it isn't a
    surprise.

    NOTE: parent/epic links (ParentId) are stored, but if a parent issue
    itself hasn't been updated recently it won't be *re-sent* in a delta
    run. That's fine here: the parent row already exists in core.Project
    from its own prior export: ParentId on the child row is just a
    foreign-key value, not a join that needs both rows in the same file.

OUTPUT
------
    ./exports/{SourceSystem}_{ProjectCode}_{YYYYMMDDHHmmss}Z.csv
    e.g. Jira_POOK-OSDM_20260723T101500Z.csv
"""

import argparse
import csv
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

API_VERSION = "3"
VALID_EFFORT_UNITS = ["StoryPoints", "Hours", "Days", "TShirt", "Other"]

STANDARD_COLUMNS = [
    "ProjectCode", "SourceSystem", "WorkItemId", "Title", "WorkItemType",
    "State", "ParentId", "AssignedTo", "AreaOrComponent", "IterationOrSprint",
    "CreatedDate", "ChangedDate", "StateChangeDate", "ClosedDate",
    "EffortValue", "EfforUnit", "IsBlocked",
    "OriginalEstimateHours", "RemainingWorkHours", "SnapshotDate",
]

PAGE_SIZE = 100

JQL_DATETIME_FMT = "%Y-%m-%d %H:%M"  # JQL literal format for absolute datetimes


def get_credentials():
    email = os.environ.get("JIRA_EMAIL")
    if not email:
        email = input("Jira account email: ").strip()
    token = os.environ.get("JIRA_API_TOKEN")
    if not token:
        token = getpass.getpass("Jira API token (input hidden): ").strip()
    return email, token


def iso_date_only(value):
    """Jira returns full ISO8601 datetimes with timezone offsets;
    the payload spec wants YYYY-MM-DD."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return value[:10]  # best-effort fallback


def extract_assignee(assignee_field):
    if not assignee_field:
        return ""
    return assignee_field.get("emailAddress") or assignee_field.get("displayName") or ""


def extract_parent_key(fields):
    parent = fields.get("parent")
    if parent:
        return parent.get("key", "")
    # Company-managed projects sometimes use a separate Epic Link field
    # instead of the standard parent relationship.
    epic_link = fields.get("customfield_10014")  # common default; varies per instance
    return epic_link or ""


def extract_sprint(fields):
    """Sprint is a custom field (Scrum boards only) returning a list of
    sprint objects; take the active/most recent one. Blank on Kanban-only
    boards, matching the spec's guidance."""
    sprint_field = fields.get("customfield_10020")  # common default; varies per instance
    if not sprint_field:
        return ""
    if isinstance(sprint_field, list) and sprint_field:
        # Prefer an active sprint if present, else the last one in the list
        for s in sprint_field:
            if isinstance(s, dict) and s.get("state") == "active":
                return s.get("name", "")
        last = sprint_field[-1]
        return last.get("name", "") if isinstance(last, dict) else ""
    return ""


def extract_is_blocked(fields):
    flagged = fields.get("customfield_10021")  # common default for "Flagged"; varies per instance
    if flagged:
        return "Yes"
    labels = fields.get("labels", []) or []
    if any("impediment" in label.lower() for label in labels):
        return "Yes"
    return ""


def seconds_to_hours(seconds):
    if seconds in (None, ""):
        return ""
    return round(seconds / 3600, 2)


# ---------------------------------------------------------------------------
# Delta / watermark helpers
# ---------------------------------------------------------------------------

def state_file_path(output_dir, project_code):
    return os.path.join(output_dir, f".last_export_state_{project_code}.json")


def load_watermark(output_dir, project_code):
    path = state_file_path(output_dir, project_code)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("last_run_utc")
    except (json.JSONDecodeError, OSError):
        return None


def save_watermark(output_dir, project_code, run_start_utc):
    os.makedirs(output_dir, exist_ok=True)
    path = state_file_path(output_dir, project_code)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"last_run_utc": run_start_utc.isoformat()}, f)


def parse_since_arg(value):
    """Accept either 'YYYY-MM-DD' or a full ISO datetime for --since."""
    try:
        if len(value) <= 10:
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        print(f"ERROR: could not parse --since value '{value}'. "
              f"Use YYYY-MM-DD or a full ISO datetime.", file=sys.stderr)
        sys.exit(1)


def resolve_since(args, output_dir, project_code):
    """Returns (since_datetime_or_None, source_description)."""
    if args.full:
        return None, "--full flag (ignoring any watermark)"
    if args.since:
        return parse_since_arg(args.since), "--since override"
    if args.since_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.since_days)
        return cutoff, f"--since-days {args.since_days} override"

    watermark = load_watermark(output_dir, project_code)
    if watermark:
        try:
            return datetime.fromisoformat(watermark), "watermark from previous run"
        except ValueError:
            pass  # corrupt watermark, fall through to full export

    return None, "no watermark found - full export"


def build_jql(project, since_dt):
    jql = f"project = {project}"
    if since_dt is not None:
        since_literal = since_dt.astimezone(timezone.utc).strftime(JQL_DATETIME_FMT)
        jql += f' AND updated >= "{since_literal}"'
    jql += " ORDER BY created ASC"
    return jql


def fetch_issues(session, site, project, story_points_field, since_dt):
    url = f"https://{site}/rest/api/{API_VERSION}/search/jql"
    fields = [
        "summary", "issuetype", "status", "parent", "assignee",
        "components", "created", "updated", "resolutiondate",
        "timetracking", "labels", "customfield_10014", "customfield_10020",
        "customfield_10021", "statuscategorychangedate",
    ]
    if story_points_field:
        fields.append(story_points_field)

    jql = build_jql(project, since_dt)

    all_issues = []
    next_page_token = None
    while True:
        body = {
            "jql": jql,
            "maxResults": PAGE_SIZE,
            "fields": fields,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token
        resp = session.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
        all_issues.extend(data.get("issues", []))
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return all_issues


def build_rows(issues, project_code, effort_unit, story_points_field):
    rows = []
    snapshot_date = datetime.now(timezone.utc).date().isoformat()

    for issue in issues:
        f = issue.get("fields", {})

        # EffortValue is size-only now: populated from Story Points when
        # configured, left blank otherwise. It no longer falls back to
        # Original Time Estimate - that value has its own dedicated
        # column below, captured independently of --story-points-field.
        effort_value = ""
        if story_points_field and f.get(story_points_field) not in (None, ""):
            effort_value = f.get(story_points_field)

        original_estimate_seconds = (f.get("timetracking") or {}).get("originalEstimateSeconds")
        original_estimate_hours = seconds_to_hours(original_estimate_seconds)

        remaining_seconds = (f.get("timetracking") or {}).get("remainingEstimateSeconds")
        remaining_hours = seconds_to_hours(remaining_seconds)

        components = f.get("components") or []
        area = components[0].get("name", "") if components else ""

        rows.append({
            "ProjectCode": project_code,
            "SourceSystem": "Jira",
            "WorkItemId": issue.get("key", ""),
            "Title": f.get("summary", ""),
            "WorkItemType": (f.get("issuetype") or {}).get("name", ""),
            "State": (f.get("status") or {}).get("name", ""),
            "ParentId": extract_parent_key(f),
            "AssignedTo": extract_assignee(f.get("assignee")),
            "AreaOrComponent": area,
            "IterationOrSprint": extract_sprint(f),
            "CreatedDate": iso_date_only(f.get("created")),
            "ChangedDate": iso_date_only(f.get("updated")),
            # Jira has no direct "state change date" field without a
            # changelog export - leaving blank per spec guidance unless
            # your instance has "Status Category Changed" exposed.
            "StateChangeDate": iso_date_only(f.get("statuscategorychangedate")),
            "ClosedDate": iso_date_only(f.get("resolutiondate")),
            "EffortValue": effort_value,
            "EfforUnit": effort_unit if effort_value not in ("", None) else "",
            "IsBlocked": extract_is_blocked(f),
            "OriginalEstimateHours": original_estimate_hours,
            "RemainingWorkHours": remaining_hours,
            "SnapshotDate": snapshot_date if remaining_hours not in ("", None) else "",
        })
    return rows


def write_csv(rows, output_path):
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=STANDARD_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Export Jira issues to standardised payload CSV (delta-filtered)")
    parser.add_argument("--site", required=True, help="Jira site hostname (e.g. darksparkconsulting.atlassian.net)")
    parser.add_argument("--project", required=True, help="Jira project key (e.g. POOK)")
    parser.add_argument("--project-code", required=True, help="DarkSpark project code (e.g. POOK-OSDM)")
    parser.add_argument("--story-points-field", default=None,
                         help="Custom field ID for Story Points on this Jira instance (e.g. customfield_10016). "
                              "If omitted, EffortValue is left blank for this project (OriginalEstimateHours "
                              "is still captured independently from Original Time Estimate).")
    parser.add_argument("--effort-unit", required=True, choices=VALID_EFFORT_UNITS,
                         help="Unit EffortValue is expressed in for this project (set once per project)")
    parser.add_argument("--output-dir", default=".", help="Directory to write the CSV into (default: current dir)")
    parser.add_argument("--since", default=None,
                         help="Only export issues updated on or after this date/datetime "
                              "(YYYY-MM-DD or full ISO datetime, UTC). Overrides the watermark.")
    parser.add_argument("--since-days", type=int, default=None,
                         help="Only export issues updated in the last N days. Overrides the watermark.")
    parser.add_argument("--full", action="store_true",
                         help="Force a full export, ignoring any watermark from a previous run.")
    parser.add_argument("--no-state-update", action="store_true",
                         help="Don't update the watermark file after this run (useful for test runs).")
    args = parser.parse_args()

    run_start_utc = datetime.now(timezone.utc)

    since_dt, since_source = resolve_since(args, args.output_dir, args.project_code)
    if since_dt is None:
        print("WARNING: no delta filter in effect - this will be a FULL export "
              f"({since_source}).")
    else:
        print(f"Delta filter: exporting issues updated on/after "
              f"{since_dt.astimezone(timezone.utc).isoformat()} ({since_source}).")

    email, token = get_credentials()
    session = requests.Session()
    session.auth = (email, token)
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    print(f"Querying issues for project '{args.project}'...")
    try:
        issues = fetch_issues(session, args.site, args.project, args.story_points_field, since_dt)
    except requests.HTTPError as e:
        print(f"ERROR: could not query issues - check site/project/credentials.\n{e}", file=sys.stderr)
        sys.exit(1)

    if not issues:
        print("No issues found in this window - nothing to export.")
        if not args.no_state_update:
            save_watermark(args.output_dir, args.project_code, run_start_utc)
        sys.exit(0)

    print(f"Found {len(issues)} issues.")
    rows = build_rows(issues, args.project_code, args.effort_unit, args.story_points_field)

    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = run_start_utc.strftime("%Y%m%dT%H%M%S")
    filename = f"Jira_{args.project_code}_{timestamp}Z.csv"
    output_path = os.path.join(args.output_dir, filename)
    write_csv(rows, output_path)

    print(f"Done. Wrote {len(rows)} rows to {output_path}")
    print("Drop this file into the CentralProjectManagement/Shared Documents SharePoint folder.")

    if not args.no_state_update:
        save_watermark(args.output_dir, args.project_code, run_start_utc)
        print(f"Watermark updated: next run will pick up changes after {run_start_utc.isoformat()}.")
    else:
        print("Watermark NOT updated (--no-state-update set).")


if __name__ == "__main__":
    main()
