import csv
import io
import json
import logging
import os
import time
from datetime import datetime, timezone

import azure.functions as func
import pyodbc
import requests
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.mgmt.sql import SqlManagementClient
from azure.storage.blob import BlobServiceClient

import devops_export_to_payload as devops_exporter
import jira_export_to_payload as jira_exporter

app = func.FunctionApp()

# --- Configure these two for your tenant ---
KEY_VAULT_URL = "https://kv-centralsystem.vault.azure.net/"
DEVOPS_ORG = "DarkSparkConsulting"
# --------------------------------------------

# --- Central SQL pause/resume config ---
SUBSCRIPTION_ID = "9af8ed4d-1ade-425e-870f-bd79eb8e00ce"
RESOURCE_GROUP = "HC_Experiments"
SQL_SERVER_NAME = "centralsystem"
SQL_DATABASE_NAME = "darkspark-central-system"
# ----------------------------------------

# --- Project export config ---
SQL_SERVER_HOST = "centralsystem.database.windows.net"
SQL_READER_USER = "func_project_config_reader"
SQL_READER_PASSWORD_SECRET_NAME = "sql-func-project-config-reader-password"

LANDING_STORAGE_ACCOUNT_URL = "https://stdarksparklanding.blob.core.windows.net"
LANDING_CONTAINER_NAME = "automated-exports"  # adjust if you'd rather use a different container name

# Any project whose OrgOrSite matches this falls back to a shared PAT if its
# own per-project/per-staff secret lookup fails. Covers new/interim projects
# (e.g. DKSP-INTL) that don't have a dedicated credential set up yet. The
# secret itself can be rotated in Key Vault at any time without touching
# this code — only the secret *name* below needs to stay in sync.
FALLBACK_ORG = "DarkSparkConsulting"
FALLBACK_DEVOPS_SECRET_NAME = "devops-pat-darkspark-all-hazel"
# ------------------------------


@app.function_name(name="TestDevOpsAuth")
@app.route(route="test-devops-auth", auth_level=func.AuthLevel.FUNCTION)
def test_devops_auth(req: func.HttpRequest) -> func.HttpResponse:
    """
    Manual CAP probe. Call with:
      GET https://<your-func-app>.azurewebsites.net/api/test-devops-auth?code=<function-key>&secret_name=devops-pat-hazel

    secret_name = the Key Vault secret holding a PAT to test.
    """
    secret_name = req.params.get("secret_name")
    if not secret_name:
        return func.HttpResponse(
            "Pass ?secret_name=<key-vault-secret-name>, e.g. devops-pat-hazel",
            status_code=400,
        )

    # 1. Pull the PAT from Key Vault using the Function App's Managed Identity
    try:
        credential = DefaultAzureCredential()
        kv_client = SecretClient(vault_url=KEY_VAULT_URL, credential=credential)
        pat = kv_client.get_secret(secret_name).value
    except Exception as e:
        logging.error(f"Key Vault fetch failed for '{secret_name}': {e}")
        return func.HttpResponse(
            f"Key Vault error (check Managed Identity has 'Key Vault Secrets User' "
            f"role, and the secret name is correct): {e}",
            status_code=500,
        )

    # 2. Make one lightweight authenticated call to Azure DevOps
    url = f"https://dev.azure.com/{DEVOPS_ORG}/_apis/projects?api-version=7.1"
    try:
        resp = requests.get(url, auth=("", pat), timeout=15)
    except Exception as e:
        logging.error(f"Request to Azure DevOps failed: {e}")
        return func.HttpResponse(f"Request error: {e}", status_code=500)

    body_snippet = resp.text[:500]
    result = (
        f"Secret used: {secret_name}\n"
        f"HTTP status: {resp.status_code}\n"
        f"Response snippet:\n{body_snippet}\n"
    )

    # Azure DevOps' tell-tale CAP block signature: a 203, or an HTML body
    # redirecting to an interactive AAD login page instead of clean JSON.
    looks_blocked = (
        resp.status_code == 203
        or "<html" in resp.text.lower()
        or "conditional access" in resp.text.lower()
        or "login.microsoftonline.com" in resp.text.lower()
    )

    if looks_blocked:
        result += (
            "\n>>> This looks like a Conditional Access block: DevOps is redirecting "
            "to an interactive sign-in instead of returning JSON. Cloud-native "
            "execution is likely a dead end for this project's PAT — next option "
            "would be a Hybrid Runbook Worker or self-hosted runner installed on a "
            "trusted/compliant machine. <<<"
        )
    elif resp.status_code == 200:
        result += "\n>>> Success — this project's PAT authenticated fine from the cloud. <<<"

    return func.HttpResponse(result, status_code=200)


