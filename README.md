# func-darkspark-devops-export

Timer-triggered Azure Function (Python 3.11, Flex Consumption plan) that pulls
client DevOps/Jira PATs from Key Vault and runs the export scripts on a
schedule (08:00, 12:00, 18:00 daily).

## What's here

- `function_app.py` — timer trigger + Key Vault secret retrieval + project loop
- `requirements.txt` — Python dependencies
- `host.json` — function host config
- `local.settings.json` — local dev template (never commit real values)

## Not yet wired in

- The actual call to `devops_export_to_payload.py` / `jira_export_to_payload.py`
  is a `TODO` in `function_app.py` — drop those scripts into this folder (or a
  shared module) and import them.
- `get_active_projects()` is a hardcoded placeholder. Once `core.ProjectCredential`
  exists, replace it with a query against that table (via `vercel_report_reader`
  or a dedicated read-only export identity).

## Deployment prerequisites

1. **Create the Function App on a Flex Consumption plan**, Python 3.11 on Linux.
2. **Enable system-assigned managed identity** on the function app.
3. **Grant that identity `Get`/`List` on secrets** in `kv-darkspark-centralsystem`
   (Key Vault Access Policy or RBAC role `Key Vault Secrets User`).
4. **App setting** `KEY_VAULT_URL` — set to
   `https://kv-darkspark-centralsystem.vault.azure.net/` in the Function App's
   Configuration blade (not just local.settings.json).
5. Optional: set `WEBSITE_TIME_ZONE` if you want the schedule to run at
   8/12/6 local time instead of UTC.

## Local testing

Requires Azurite (or a real storage account) for `AzureWebJobsStorage`, and
`az login` with an identity that has Key Vault read access, since
`DefaultAzureCredential` will fall back to your local Azure CLI credentials
outside of the deployed environment.
