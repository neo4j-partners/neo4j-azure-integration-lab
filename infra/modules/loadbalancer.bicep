param location string
param resourceSuffix string
param loadBalancerCondition bool
param subnetId string
param plsSubnetId string
param plsName string = 'pls-neo4j'

var loadBalancerName = 'lb-neo4j-${location}-${resourceSuffix}'

resource loadBalancer 'Microsoft.Network/loadBalancers@2025-03-01' = if (loadBalancerCondition) {
  name: loadBalancerName
  location: location
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    backendAddressPools: [
      {
        name: 'backend'
      }
    ]
    frontendIPConfigurations: [
      {
        name: 'lbipnew'
        properties: {
          subnet: { id: subnetId }
          privateIPAllocationMethod: 'Dynamic'
        }
      }
    ]
    loadBalancingRules: [
      {
        name: 'inboundrule7474'
        properties: {
          frontendIPConfiguration: {
            id: resourceId('Microsoft.Network/loadBalancers/frontendIpConfigurations', loadBalancerName, 'lbipnew')
          }
          frontendPort: 7474
          backendPort: 7474
          enableFloatingIP: false
          idleTimeoutInMinutes: 4
          protocol: 'Tcp'
          enableTcpReset: true
          loadDistribution: 'Default'
          disableOutboundSnat: true
          backendAddressPool: {
            id: resourceId('Microsoft.Network/loadBalancers/backendAddressPools', loadBalancerName, 'backend')
          }
          backendAddressPools: [
            {
              id: resourceId('Microsoft.Network/loadBalancers/backendAddressPools', loadBalancerName, 'backend')
            }
          ]
          probe: {
            id: resourceId('Microsoft.Network/loadBalancers/probes', loadBalancerName, 'httpprobe')
          }
        }
      }
      {
        name: 'inbound7687'
        properties: {
          frontendIPConfiguration: {
            id: resourceId('Microsoft.Network/loadBalancers/frontendIpConfigurations', loadBalancerName, 'lbipnew')
          }
          frontendPort: 7687
          backendPort: 7687
          enableFloatingIP: false
          idleTimeoutInMinutes: 4
          protocol: 'Tcp'
          enableTcpReset: true
          loadDistribution: 'Default'
          disableOutboundSnat: true
          backendAddressPool: {
            id: resourceId('Microsoft.Network/loadBalancers/backendAddressPools', loadBalancerName, 'backend')
          }
          backendAddressPools: [
            {
              id: resourceId('Microsoft.Network/loadBalancers/backendAddressPools', loadBalancerName, 'backend')
            }
          ]
          probe: {
            id: resourceId('Microsoft.Network/loadBalancers/probes', loadBalancerName, 'boltprobe')
          }
        }
      }
      {
        name: 'inbound7688'
        properties: {
          frontendIPConfiguration: {
            id: resourceId('Microsoft.Network/loadBalancers/frontendIpConfigurations', loadBalancerName, 'lbipnew')
          }
          frontendPort: 7688
          backendPort: 7688
          enableFloatingIP: false
          idleTimeoutInMinutes: 4
          protocol: 'Tcp'
          enableTcpReset: true
          loadDistribution: 'Default'
          disableOutboundSnat: true
          backendAddressPool: {
            id: resourceId('Microsoft.Network/loadBalancers/backendAddressPools', loadBalancerName, 'backend')
          }
          backendAddressPools: [
            {
              id: resourceId('Microsoft.Network/loadBalancers/backendAddressPools', loadBalancerName, 'backend')
            }
          ]
          probe: {
            id: resourceId('Microsoft.Network/loadBalancers/probes', loadBalancerName, 'boltroutingprobe')
          }
        }
      }
    ]
    probes: [
      {
        name: 'httpprobe'
        properties: {
          protocol: 'Http'
          port: 7474
          requestPath: '/'
          intervalInSeconds: 5
          numberOfProbes: 1
          probeThreshold: 1
        }
      }
      {
        name: 'boltprobe'
        properties: {
          protocol: 'Http'
          port: 7474
          requestPath: '/'
          intervalInSeconds: 5
          numberOfProbes: 1
          probeThreshold: 1
        }
      }
      {
        name: 'boltroutingprobe'
        properties: {
          protocol: 'Http'
          port: 7474
          requestPath: '/'
          intervalInSeconds: 5
          numberOfProbes: 1
          probeThreshold: 1
        }
      }
    ]
  }
}

resource privateLinkService 'Microsoft.Network/privateLinkServices@2025-03-01' = if (loadBalancerCondition) {
  name: plsName
  location: location
  properties: {
    loadBalancerFrontendIpConfigurations: [
      {
        id: loadBalancer!.properties.frontendIPConfigurations[0].id
      }
    ]
    ipConfigurations: [
      {
        name: 'pls-nat-ip'
        properties: {
          subnet: { id: plsSubnetId }
          privateIPAllocationMethod: 'Dynamic'
          primary: true
        }
      }
    ]
    visibility: {
      subscriptions: ['*']
    }
    autoApproval: {
      subscriptions: []
    }
    enableProxyProtocol: false
  }
}

output loadBalancerBackendAddressPools array = loadBalancerCondition ? loadBalancer!.properties.backendAddressPools : []
output privateIpAddress string = loadBalancerCondition ? loadBalancer!.properties.frontendIPConfigurations[0].properties.privateIPAddress : ''
output privateLinkServiceId string = loadBalancerCondition ? privateLinkService!.id : ''
