import logging
import azure.functions as func
import requests
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.mgmt.sql import SqlManagementClient

app = func.FunctionApp()

# --- Configure these two for your tenant ---
KEY_VAULT_URL = "https://kv-darkspark-centralsystem.vault.azure.net/"
DEVOPS_ORG = "DarkSparkConsulting"
# --------------------------------------------

# --- Central SQL pause/resume config ---
SUBSCRIPTION_ID = "9af8ed4d-1ade-425e-870f-bd79eb8e00ce"
RESOURCE_GROUP = "HC_Experiments"
SQL_SERVER_NAME = "centralsystem"
SQL_DATABASE_NAME = "darkspark-central-system"
# ----------------------------------------


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
