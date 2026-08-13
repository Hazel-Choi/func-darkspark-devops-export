#!/usr/bin/env python3
"""
DarkSparkConsulting — Azure DevOps -> Standardised Projects Payload exporter
=============================================================================
DELTA EDITION — only exports work items created or changed since the last
successful run (or an explicit --since / --since-days override), instead of
re-pulling the entire backlog every time.

Pulls work items directly from a client's Azure DevOps project via the REST
API (the same dev.azure.com endpoint already used elsewhere in this project
- NOT the analytics.dev.azure.com OData feed, which is unaffected by this
tool) and writes a CSV that matches the DarkSpark Standardised Projects
Payload spec, Section 3 canonical schema, exactly. No manual export, no
renaming, no reformatting needed afterwards - drop the output file straight
into the SharePoint landing folder.

USAGE
-----
    # Normal day-to-day run - picks up automatically from the last run:
    python devops_export_to_payload.py \
        --org DarkSparkConsulting \
        --project "Zeus Capital" \
        --project-code ZEUS \
        --effort-unit StoryPoints \
        --output-dir ./exports

    # First-ever run for a project (no watermark yet) does a full export
    # automatically and prints a warning. To do that explicitly instead:
    python devops_export_to_payload.py ... --full

    # Manual overrides:
    python devops_export_to_payload.py ... --since 2026-08-01
    python devops_export_to_payload.py ... --since-days 3

Your Personal Access Token (PAT) is never passed on the command line or
stored anywhere. Either:
  - set it once per terminal session:   export AZURE_DEVOPS_PAT="xxxx"
  - or just run the script without it - you'll be prompted securely
    (input is hidden, nothing is written to disk or shell history)

PAT SCOPE REQUIRED
-------------------
    Work Items (Read) — that's it. No org-admin permissions needed.

EFFORT UNIT
-----------
    Azure DevOps has no native field for "what unit is this estimate in" -
    that's a per-project convention (Section 3: EfforUnit, "set once per
    project"). Pass it explicitly with --effort-unit; must be one of:
    StoryPoints, Hours, Days, TShirt, Other.

REMAINING WORK / SNAPSHOT
--------------------------
    RemainingWorkHours (Microsoft.VSTS.Scheduling.RemainingWork) is a
    manually-maintained hours field with no history retained by DevOps
    itself - only "today's value" exists. Every row in this export
    therefore also carries SnapshotDate: the date THIS export ran. This
    pair is the one exception to "current state only" in the payload
    spec - downstream, RemainingWorkHours + SnapshotDate rows are
    APPENDED to a history table (core.WorkItem_RemainingWorkHistory),
    never upserted like every other column. Only leaf-level items
    (Task, Bug, User Story - not Epic/Feature) typically carry this
    field; it will be blank for containers, which is expected.

DELTA FILTERING / WATERMARK
-----------------------------
    Because core.SharePointWorkItem is upserted (current-state-only, keyed
    on WorkItemId), it's always safe to re-send a work item that hasn't
    actually changed - it just won't do anything downstream. So the delta
    filter here is a courtesy to cut export/upload time and payload size,
    not a correctness requirement.

    On every successful run, a small watermark file is written next to
    your exports:
        .last_export_state_{ProjectCode}.json
    containing the UTC timestamp the run STARTED at (not when it finished
    - this way, anything changed while the export was running gets swept
    up on the *next* run instead of silently skipped).

    Resolution order for "since when":
        1. --since <date/datetime>   (explicit override, highest priority)
        2. --since-days <N>          (relative override)
        3. watermark file from a previous run in --output-dir
        4. none of the above -> FULL EXPORT (first run), with a warning

    --full forces a full export and ignores the watermark (it still
    updates the watermark afterwards unless you also pass
    --no-state-update).

    NOTE: parent/child relationships (ParentId) are stored, but if a
    parent itself hasn't changed recently it won't be *re-sent* in a
    delta run. That's fine for this pipeline: the parent row already
    exists in core.Project from its own prior export, and ParentId here
    is just a foreign-key value on the child row, not a join that needs
    both rows present in the same file.

OUTPUT
------
    ./exports/{SourceSystem}_{ProjectCode}_{YYYYMMDDHHmmss}Z.csv
    e.g. AzureDevOps_ZEUS_20260723T101500Z.csv

    Columns exactly match the Standardised Projects Payload spec, Section 3:
    ProjectCode, SourceSystem, WorkItemId, Title, WorkItemType, State,
    ParentId, AssignedTo, AreaOrComponent, IterationOrSprint, CreatedDate,
    ChangedDate, StateChangeDate, ClosedDate, EffortValue, EfforUnit,
    IsBlocked, OriginalEstimateHours, RemainingWorkHours, SnapshotDate

ORIGINAL ESTIMATE
------------------
    OriginalEstimateHours (Microsoft.VSTS.Scheduling.OriginalEstimate) is
    the baseline hours estimate for a work item, paired with
    RemainingWorkHours to compute time-based progress (e.g. flagging
    tickets past 50% of estimated time consumed). Unlike RemainingWorkHours,
    this is current-state and upserted like every other column - it is
    NOT snapshotted/appended to a history table, since an original
    estimate isn't expected to change day to day. As with RemainingWork,
    it's typically only populated on leaf-level items (Task, Bug, User
    Story - not Epic/Feature); blank is expected for containers.
"""