def _get_sql_client() -> SqlManagementClient:
    credential = DefaultAzureCredential()
    return SqlManagementClient(credential, SUBSCRIPTION_ID)


@app.function_name(name="PauseCentralSql")
@app.timer_trigger(schedule="0 0 19 * * 1-5", arg_name="pauseTimer", run_on_startup=False)
def pause_central_sql(pauseTimer: func.TimerRequest) -> None:
    """
    Weekday 7pm pause for darkspark-central-system. Left paused straight through
    the weekend since the Monday resume trigger is the next thing to touch it.
    Uses the Function App's Managed Identity, scoped via the custom
    'SQL DB Pause-Resume Operator' role to just this one database.
    """
    logging.info("Pausing darkspark-central-system for the evening.")
    client = _get_sql_client()
    try:
        poller = client.databases.begin_pause(RESOURCE_GROUP, SQL_SERVER_NAME, SQL_DATABASE_NAME)
        poller.result()
        logging.info("Database pause completed.")
    except Exception as e:
        # Already-paused (e.g. from inactivity auto-pause) throws a conflict-type
        # error here — log it, but it's not a real failure.
        logging.warning(f"Pause request finished with a non-fatal issue: {e}")


@app.function_name(name="ResumeCentralSql")
@app.timer_trigger(schedule="0 0 7 * * 1-5", arg_name="resumeTimer", run_on_startup=False)
def resume_central_sql(resumeTimer: func.TimerRequest) -> None:
    """
    Weekday 7am resume for darkspark-central-system, warmed up before the first
    user connects so nobody hits the serverless cold-start ETIMEOUT.
    """
    logging.info("Resuming darkspark-central-system for the workday.")
    client = _get_sql_client()
    try:
        poller = client.databases.begin_resume(RESOURCE_GROUP, SQL_SERVER_NAME, SQL_DATABASE_NAME)
        poller.result()
        logging.info("Database resume completed.")
    except Exception as e:
        logging.warning(f"Resume request finished with a non-fatal issue: {e}")


# =====================================================================
# Project export orchestrator
# =====================================================================

