targetScope = 'subscription'

param location string
param neo4jResourceGroup string
param neo4jVnetId string
param neo4jNsgName string
param databricksResourceGroup string
param databricksWorkspaceName string = 'neo4j-dbx'
param databricksVnetCidr string = '192.168.0.0/16'
@description('Source CIDR for the SSH NSG rule. Defaults to Internet (open). Restrict to a known range for production.')
param sshSourceCidr string = 'Internet'

// Step 1: Create the Databricks resource group
resource databricksRg 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: databricksResourceGroup
  location: location
}

// Step 2: Compute local variables
var resourceSuffix = uniqueString(subscription().subscriptionId, databricksResourceGroup)
var managedRgId = subscriptionResourceId('Microsoft.Resources/resourceGroups', '${databricksResourceGroup}-managed')
var neo4jVnetName = last(split(neo4jVnetId, '/'))

// Step 3: Deploy Databricks VNet
module dbxVnet 'modules/databricks-vnet.bicep' = {
  name: 'dbx-vnet'
  scope: databricksRg
  dependsOn: [databricksRg]
  params: {
    location: location
    resourceSuffix: resourceSuffix
    vnetCidr: databricksVnetCidr
  }
}

// Step 4: Deploy Databricks Workspace
module dbxWorkspace 'modules/databricks-workspace.bicep' = {
  name: 'dbx-workspace'
  scope: databricksRg
  dependsOn: [databricksRg]
  params: {
    workspaceName: databricksWorkspaceName
    location: location
    managedResourceGroupId: managedRgId
    vnetId: dbxVnet.outputs.vnetId
    hostSubnetName: dbxVnet.outputs.hostSubnetName
    containerSubnetName: dbxVnet.outputs.containerSubnetName
  }
}

// Step 5: Peering DBX -> Neo4j
module peeringDbxToNeo4j 'modules/vnet-peering.bicep' = {
  name: 'peering-dbx-to-neo4j'
  scope: resourceGroup(databricksResourceGroup)
  dependsOn: [dbxWorkspace]
  params: {
    localVnetName: dbxVnet.outputs.vnetName
    remoteVnetId: neo4jVnetId
    peeringName: 'dbx-to-neo4j'
  }
}

// Step 6: Peering Neo4j -> DBX
module peeringNeo4jToDbx 'modules/vnet-peering.bicep' = {
  name: 'peering-neo4j-to-dbx'
  scope: resourceGroup(neo4jResourceGroup)
  dependsOn: [dbxWorkspace]
  params: {
    localVnetName: neo4jVnetName
    remoteVnetId: dbxVnet.outputs.vnetId
    peeringName: 'neo4j-to-dbx'
  }
}

// Step 7: NSG update (runs in parallel with workspace)
module nsgUpdate 'modules/neo4j-nsg-peering.bicep' = {
  name: 'nsg-update'
  scope: resourceGroup(neo4jResourceGroup)
  params: {
    nsgName: neo4jNsgName
    location: location
    databricksCidr: databricksVnetCidr
    sshSourceCidr: sshSourceCidr
  }
}

output databricksWorkspaceUrl string = dbxWorkspace.outputs.workspaceUrl
output databricksVnetId string = dbxVnet.outputs.vnetId