import argparse
import csv
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

API_VERSION = "7.1"

VALID_EFFORT_UNITS = ["StoryPoints", "Hours", "Days", "TShirt", "Other"]

STANDARD_COLUMNS = [
    "ProjectCode", "SourceSystem", "WorkItemId", "Title", "WorkItemType",
    "State", "ParentId", "AssignedTo", "AreaOrComponent", "IterationOrSprint",
    "CreatedDate", "ChangedDate", "StateChangeDate", "ClosedDate",
    "EffortValue", "EfforUnit", "IsBlocked",
    "OriginalEstimateHours", "RemainingWorkHours", "SnapshotDate",
]

# Requested fields. Effort is process-template-specific (Agile vs Scrum vs
# CMMI) - we request both plausible candidates and pick the first present.
FIELD_REFS = [
    "System.Id", "System.Title", "System.WorkItemType", "System.State",
    "System.Parent", "System.AssignedTo",
    "System.AreaPath", "System.IterationPath",
    "System.CreatedDate", "System.ChangedDate",
    "Microsoft.VSTS.Common.StateChangeDate", "Microsoft.VSTS.Common.ClosedDate",
    "Microsoft.VSTS.Scheduling.Effort", "Microsoft.VSTS.Scheduling.StoryPoints",
    "Microsoft.VSTS.Scheduling.RemainingWork",
    "Microsoft.VSTS.Scheduling.OriginalEstimate",
    "Microsoft.VSTS.CMMI.Blocked", "System.Tags",
]

BATCH_SIZE = 200

WIQL_DATETIME_FMT = "%Y-%m-%dT%H:%M:%SZ"  # WIQL literal format (no 'T'/'Z')


def get_pat():
    pat = os.environ.get("AZURE_DEVOPS_PAT")
    if pat:
        return pat
    return getpass.getpass("Azure DevOps Personal Access Token (input hidden): ").strip()


def iso_date_only(value):
    """DevOps returns full ISO8601 datetimes; the payload spec wants YYYY-MM-DD."""
    if not value:
        return ""
    try:
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).date().isoformat()
    except ValueError:
        return value[:10]  # best-effort fallback


def extract_assigned_to(field_value):
    """AssignedTo comes back as an identity object, not a plain string."""
    if not field_value:
        return ""
    if isinstance(field_value, dict):
        return field_value.get("uniqueName") or field_value.get("displayName") or ""
    return str(field_value)


def first_present(fields, *ref_names):
    """Return the first non-empty value among several candidate field refs
    (used for Effort/StoryPoints, which vary by process template)."""
    for ref in ref_names:
        if ref in fields and fields[ref] not in (None, ""):
            return fields[ref]
    return ""


def extract_is_blocked(fields):
    """Blocked field varies by process template (CMMI has a dedicated
    field; Agile/Scrum teams often use a 'Blocked' tag instead)."""
    blocked_field = fields.get("Microsoft.VSTS.CMMI.Blocked")
    if blocked_field:
        return "Yes" if str(blocked_field).strip().lower() == "yes" else "No"
    tags = fields.get("System.Tags", "") or ""
    if "blocked" in tags.lower():
        return "Yes"
    return ""  # leave blank if not tracked at all - spec treats blank as "not blocked"


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


def build_wiql(project, since_dt):
    query = (
        "SELECT [System.Id] FROM WorkItems "
        "WHERE [System.TeamProject] = @project "
        "AND [System.State] <> 'Removed' "
    )
    if since_dt is not None:
        since_literal = since_dt.astimezone(timezone.utc).strftime(WIQL_DATETIME_FMT)
        query += f"AND [System.ChangedDate] >= '{since_literal}' "
    query += "ORDER BY [System.Id]"
    return query


def fetch_work_item_ids(session, org, project, since_dt):
    url = f"https://dev.azure.com/{org}/{project}/_apis/wit/wiql?timePrecision=true&api-version={API_VERSION}"
    wiql = {"query": build_wiql(project, since_dt)}
    resp = session.post(url, json=wiql)
    resp.raise_for_status()
    return [wi["id"] for wi in resp.json().get("workItems", [])]


def fetch_work_items_batch(session, org, ids):
    url = f"https://dev.azure.com/{org}/_apis/wit/workitemsbatch?api-version={API_VERSION}"
    body = {"ids": ids, "fields": FIELD_REFS}
    resp = session.post(url, json=body)
    resp.raise_for_status()
    return resp.json().get("value", [])


def chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def build_rows(work_items, project_code, effort_unit):
    rows = []
    # One snapshot date for the whole export run - every row in this file
    # represents "state as of this moment", so RemainingWorkHours values
    # across the file are all comparable to each other as one point in time.
    snapshot_date = datetime.now(timezone.utc).date().isoformat()

    for wi in work_items:
        f = wi.get("fields", {})
        effort_value = first_present(
            f, "Microsoft.VSTS.Scheduling.Effort", "Microsoft.VSTS.Scheduling.StoryPoints")
        remaining_work = f.get("Microsoft.VSTS.Scheduling.RemainingWork", "")
        original_estimate = f.get("Microsoft.VSTS.Scheduling.OriginalEstimate", "")

        rows.append({
            "ProjectCode": project_code,
            "SourceSystem": "AzureDevOps",
            "WorkItemId": wi.get("id", ""),
            "Title": f.get("System.Title", ""),
            "WorkItemType": f.get("System.WorkItemType", ""),
            "State": f.get("System.State", ""),
            "ParentId": f.get("System.Parent", ""),
            "AssignedTo": extract_assigned_to(f.get("System.AssignedTo")),
            "AreaOrComponent": f.get("System.AreaPath", ""),
            "IterationOrSprint": f.get("System.IterationPath", ""),
            "CreatedDate": iso_date_only(f.get("System.CreatedDate")),
            "ChangedDate": iso_date_only(f.get("System.ChangedDate")),
            "StateChangeDate": iso_date_only(f.get("Microsoft.VSTS.Common.StateChangeDate")),
            "ClosedDate": iso_date_only(f.get("Microsoft.VSTS.Common.ClosedDate")),
            # EfforUnit only applies when there's actually an effort value to qualify
            "EffortValue": effort_value,
            "EfforUnit": effort_unit if effort_value not in ("", None) else "",
            "IsBlocked": extract_is_blocked(f),
            # Current-state field like the rest of the payload (upserted,
            # not snapshotted) - unlike RemainingWorkHours, it doesn't need
            # SnapshotDate gating since it isn't appended to a history table.
            "OriginalEstimateHours": original_estimate,
            "RemainingWorkHours": remaining_work,
            # Blank RemainingWorkHours -> blank SnapshotDate too, so a
            # ticket that never carried this field doesn't get a false
            # "0 hours remaining" history row downstream.
            "SnapshotDate": snapshot_date if remaining_work not in ("", None) else "",
        })
    return rows


def write_csv(rows, output_path):
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=STANDARD_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Export Azure DevOps work items to standardised payload CSV (delta-filtered)")
    parser.add_argument("--org", required=True, help="Azure DevOps organisation name (e.g. DarkSparkConsulting)")
    parser.add_argument("--project", required=True, help="Azure DevOps project name (e.g. 'Zeus Capital')")
    parser.add_argument("--project-code", required=True, help="DarkSpark project code (e.g. ZEUS)")
    parser.add_argument("--effort-unit", required=True, choices=VALID_EFFORT_UNITS,
                         help="Unit EffortValue is expressed in for this project (set once per project)")
    parser.add_argument("--output-dir", default=".", help="Directory to write the CSV into (default: current dir)")
    parser.add_argument("--since", default=None,
                         help="Only export items changed/created on or after this date/datetime "
                              "(YYYY-MM-DD or full ISO datetime). Overrides the watermark.")
    parser.add_argument("--since-days", type=int, default=None,
                         help="Only export items changed/created in the last N days. Overrides the watermark.")
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
        print(f"Delta filter: exporting items changed on/after "
              f"{since_dt.astimezone(timezone.utc).isoformat()} ({since_source}).")

    pat = get_pat()
    session = requests.Session()
    session.auth = ("", pat)

    print(f"Querying work item IDs for project '{args.project}'...")
    try:
        ids = fetch_work_item_ids(session, args.org, args.project, since_dt)
    except requests.HTTPError as e:
        print(f"ERROR: could not query work items - check org/project name and PAT scope.\n{e}", file=sys.stderr)
        sys.exit(1)

    if not ids:
        print("No work items found in this window - nothing to export.")
        if not args.no_state_update:
            save_watermark(args.output_dir, args.project_code, run_start_utc)
        sys.exit(0)

    print(f"Found {len(ids)} work items. Fetching details in batches of {BATCH_SIZE}...")
    all_items = []
    for batch in chunk(ids, BATCH_SIZE):
        all_items.extend(fetch_work_items_batch(session, args.org, batch))

    rows = build_rows(all_items, args.project_code, args.effort_unit)

    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = run_start_utc.strftime("%Y%m%dT%H%M%S")
    filename = f"AzureDevOps_{args.project_code}_{timestamp}Z.csv"
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
