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
# Azure DevOps Area Path mappings for OTS projects
DEVOPS_AREA_PATHS = {
    "OTSG-SALE": "OTS\\Sales Data Support",
    "OTSG-FINC": "OTS\\Finance Data Support",
}
# --------------------------------------------

# --- Central SQL pause/resume config ---
SUBSCRIPTION_ID = "9af8ed4d-1ade-425e-870f-bd79eb8e00ce"
RESOURCE_GROUP = "DSPR"
SQL_SERVER_NAME = "centralsystem"
SQL_DATABASE_NAME = "DSPR-PROD-01"
SQL_RECONCILE_WRITER_USER = "func_project_reconcile_writer"
SQL_RECONCILE_WRITER_PASSWORD_SECRET_NAME = "sql-func-project-reconcile-writer-password"
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

def _get_sql_client() -> SqlManagementClient:
    credential = DefaultAzureCredential()
    return SqlManagementClient(credential, SUBSCRIPTION_ID)

def _connect_sql(kv_client: SecretClient, user: str, password_secret_name: str) -> pyodbc.Connection:
    password = kv_client.get_secret(password_secret_name).value
    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{SQL_SERVER_HOST},1433;"
        f"Database={SQL_DATABASE_NAME};"
        f"Uid={user};"
        f"Pwd={password};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60;"
    )
    last_error = None
    for attempt in range(1, 4):
        try:
            return pyodbc.connect(conn_str)
        except pyodbc.Error as e:
            last_error = e
            logging.warning(f"SQL connection attempt {attempt}/3 failed ({user}): {e}")
            if attempt < 3:
                time.sleep(15 * attempt)
    raise last_error


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

