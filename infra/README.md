# Azure infrastructure

This Bicep deployment creates the Makler Search platform in `westus3` and in
resource group `MaklerApp_v2`:

- Linux App Service on the `F1` Free plan
- Linux Function App on the `Y1` Consumption plan
- Private listing container `maklerapp` in a `Standard_LRS` / `Hot` storage account
- Separate `Standard_LRS` / `Hot` storage account for the Function host
- System-assigned identity and `Storage Blob Data Contributor` for the Web App
- App Service Authentication with the supplied Entra tenant
- Monthly Resource Group budget with 80% and 100% email alerts

## Deploy

1. Copy `main.parameters.example.json` to a file outside source control.
2. Replace all `<...>` values. Storage, Web App, and Function names must be
   globally unique where Azure requires it.
  Set `budgetAlertEmail` to enable the `5`-currency-unit monthly budget;
  adjust `monthlyBudgetAmount` if needed.
3. Deploy with the requested subscription:

```bash
az login --tenant 6bb6fc0a-c0e2-425e-813d-0ae4d8235cd9
az account set --subscription 5b7b398e-f933-478c-8f6f-b7fa3e224df8
az deployment sub create \
  --name maklerapp-v2 \
  --location westus3 \
  --template-file infra/main.bicep \
  --parameters @infra/main.parameters.json
```

The Entra application registration must already exist. Set its supported
account type to **Accounts in this organizational directory only**. This
allows members and invited B2B guest users from this tenant, while excluding
accounts from other tenants. Configure its redirect URI as:

```text
https://<web-app-name>.azurewebsites.net/.auth/login/aad/callback
```

Only users from the configured tenant can sign in. Entra B2B guest users are
also tenant users after they have been invited and accepted. The app itself
must still be deployed after the infrastructure deployment.

## E-mail cost choice

No separate Azure mail resource is provisioned. The timer Function receives
SMTP settings through App Settings, which allows a low-cost external SMTP
relay or an existing mail provider to be used. Configure at least five and at
most ten unique valid addresses in `emailRecipients`; the Function code must
validate this before sending and should use BCC.

Do not commit `main.parameters.json`, SMTP passwords, or Entra client secrets.
Use deployment-time secure parameters or Key Vault references for production.

## GitHub Actions

The workflow in `.github/workflows/main_maklerapp.yml` validates the Bicep
template, deploys the Bicep infrastructure, and then deploys the Python app to
`maklerapp-v2` by default. The Azure CLI uses its default incremental mode, so
resources not managed by this template are not deleted. Configure these
optional repository variables when the defaults differ:

```text
AZURE_WEBAPP_NAME=<deployed-web-app-name>
AZURE_RESOURCE_GROUP=<resource-group-name>
AZURE_LOCATION=<azure-region>
AZURE_MONTHLY_BUDGET_AMOUNT=5
AZURE_BUDGET_ALERT_EMAIL=<budget-alert-email>
```

`AZURE_BUDGET_ALERT_EMAIL` must be set to create or update the Bicep-managed
budget notifications. Keep Entra client secrets and SMTP passwords in GitHub
Actions secrets, not repository variables.

The service principal referenced by the existing `AZUREAPPSERVICE_*` secrets
must have `Contributor` on the subscription because the workflow runs a
subscription-scoped Bicep deployment. Its federated GitHub credential must
match the repository, branch `main`, and workflow environment used by the
action. The OIDC login continues to use the existing tenant and subscription
secrets. A resource-group-only `Contributor` or `Website Contributor` role is
not sufficient for the infrastructure step.

## Important network limitation

An F1 App Service cannot use VNet Integration. Therefore this cost-minimal
variant keeps the Blob endpoint public at the network layer while disabling
anonymous Blob access and enforcing the Web App managed identity with RBAC.
The Blob container is not public, and the app is the only identity granted
access by this template.

A Service Endpoint that restricts the Storage Account to the Web App subnet
requires VNet Integration and therefore a paid App Service plan such as B1.
That is a mutually exclusive requirement with `F1`; do not claim network-only
isolation while retaining the Free plan.
