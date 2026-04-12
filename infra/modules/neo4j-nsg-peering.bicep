param nsgName string
param location string
param databricksCidr string
param sshSourceCidr string = 'Internet'

resource networkSG 'Microsoft.Network/networkSecurityGroups@2025-03-01' = {
  name: nsgName
  location: location
  properties: {
    // Every Neo4j port rule in this module — 7473, 7474, 7687, 7688 — must reference
    // databricksCidr, not 'Internet'. After the peering deployment all four ports are
    // private-only; a hardcoded 'Internet' source on any one of them leaves that port
    // publicly reachable and defeats the purpose of the peering NSG. Port 7473 (HTTPS)
    // was originally misconfigured with 'Internet' — do not revert it.
    //
    // AzureLoadBalancerProbe must be present or the Standard ILB health probes cannot reach
    // the VMSS instances. If absent the LB marks all backends unhealthy and RSTs every
    // inbound connection. This rule must survive every full NSG replacement.
    securityRules: [
      {
        name: 'SSH'
        properties: {
          protocol: 'Tcp'
          destinationPortRange: '22'
          sourceAddressPrefix: sshSourceCidr
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
