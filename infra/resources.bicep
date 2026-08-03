targetScope = 'resourceGroup'

param location string
param tenantId string
param entraClientId string
@secure()
param entraClientSecret string
param listingsStorageAccountName string
param functionStorageAccountName string
param webAppName string
param functionAppName string
param keyVaultName string
param monthlyBudgetAmount int
param budgetAlertEmail string
param backendRefreshUrl string
@secure()
param backendRefreshToken string
param deploymentPrincipalObjectId string
param emailSmtpHost string
param emailSmtpPort string
param emailSmtpUsername string
@secure()
param emailSmtpPassword string
param emailFromAddress string
param emailRecipients string

var listingsContainerName = 'maklerapp'
var listingsBlobName = 'latest.json'
var functionDeploymentContainerName = 'function-deployments'
var listingsContainerUrl = 'https://${listingsStorageAccount.name}.blob.core.windows.net/${listingsContainerName}'
var functionDeploymentContainerUrl = 'https://${functionStorageAccount.name}.blob.core.windows.net/${functionDeploymentContainerName}'
var entraIssuer = 'https://login.microsoftonline.com/${tenantId}/v2.0'
var keyVaultReference = '@Microsoft.KeyVault(SecretUri='

resource monthlyBudget 'Microsoft.Consumption/budgets@2023-05-01' = if (!empty(budgetAlertEmail)) {
  name: 'MaklerApp-v2-monthly-limit'
  properties: {
    category: 'Cost'
    amount: monthlyBudgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: '2026-08-01T00:00:00Z'
      endDate: '2030-12-31T23:59:59Z'
    }
    notifications: {
      actual_GreaterThan_80_Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        contactEmails: [budgetAlertEmail]
      }
      actual_GreaterThan_100_Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        contactEmails: [budgetAlertEmail]
      }
    }
  }
}

resource listingsStorageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: listingsStorageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
  }
}

resource listingsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  name: '${listingsStorageAccount.name}/default/${listingsContainerName}'
  properties: {
    publicAccess: 'None'
  }
}

resource functionStorageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: functionStorageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
  }
}

resource functionStorageBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  name: '${functionStorageAccount.name}/default'
}

resource functionStorageQueueService 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  name: '${functionStorageAccount.name}/default'
}

resource functionStorageTableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  name: '${functionStorageAccount.name}/default'
}

resource functionDeploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  name: '${functionStorageAccount.name}/default/${functionDeploymentContainerName}'
  properties: {
    publicAccess: 'None'
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Enabled'
  }
}

resource entraClientSecretValue 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(entraClientSecret)) {
  name: 'entra-client-secret'
  parent: keyVault
  properties: {
    value: entraClientSecret
  }
}

resource smtpPasswordValue 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(emailSmtpPassword)) {
  name: 'smtp-password'
  parent: keyVault
  properties: {
    value: emailSmtpPassword
  }
}

resource backendRefreshTokenValue 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(backendRefreshToken)) {
  name: 'backend-refresh-token'
  parent: keyVault
  properties: {
    value: backendRefreshToken
  }
}

resource webPlan 'Microsoft.Web/serverfarms@2022-09-01' = {
  name: '${webAppName}-plan'
  location: location
  kind: 'linux'
  sku: {
    name: 'F1'
    tier: 'Free'
  }
  properties: {
    reserved: true
  }
}

resource webApp 'Microsoft.Web/sites@2022-09-01' = {
  name: webAppName
  location: location
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: webPlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.14'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      alwaysOn: false
      appSettings: concat([
        {
          name: 'LISTINGS_BLOB_ENABLED'
          value: 'true'
        }
        {
          name: 'LISTINGS_BLOB_CONTAINER_URL'
          value: listingsContainerUrl
        }
        {
          name: 'LISTINGS_BLOB_NAME'
          value: listingsBlobName
        }
      ], !empty(entraClientSecret) ? [{
        name: 'ENTRA_CLIENT_SECRET'
        value: '${keyVaultReference}${entraClientSecretValue!.properties.secretUriWithVersion})'
      }] : [], !empty(backendRefreshToken) ? [{
        name: 'INTERNAL_REFRESH_TOKEN'
        value: '${keyVaultReference}${backendRefreshTokenValue!.properties.secretUriWithVersion})'
      }] : [])
    }
  }
}

