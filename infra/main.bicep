targetScope = 'resourceGroup'

@description('Azure region for all regional resources.')
param location string = 'westus3'

@description('Existing Entra tenant ID.')
param tenantId string = '6bb6fc0a-c0e2-425e-813d-0ae4d8235cd9'

@description('Entra application client ID used by App Service Authentication.')
param entraClientId string

@secure()
@description('Client secret for the Entra application. Store this in Key Vault for production use.')
param entraClientSecret string = ''

@description('Globally unique name for the listing storage account.')
param listingsStorageAccountName string = 'maklerappv2listings'

@description('Globally unique name for the Function host storage account.')
param functionStorageAccountName string = 'maklerappv2func'

@description('Free Linux App Service name.')
param webAppName string = 'maklerapp-v2'

@description('Linux Consumption Function App name.')
param functionAppName string = 'maklerapp-v2-timer'

@description('Globally unique Key Vault name for application secrets.')
param keyVaultName string = 'maklerappv2kv'

@description('Monthly Resource Group budget amount in the subscription billing currency.')
param monthlyBudgetAmount int = 5

@description('Email address receiving 80% and 100% budget alerts. Leave empty to disable the budget resource.')
param budgetAlertEmail string = ''

@description('Backend URL called by the timer function after deployment.')
param backendRefreshUrl string = 'https://maklerapp-v2.azurewebsites.net/internal/refresh'

@secure()
@description('Shared secret used by the timer function to call the internal refresh endpoint.')
param backendRefreshToken string

@description('Object ID of the GitHub Actions deployment service principal. It needs Blob Data Contributor on the Function host storage for RBAC package deployment.')
param deploymentPrincipalObjectId string

@description('SMTP host used by the timer function. No Azure email resource is provisioned to keep recurring costs low.')
param emailSmtpHost string = ''

@description('SMTP port used by the timer function.')
param emailSmtpPort string = '587'

@description('SMTP username used by the timer function.')
param emailSmtpUsername string = ''

@secure()
@description('SMTP password used by the timer function. Prefer Key Vault references in production.')
param emailSmtpPassword string = ''

@description('Verified sender address for the timer function.')
param emailFromAddress string = ''

@description('Comma-separated BCC recipients. The timer function must enforce 5-10 valid unique addresses.')
param emailRecipients string = ''

module platform './resources.bicep' = {
  name: 'maklerSearchPlatform'
  params: {
    location: location
    tenantId: tenantId
    entraClientId: entraClientId
    entraClientSecret: entraClientSecret
    listingsStorageAccountName: listingsStorageAccountName
    functionStorageAccountName: functionStorageAccountName
    webAppName: webAppName
    functionAppName: functionAppName
    keyVaultName: keyVaultName
    monthlyBudgetAmount: monthlyBudgetAmount
    budgetAlertEmail: budgetAlertEmail
    backendRefreshUrl: backendRefreshUrl
    backendRefreshToken: backendRefreshToken
    deploymentPrincipalObjectId: deploymentPrincipalObjectId
    emailSmtpHost: emailSmtpHost
    emailSmtpPort: emailSmtpPort
    emailSmtpUsername: emailSmtpUsername
    emailSmtpPassword: emailSmtpPassword
    emailFromAddress: emailFromAddress
    emailRecipients: emailRecipients
  }
}

output resourceGroupId string = resourceGroup().id
output webAppName string = webAppName
output functionAppName string = functionAppName
output listingsStorageAccountName string = listingsStorageAccountName