def _get_active_projects(kv_client: SecretClient) -> list[dict]:
    """
    Reads core.vw_ActiveExportableProjects — only fully-configured, active
    projects appear here. A project missing OrgOrSite/SourceProjectName/
    EffortUnit or with IsActive=0 simply isn't returned, so incomplete
    projects are skipped with zero special-casing in this code.

    Uses pyodbc (the standard Microsoft-supported path for Python + Azure
    SQL) after python-tds hit an unresolvable upstream compatibility bug
    between its TLS handling and current pyOpenSSL/cryptography versions.
    Requires the ODBC Driver 18 for SQL Server to be present on the host —
    if it isn't, this will fail with a clear "driver not found" error rather
    than an obscure one.
    """
    password = kv_client.get_secret(SQL_READER_PASSWORD_SECRET_NAME).value
    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{SQL_SERVER_HOST},1433;"
        f"Database={SQL_DATABASE_NAME};"
        f"Uid={SQL_READER_USER};"
        f"Pwd={password};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60;"
    )

    # The database's own inactivity-based auto-pause is independent of our
    # PauseCentralSql/ResumeCentralSql schedule — if it's gone to sleep
    # between runs, the first connection attempt can time out waking it,
    # even though that same attempt kicks off the resume server-side. Retry
    # a couple of times with backoff rather than failing the whole run.
    last_error = None
    for attempt in range(1, 4):
        try:
            conn = pyodbc.connect(conn_str)
            break
        except pyodbc.Error as e:
            last_error = e
            logging.warning(f"SQL connection attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                time.sleep(15 * attempt)
    else:
        raise last_error

    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ProjectCode, SourceSystem, OrgOrSite, SourceProjectName, "
            "EffortUnit, JiraStoryPointsField, KeyVaultSecretName "
            "FROM core.vw_ActiveExportableProjects"
        )
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def _watermark_blob_client(blob_service_client: BlobServiceClient, project_code: str):
    return blob_service_client.get_blob_client(
        container=LANDING_CONTAINER_NAME, blob=f"_watermarks/{project_code}.json"
    )


def _load_watermark(blob_service_client: BlobServiceClient, project_code: str):
    """Returns a UTC datetime, or None if no watermark yet (-> full export)."""
    blob_client = _watermark_blob_client(blob_service_client, project_code)
    try:
        raw = blob_client.download_blob().readall()
        payload = json.loads(raw)
        return datetime.fromisoformat(payload["last_run_start_utc"])
    except Exception:
        # Blob doesn't exist yet, or is corrupt — either way, fall back to a
        # full export rather than fail the whole run.
        return None


def _save_watermark(blob_service_client: BlobServiceClient, project_code: str, run_start_utc: datetime) -> None:
    blob_client = _watermark_blob_client(blob_service_client, project_code)
    payload = json.dumps({"last_run_start_utc": run_start_utc.isoformat()})
    blob_client.upload_blob(payload, overwrite=True)


