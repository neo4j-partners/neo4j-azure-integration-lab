param location string
param resourceSuffix string
param sshSourceCidr string = 'Internet'

var networkSGName = 'nsg-neo4j-${location}-${resourceSuffix}'
var vnetName = 'vnet-neo4j-${location}-${resourceSuffix}'

resource networkSG 'Microsoft.Network/networkSecurityGroups@2025-03-01' = {
  name: networkSGName
  location: location
  properties: {
    securityRules: [
      {
        name: 'SSH'
        properties: {
          description: 'SSH'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '22'
          sourceAddressPrefix: sshSourceCidr
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 100
          direction: 'Inbound'
        }
      }
      {
        name: 'HTTPS'
        properties: {
          description: 'HTTPS'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '7473'
          sourceAddressPrefix: 'Internet'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 101
          direction: 'Inbound'
        }
      }
      {
        name: 'HTTP'
        properties: {
          description: 'HTTP'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '7474'
          sourceAddressPrefix: 'Internet'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 102
          direction: 'Inbound'
        }
      }
      {
        name: 'Bolt'
        properties: {
          description: 'Bolt'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '7687'
          sourceAddressPrefix: 'Internet'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 103
          direction: 'Inbound'
        }
      }
      {
        name: 'ClusterCommunication'
        properties: {
          description: 'Cluster communication and transaction shipping (Neo4j 5.x)'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '6000'
          sourceAddressPrefix: 'VirtualNetwork'
          destinationAddressPrefix: 'VirtualNetwork'
          access: 'Allow'
          priority: 104
          direction: 'Inbound'
        }
      }
      {
        name: 'ClusterRaft'
        properties: {
          description: 'Raft consensus protocol (Neo4j 5.x)'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '7000'
          sourceAddressPrefix: 'VirtualNetwork'
          destinationAddressPrefix: 'VirtualNetwork'
          access: 'Allow'
          priority: 105
          direction: 'Inbound'
        }
      }
      {
        name: 'BoltRouting'
        properties: {
          description: 'Bolt routing connector for cluster-aware drivers'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '7688'
          sourceAddressPrefix: 'Internet'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 106
          direction: 'Inbound'
        }
      }
      // AzureLoadBalancerProbe must be present or the Standard ILB health probes cannot reach
      // the VMSS instances. If absent the LB marks all backends unhealthy and sends TCP RST to
      // all inbound connections (enableTcpReset: true on every LB rule). This rule must be
      // explicitly re-added whenever the NSG ruleset is fully replaced (e.g. during Databricks
      // peering), because a full PUT that omits it leaves the LB probe path blocked.
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
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2025-03-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.0.0.0/8'
      ]
    }
    subnets: [
      {
        name: 'subnet'
        properties: {
          addressPrefix: '10.0.0.0/16'
          networkSecurityGroup: {
            id: networkSG.id
          }
        }
      }
      // pls-subnet must be outside the main subnet's /16 block (10.0.0.0/16). An address inside
      // that range causes Azure to reject the VNet with a subnet overlap conflict. 10.1.0.0/28
      // is within the VNet's /8 address space but clear of the /16. The /28 provides 11 usable
      // addresses — Azure recommends at least 8 NAT IPs for Private Link Service capacity.
      {
        name: 'pls-subnet'
        properties: {
          addressPrefix: '10.1.0.0/28'
          privateLinkServiceNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

output vnetId string = vnet.id
output subnetId string = vnet.properties.subnets[0].id
output nsgId string = networkSG.id
output plsSubnetId string = vnet.properties.subnets[1].id
