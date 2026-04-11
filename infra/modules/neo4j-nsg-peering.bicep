param nsgName string
param location string
param databricksCidr string

resource networkSG 'Microsoft.Network/networkSecurityGroups@2025-03-01' = {
  name: nsgName
  location: location
  properties: {
    securityRules: [
      {
        name: 'SSH'
        properties: {
          protocol: 'Tcp'
          destinationPortRange: '22'
          sourceAddressPrefix: 'Internet'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 100
          direction: 'Inbound'
          sourcePortRange: '*'
        }
      }
      {
        name: 'HTTPS'
        properties: {
          protocol: 'Tcp'
          destinationPortRange: '7473'
          sourceAddressPrefix: databricksCidr
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 101
          direction: 'Inbound'
          sourcePortRange: '*'
        }
      }
      {
        name: 'HTTP'
        properties: {
          protocol: 'Tcp'
          destinationPortRange: '7474'
          sourceAddressPrefix: databricksCidr
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 102
          direction: 'Inbound'
          sourcePortRange: '*'
        }
      }
      {
        name: 'Bolt'
        properties: {
          protocol: 'Tcp'
          destinationPortRange: '7687'
          sourceAddressPrefix: databricksCidr
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 103
          direction: 'Inbound'
          sourcePortRange: '*'
        }
      }
      {
        name: 'ClusterCommunication'
        properties: {
          protocol: 'Tcp'
          destinationPortRange: '6000'
          sourceAddressPrefix: 'VirtualNetwork'
          destinationAddressPrefix: 'VirtualNetwork'
          access: 'Allow'
          priority: 104
          direction: 'Inbound'
          sourcePortRange: '*'
        }
      }
      {
        name: 'ClusterRaft'
        properties: {
          protocol: 'Tcp'
          destinationPortRange: '7000'
          sourceAddressPrefix: 'VirtualNetwork'
          destinationAddressPrefix: 'VirtualNetwork'
          access: 'Allow'
          priority: 105
          direction: 'Inbound'
          sourcePortRange: '*'
        }
      }
      {
        name: 'BoltRouting'
        properties: {
          protocol: 'Tcp'
          destinationPortRange: '7688'
          sourceAddressPrefix: databricksCidr
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 106
          direction: 'Inbound'
          sourcePortRange: '*'
        }
      }
      {
        name: 'AzureLoadBalancerProbe'
        properties: {
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '*'
          sourceAddressPrefix: 'AzureLoadBalancer'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 110
          direction: 'Inbound'
        }
      }
      {
        name: 'DenyDatabricks'
        properties: {
          protocol: '*'
          destinationPortRange: '*'
          sourceAddressPrefix: databricksCidr
          destinationAddressPrefix: '*'
          access: 'Deny'
          priority: 200
          direction: 'Inbound'
          sourcePortRange: '*'
        }
      }
    ]
  }
}
