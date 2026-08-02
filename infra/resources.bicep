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
param backendRefreshUrl string
param emailSmtpHost string
param emailSmtpPort string
param emailSmtpUsername string
@secure()
param emailSmtpPassword string
param emailFromAddress string
param emailRecipients string

var listingsContainerName = 'maklerapp'
var listingsBlobName = 'latest.json'
var listingsContainerUrl = 'https://${listingsStorageAccount.name}.blob.core.windows.net/${listingsContainerName}'
var entraIssuer = 'https://login.microsoftonline.com/${tenantId}/v2.0'

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
      appSettings: [
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
        {
          name: 'ENTRA_CLIENT_SECRET'
          value: entraClientSecret
        }
      ]
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
        enabled: true
      }
    }
  }
}

resource webAppBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(listingsStorageAccount.id, webApp.id, 'Storage Blob Data Contributor')
  scope: listingsStorageAccount
  properties: {
    principalId: webApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
    )
  }
}

resource functionPlan 'Microsoft.Web/serverfarms@2022-09-01' = {
  name: '${functionAppName}-plan'
  location: location
  kind: 'functionapp'
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2022-09-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: functionPlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'Python|3.12'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME_VERSION'
          value: '3.12'
        }
        {
          name: 'AzureWebJobsStorage'
          value: 'DefaultEndpointsProtocol=https;AccountName=${functionStorageAccount.name};AccountKey=${listKeys(functionStorageAccount.id, functionStorageAccount.apiVersion).keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
        }
        {
          name: 'WEBSITE_RUN_FROM_PACKAGE'
          value: '1'
        }
        {
          name: 'BACKEND_REFRESH_URL'
          value: backendRefreshUrl
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
          name: 'EMAIL_SMTP_PASSWORD'
          value: emailSmtpPassword
        }
        {
          name: 'EMAIL_FROM_ADDRESS'
          value: emailFromAddress
        }
        {
          name: 'EMAIL_RECIPIENTS'
          value: emailRecipients
        }
      ]
    }
  }
}

output listingsContainerUrl string = listingsContainerUrl
output listingsStorageAccountId string = listingsStorageAccount.id
output functionAppDefaultHostName string = functionApp.properties.defaultHostName
