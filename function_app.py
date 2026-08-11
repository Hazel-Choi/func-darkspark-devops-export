import logging
import os

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

app = func.FunctionApp()

KEY_VAULT_URL = os.environ.get(
    "KEY_VAULT_URL", "https://kv-darkspark-centralsystem.vault.azure.net/"
)

# NCRONTAB format: {second} {minute} {hour} {day} {month} {day-of-week}
# Runs daily at 08:00, 12:00, and 18:00 (function app timezone = UTC by default —
# set WEBSITE_TIME_ZONE app setting if you want local time instead).
SCHEDULE = "0 0 8,12,18 * * *"


def get_secret(secret_name: str) -> str:
    """Fetch a secret from Key Vault using the function app's managed identity."""
    credential = DefaultAzureCredential()
    client = SecretClient(vault_url=KEY_VAULT_URL, credential=credential)
    return client.get_secret(secret_name).value


# --- Project registry -------------------------------------------------------
# Placeholder until core.ProjectCredential exists. Each entry maps a project
# key to the Key Vault secret name holding its PAT. Once the table is built,
# this function should query it instead of using a hardcoded list.
def get_active_projects() -> list[dict]:
    return [
        {"project_key": "ZEUS-DAAC", "secret_name": "devops-pat-zeus"},
        # {"project_key": "POOK-OSDM", "secret_name": "devops-pat-pooky"},
    ]


@app.timer_trigger(
    schedule=SCHEDULE,
    arg_name="myTimer",
    run_on_startup=False,
    use_monitor=True,
)
def devops_export_timer(myTimer: func.TimerRequest) -> None:
    if myTimer.past_due:
        logging.warning("Timer is past due — a scheduled run was missed or delayed.")

    logging.info("Starting scheduled DevOps export run.")

    projects = get_active_projects()

    for project in projects:
        project_key = project["project_key"]
        try:
            pat = get_secret(project["secret_name"])
            logging.info("Retrieved PAT for %s from Key Vault.", project_key)

            # TODO: wire in the real export logic, e.g.:
            # from devops_export_to_payload import run_export
            # run_export(pat=pat, project_key=project_key)

            logging.info("Export completed for %s.", project_key)

        except Exception:
            # Log and continue so one project's failure doesn't block the rest.
            logging.exception("Export failed for project %s", project_key)

    logging.info("DevOps export run complete.")
