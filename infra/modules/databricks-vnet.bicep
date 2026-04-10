param location string
param resourceSuffix string
param vnetCidr string = '192.168.0.0/16'

var vnetName = 'vnet-dbx-${location}-${resourceSuffix}'
var natGatewayName = 'nat-dbx-${location}-${resourceSuffix}'
var natPublicIpName = 'pip-nat-dbx-${location}-${resourceSuffix}'
var hostNsgName = 'nsg-dbx-host-${location}-${resourceSuffix}'
var containerNsgName = 'nsg-dbx-container-${location}-${resourceSuffix}'

resource natPublicIp 'Microsoft.Network/publicIPAddresses@2023-09-01' = {
  name: natPublicIpName
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
  }
}

resource natGateway 'Microsoft.Network/natGateways@2023-09-01' = {
  name: natGatewayName
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIpAddresses: [
      {
        id: natPublicIp.id
      }
    ]
  }
}

resource hostNsg 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: hostNsgName
  location: location
  properties: {}
}

resource containerNsg 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: containerNsgName
  location: location
  properties: {}
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetCidr
      ]
    }
    subnets: [
      {
        name: 'dbx-host-subnet'
        properties: {
          addressPrefix: '192.168.0.0/26'
          natGateway: {
            id: natGateway.id
          }
          networkSecurityGroup: {
            id: hostNsg.id
          }
          delegations: [
            {
              name: 'databricks-host-delegation'
              properties: {
                serviceName: 'Microsoft.Databricks/workspaces'
              }
            }
          ]
        }
      }
      {
        name: 'dbx-container-subnet'
        properties: {
          addressPrefix: '192.168.64.0/26'
          natGateway: {
            id: natGateway.id
          }
          networkSecurityGroup: {
            id: containerNsg.id
          }
          delegations: [
            {
              name: 'databricks-container-delegation'
              properties: {
                serviceName: 'Microsoft.Databricks/workspaces'
              }
            }
          ]
        }
      }
    ]
  }
}

output vnetId string = vnet.id
output vnetName string = vnet.name
output hostSubnetName string = 'dbx-host-subnet'
output containerSubnetName string = 'dbx-container-subnet'
