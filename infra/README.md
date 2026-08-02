# Azure infrastructure

This Bicep deployment creates or updates the Makler Search platform in
resource group `MaklerApp_v2` in `westus3`:

- Linux App Service on the `F1` Free plan
- Linux Function App on the `Y1` Consumption plan
- Private listing container `maklerapp` in a `Standard_LRS` / `Hot` storage account
- Separate `Standard_LRS` / `Hot` storage account for the Function host
- System-assigned identity and `Storage Blob Data Contributor` for the Web App
- Blob RBAC scoped to the private listing container
- Key Vault-backed Entra, SMTP, and Function Storage secrets
- App Service Authentication with the supplied Entra tenant
- Monthly Resource Group budget with 80% and 100% email alerts

## Deploy

1. Ensure the target resource group exists, then copy
  `main.parameters.example.json` to a file outside source control.
2. Replace all `<...>` values. Storage, Web App, and Function names must be
   globally unique where Azure requires it.
  Set `budgetAlertEmail` to enable the `5`-currency-unit monthly budget;
  adjust `monthlyBudgetAmount` if needed.
3. Deploy with the requested subscription:

```bash
az login --tenant 6bb6fc0a-c0e2-425e-813d-0ae4d8235cd9
az account set --subscription 5b7b398e-f933-478c-8f6f-b7fa3e224df8
az deployment group create \
  --name maklerapp-v2 \
  --resource-group MaklerApp_v2 \
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
template, deploys the Bicep infrastructure to the existing resource group,
and then deploys the Python app to `maklerapp-v2` by default. The Azure CLI
uses its default incremental mode, so resources not managed by this template
are not deleted. Configure these
optional repository variables when the defaults differ:

```text
AZURE_WEBAPP_NAME=<deployed-web-app-name>
AZURE_RESOURCE_GROUP=<resource-group-name>
AZURE_LOCATION=<azure-region>
AZURE_MONTHLY_BUDGET_AMOUNT=5
AZURE_BUDGET_ALERT_EMAIL=<budget-alert-email>
AZURE_ENTRA_CLIENT_ID=<entra-client-id>
AZURE_KEY_VAULT_NAME=<globally-unique-key-vault-name>
AZURE_SMTP_HOST=smtp.gmail.com
AZURE_SMTP_PORT=587
AZURE_SMTP_USERNAME=<gmail-address>
AZURE_EMAIL_FROM=<gmail-address>
AZURE_EMAIL_RECIPIENTS=<comma-separated-5-to-10-addresses>
```

`AZURE_BUDGET_ALERT_EMAIL` must be set to create or update the Bicep-managed
budget notifications. Store `AZURE_ENTRA_CLIENT_SECRET` and
`AZURE_SMTP_PASSWORD` as GitHub Actions secrets. For Gmail,
`AZURE_SMTP_PASSWORD` must be a Google App Password, not the normal account
password.

The service principal referenced by the existing `AZUREAPPSERVICE_*` secrets
must have `Contributor` or `Website Contributor` on resource group
`MaklerApp_v2`, because the workflow runs a resource-group-scoped Bicep
deployment. Its federated GitHub credential must
match the repository, branch `main`, and workflow environment used by the
action. The OIDC login continues to use the existing tenant and subscription
secrets. A subscription-level role is not required for this workflow.

Because the template manages the Web App Blob RBAC assignment, the service
principal needs the built-in `Role Based Access Control Administrator` role
once at resource-group scope. This is narrower than `User Access
Administrator`: it grants RBAC assignment management without granting general
resource access. An owner or existing user access administrator must run this
bootstrap command:

```bash
az role assignment create \
  --assignee-object-id 78e19b89-c199-4a0e-a127-5fa2a85f84b7 \
  --assignee-principal-type ServicePrincipal \
  --role "Role Based Access Control Administrator" \
  --scope /subscriptions/5b7b398e-f933-478c-8f6f-b7fa3e224df8/resourceGroups/MaklerApp_v2
```

The GitHub service principal's normal `Contributor` role must be granted
separately at resource-group scope. It is intentionally not assigned by this
template, because assigning a role to the identity executing the deployment
creates a first-run chicken-and-egg problem. After these one-time bootstrap
assignments, rerun the GitHub Action. The Bicep deployment can then create and
maintain the Web App Blob access role.

The Function host storage uses the Function App's system-assigned managed
identity with Blob, Queue, and Table data roles. No storage account key is
placed in Function settings or Key Vault.

## Important network limitation

An F1 App Service cannot use VNet Integration. Therefore this cost-minimal
variant keeps the Storage endpoints public at the network layer while disabling
anonymous Blob access and enforcing the Web App managed identity with RBAC.
The Blob container is not public, and the app is the only identity granted
access by this template.

A Service Endpoint that restricts the Storage Account to the Web App subnet
requires VNet Integration and therefore a paid App Service plan such as B1.
That is a mutually exclusive requirement with `F1`; do not claim network-only
isolation while retaining the Free plan.