resource webAppAuth 'Microsoft.Web/sites/config@2022-09-01' = if (!empty(entraClientId)) {
  name: 'authsettingsV2'
  parent: webApp
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      excludedPaths: [
        '/internal/refresh'
      ]
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraClientId
          clientSecretSettingName: 'ENTRA_CLIENT_SECRET'
          openIdIssuer: entraIssuer
        }
        validation: {
          allowedAudiences: [
            entraClientId
            'api://${entraClientId}'
          ]
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
}

resource webAppBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(listingsContainer.id, webApp.id, 'Storage Blob Data Contributor')
  scope: listingsContainer
  properties: {
    principalId: webApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
    )
  }
}

resource functionPlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${functionAppName}-plan'
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: functionPlan.id
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: functionDeploymentContainerUrl
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 512
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: concat([
        {
          name: 'AzureWebJobsStorage__accountName'
          value: functionStorageAccount.name
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'AzureWebJobsStorage__blobServiceUri'
          value: 'https://${functionStorageAccount.name}.blob.core.windows.net'
        }
        {
          name: 'AzureWebJobsStorage__queueServiceUri'
          value: 'https://${functionStorageAccount.name}.queue.core.windows.net'
        }
        {
          name: 'AzureWebJobsStorage__tableServiceUri'
          value: 'https://${functionStorageAccount.name}.table.core.windows.net'
        }
        {
          name: 'BACKEND_REFRESH_URL'
          value: backendRefreshUrl
        }
        {
          name: 'INTERNAL_REFRESH_TOKEN'
          value: '${keyVaultReference}${backendRefreshTokenValue!.properties.secretUriWithVersion})'
        }
        {
          name: 'AUTO_REFRESH_TIME_ZONE'
          value: 'Europe/Berlin'
        }
        {
          name: 'EMAIL_SMTP_HOST'
          value: emailSmtpHost
        }
        {
          name: 'EMAIL_SMTP_PORT'
          value: emailSmtpPort
        }
        {
          name: 'EMAIL_SMTP_USERNAME'
          value: emailSmtpUsername
        }
        {
          name: 'EMAIL_FROM_ADDRESS'
          value: emailFromAddress
        }
        {
          name: 'EMAIL_RECIPIENTS'
          value: emailRecipients
        }
      ], !empty(emailSmtpPassword) ? [{
        name: 'EMAIL_SMTP_PASSWORD'
        value: '${keyVaultReference}${smtpPasswordValue!.properties.secretUriWithVersion})'
      }] : [])
    }
  }
  dependsOn: [
    functionDeploymentContainer
  ]
}

resource functionBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(functionStorageAccount.id, functionApp.id, 'Storage Blob Data Owner')
  scope: functionStorageAccount
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
    )
  }
}

resource functionDeploymentBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(functionStorageAccount.id, deploymentPrincipalObjectId, 'Storage Blob Data Contributor')
  scope: functionStorageAccount
  properties: {
    principalId: deploymentPrincipalObjectId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
    )
  }
}

resource functionQueueRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(functionStorageAccount.id, functionApp.id, 'Storage Queue Data Contributor')
  scope: functionStorageAccount
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
    )
  }
}

resource functionTableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(functionStorageAccount.id, functionApp.id, 'Storage Table Data Contributor')
  scope: functionStorageAccount
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
    )
  }
}

resource webAppKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, webApp.id, 'Key Vault Secrets User')
  scope: keyVault
  properties: {
    principalId: webApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource functionKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, functionApp.id, 'Key Vault Secrets User')
  scope: keyVault
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

output listingsContainerUrl string = listingsContainerUrl
output listingsStorageAccountId string = listingsStorageAccount.id
output functionAppDefaultHostName string = functionApp.properties.defaultHostName
