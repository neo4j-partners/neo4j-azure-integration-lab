param workspaceName string
param location string
param managedResourceGroupId string
param vnetId string
param hostSubnetName string
param containerSubnetName string

resource databricksWorkspace 'Microsoft.Databricks/workspaces@2024-05-01' = {
  name: workspaceName
  location: location
  sku: {
    name: 'premium'
  }
  properties: {
    managedResourceGroupId: managedResourceGroupId
    parameters: {
      customVirtualNetworkId: {
        value: vnetId
      }
      customPublicSubnetName: {
        value: hostSubnetName
      }
      customPrivateSubnetName: {
        value: containerSubnetName
      }
      enableNoPublicIp: {
        value: true
      }
    }
  }
}

output workspaceId string = databricksWorkspace.id
output workspaceUrl string = databricksWorkspace.properties.workspaceUrl