def _rows_to_csv_text(rows: list[dict], columns: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _upload_csv(blob_service_client: BlobServiceClient, project_code: str, source_system: str,
                 run_start_utc: datetime, csv_text: str) -> str:
    timestamp = run_start_utc.strftime("%Y%m%dT%H%M%S")
    filename = f"{source_system}_{project_code}_{timestamp}Z.csv"
    blob_path = f"{project_code}/{filename}"
    blob_client = blob_service_client.get_blob_client(container=LANDING_CONTAINER_NAME, blob=blob_path)
    blob_client.upload_blob(csv_text, overwrite=True)
    return blob_path


def _process_devops_project(kv_client: SecretClient, blob_service_client: BlobServiceClient, project: dict) -> int:
    project_code = project["ProjectCode"]
    secret_name = project["KeyVaultSecretName"]

    try:
        pat = kv_client.get_secret(secret_name).value
    except Exception as primary_err:
        if project.get("OrgOrSite") != FALLBACK_ORG:
            raise
        logging.warning(
            f"{project_code}: primary secret '{secret_name}' failed ({primary_err}); "
            f"trying fallback '{FALLBACK_DEVOPS_SECRET_NAME}'"
        )
        pat = kv_client.get_secret(FALLBACK_DEVOPS_SECRET_NAME).value

    run_start_utc = datetime.now(timezone.utc)
    since_dt = _load_watermark(blob_service_client, project_code)
    if since_dt is None:
        logging.warning(f"{project_code}: no watermark found — this will be a FULL export.")

    session = requests.Session()
    session.auth = ("", pat)

    ids = devops_exporter.fetch_work_item_ids(session, project["OrgOrSite"], project["SourceProjectName"], since_dt)
    if not ids:
        logging.info(f"{project_code}: no work items in this window.")
        _save_watermark(blob_service_client, project_code, run_start_utc)
        return 0

    all_items = []
    for batch in devops_exporter.chunk(ids, devops_exporter.BATCH_SIZE):
        all_items.extend(devops_exporter.fetch_work_items_batch(session, project["OrgOrSite"], batch))

    rows = devops_exporter.build_rows(all_items, project_code, project["EffortUnit"])
    csv_text = _rows_to_csv_text(rows, devops_exporter.STANDARD_COLUMNS)
    blob_path = _upload_csv(blob_service_client, project_code, "AzureDevOps", run_start_utc, csv_text)
    _save_watermark(blob_service_client, project_code, run_start_utc)

    logging.info(f"{project_code}: wrote {len(rows)} rows to {blob_path}")
    return len(rows)


def _process_jira_project(kv_client: SecretClient, blob_service_client: BlobServiceClient, project: dict) -> int:
    project_code = project["ProjectCode"]
    secret_value = kv_client.get_secret(project["KeyVaultSecretName"]).value

    # Jira auth is (email, token) — stored as a single secret in "email:token"
    # format, since ProjectCredential only has one KeyVaultSecretName column.
    if ":" not in secret_value:
        raise ValueError(
            f"{project_code}: Jira secret '{project['KeyVaultSecretName']}' must be "
            f"in 'email:token' format — got a value with no ':' separator."
        )
    email, token = secret_value.split(":", 1)

    run_start_utc = datetime.now(timezone.utc)
    since_dt = _load_watermark(blob_service_client, project_code)
    if since_dt is None:
        logging.warning(f"{project_code}: no watermark found — this will be a FULL export.")

    session = requests.Session()
    session.auth = (email, token)
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    story_points_field = project.get("JiraStoryPointsField") or None

    issues = jira_exporter.fetch_issues(
        session, project["OrgOrSite"], project["SourceProjectName"], story_points_field, since_dt
    )
    if not issues:
        logging.info(f"{project_code}: no issues in this window.")
        _save_watermark(blob_service_client, project_code, run_start_utc)
        return 0

    rows = jira_exporter.build_rows(issues, project_code, project["EffortUnit"], story_points_field)
    csv_text = _rows_to_csv_text(rows, jira_exporter.STANDARD_COLUMNS)
    blob_path = _upload_csv(blob_service_client, project_code, "Jira", run_start_utc, csv_text)
    _save_watermark(blob_service_client, project_code, run_start_utc)

    logging.info(f"{project_code}: wrote {len(rows)} rows to {blob_path}")
    return len(rows)


@app.function_name(name="RunProjectExports")
@app.timer_trigger(schedule="0 0 8,12,18 * * *", arg_name="exportTimer", run_on_startup=False)
def run_project_exports(exportTimer: func.TimerRequest) -> None:
    """
    Runs at 8am, 12pm, and 6pm daily. Reads core.vw_ActiveExportableProjects
    (only complete, active projects), then loops per project with isolated
    exception handling — one client's failure never blocks the others.
    """
    credential = DefaultAzureCredential()
    kv_client = SecretClient(vault_url=KEY_VAULT_URL, credential=credential)
    blob_service_client = BlobServiceClient(account_url=LANDING_STORAGE_ACCOUNT_URL, credential=credential)

    try:
        projects = _get_active_projects(kv_client)
    except Exception as e:
        logging.error(f"Could not read active project list from SQL: {e}")
        return

    logging.info(f"Found {len(projects)} active, fully-configured project(s) to export.")

    for project in projects:
        project_code = project["ProjectCode"]
        try:
            if project["SourceSystem"] == "AzureDevOps":
                _process_devops_project(kv_client, blob_service_client, project)
            elif project["SourceSystem"] == "Jira":
                _process_jira_project(kv_client, blob_service_client, project)
            else:
                logging.warning(f"{project_code}: unknown SourceSystem '{project['SourceSystem']}', skipping.")
        except Exception as e:
            logging.error(f"{project_code}: export failed — {e}")
