// Ask Your Database on Azure Container Apps.
//
// Creates two user-assigned managed identities, a Key Vault for the Claude API key,
// a Log Analytics workspace, a Container Apps environment and the app itself.
//
// Least privilege, no passwords anywhere:
//   id-<app>-app     reads the API key from Key Vault and writes the audit log (schema askdb)
//   id-<app>-reader  runs the questions' SQL: SELECT on the data schemas, nothing else
// The database rights are granted by infra/grant_sql_access.py (deploy.ps1 runs it).

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short name used to derive resource names.')
param appName string = 'askdb'

@description('Container image to run, e.g. ghcr.io/<owner>/ask-your-database:latest')
param image string

@description('Azure SQL server host name, e.g. myserver.database.windows.net')
param sqlServerHost string

@description('Azure SQL database name.')
param sqlDatabase string

@description('Comma-separated schemas the assistant may query, e.g. shop or sales,dbo')
param dataSchemas string = 'shop'

@description('Claude model id.')
param claudeModel string = 'claude-opus-5-5'

@description('Comma-separated sign-in names allowed to use the app; empty allows any account in the tenant.')
param allowedUsers string = ''

@description('Comma-separated sign-in names that may read everyone\'s audit log.')
param adminUsers string = ''

@description('Daily spending limit per user, in USD.')
param dailyBudgetUsd string = '1.00'

@description('Daily spending limit for the whole app, in USD.')
param dailyBudgetTotalUsd string = '5.00'

@description('Object id of the person deploying, given access to set the API key secret.')
param deployerObjectId string

@description('false on the first run: creates the vault so the API key can be added before the app starts.')
param deployApp bool = true

var suffix = uniqueString(resourceGroup().id)
var keyVaultName = 'kv-${appName}-${take(suffix, 6)}'

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${appName}-app'
  location: location
}

resource readerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${appName}-reader'
  location: location
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

// Built-in role ids.
var keyVaultSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'
var keyVaultSecretsOfficer = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'

resource appCanReadSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, appIdentity.id, keyVaultSecretsUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUser)
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource deployerCanSetSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, deployerObjectId, keyVaultSecretsOfficer)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsOfficer)
    principalId: deployerObjectId
    principalType: 'User'
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${appName}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: 1 } // hard cap on log ingestion cost
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${appName}'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = if (deployApp) {
  name: 'ca-${appName}'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
      '${readerIdentity.id}': {}
    }
  }
  dependsOn: [ appCanReadSecrets ]
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      secrets: [
        {
          name: 'anthropic-api-key'
          keyVaultUrl: '${vault.properties.vaultUri}secrets/anthropic-api-key'
          identity: appIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: image
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
            { name: 'CLAUDE_MODEL', value: claudeModel }
            // The data: queried as the reader identity, which can only SELECT these schemas.
            { name: 'ASKDB_DATABASE', value: 'mssql://${sqlServerHost}/${sqlDatabase}?schemas=${dataSchemas}' }
            { name: 'READER_CLIENT_ID', value: readerIdentity.properties.clientId }
            // The audit log: written as the app identity (DefaultAzureCredential uses AZURE_CLIENT_ID).
            { name: 'ASKDB_STORE', value: 'mssql://${sqlServerHost}/${sqlDatabase}' }
            { name: 'AZURE_CLIENT_ID', value: appIdentity.properties.clientId }
            { name: 'REQUIRE_READ_ONLY', value: '1' }
            { name: 'AUTH_MODE', value: 'easyauth' }
            { name: 'ALLOWED_USERS', value: allowedUsers }
            { name: 'ADMIN_USERS', value: adminUsers }
            { name: 'DAILY_BUDGET_USD', value: dailyBudgetUsd }
            { name: 'DAILY_BUDGET_TOTAL_USD', value: dailyBudgetTotalUsd }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              periodSeconds: 30
            }
            {
              type: 'Startup'
              // The first connection can wait up to a minute for an auto-paused database.
              httpGet: { path: '/healthz', port: 8000 }
              periodSeconds: 5
              failureThreshold: 30
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0 // scale to zero when nobody uses it: no cost while idle
        maxReplicas: 2
        rules: [
          {
            name: 'http'
            http: { metadata: { concurrentRequests: '20' } }
          }
        ]
      }
    }
  }
}

output appUrl string = deployApp ? 'https://${app!.properties.configuration.ingress.fqdn}' : ''
output appName string = 'ca-${appName}'
output keyVaultName string = vault.name
output appIdentityName string = appIdentity.name
output readerIdentityName string = readerIdentity.name
