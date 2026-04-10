# Private Databricks-to-Neo4j Connectivity on Azure

## Overview

This architecture establishes a private network path between a Databricks analytics workspace and a Neo4j graph database on Azure. Databricks cluster nodes reach Neo4j through a bidirectional VNet peering connection that routes all traffic between the two systems directly over the Microsoft backbone. The Neo4j database ports accept inbound connections from the Databricks address space only. Cluster nodes carry no public IP addresses. Every component spans two customer-owned resource groups and is orchestrated through a single subscription-scoped deployment.

The design centers on VNet injection: placing the Databricks compute plane into a customer-managed Virtual Network rather than the Microsoft-managed default. That single decision is what makes standard Azure VNet peering possible between the two networks. The peering creates the private route; an NSG update on the Neo4j side defines exactly which ports are accessible across it; and a NAT gateway gives cluster nodes the outbound path they need to reach the Databricks control plane.

**Major components:**

- A customer-managed Azure Virtual Network (192.168.0.0/16) with two dedicated subnets and empty NSGs on each, satisfying Azure's subnet delegation requirements for Databricks
- A NAT gateway (Standard SKU, static public IP) attached to both subnets, providing mandatory outbound internet access for the Databricks control plane
- A Databricks workspace (Standard SKU) deployed with VNet injection into the customer-managed VNet, with Secure Cluster Connectivity enabled so cluster nodes carry no public IP addresses
- Bidirectional VNet peering connecting the Neo4j VNet (10.0.0.0/8) and the Databricks VNet (192.168.0.0/16) across two resource groups in the same subscription and region
- An NSG update on the Neo4j side that replaces the Internet-open inbound rules for ports 7473, 7474, 7687, and 7688 with rules scoped to the Databricks CIDR, plus a deny-all rule from that CIDR at priority 200

The result is a Databricks cluster that can open a Neo4j Bolt session over a private IP address with all traffic traveling on the Microsoft network backbone.

---

## Topology

![Azure Subscription: Neo4j Resource Group and Databricks Resource Group connected by bidirectional VNet Peering, with Databricks Managed Resource Group below](p2p-dbx.png)

The diagram shows the subscription boundary containing three resource groups. The Neo4j resource group holds the VMSS, its VNet, and the NSG. The Databricks resource group holds the customer-managed VNet, the NAT gateway, and the workspace resource. A third resource group, created automatically by the Databricks service, holds the cluster VMs; this group is not directly accessible. Two VNet peering connections bridge the Neo4j and Databricks VNets: one peering flows from the Databricks side toward Neo4j, and the other flows from the Neo4j side back toward Databricks. Both must be present and in the Connected state before cluster traffic can reach the Neo4j subnet.

---

## Resource Group and NSG Structure

The tree below maps each Azure resource to its resource group and shows the NSG rules that govern traffic at the Neo4j subnet boundary. Reading it top to bottom traces the path a packet takes from a Databricks cluster node: it leaves the container subnet in the Databricks resource group, crosses the peering connection at the bottom of the tree, and arrives at the Neo4j subnet where the NSG rules determine whether it is permitted to reach the VMSS.

```
Azure Subscription
│
├── Resource Group: neo4j-rg
│   ├── VNet  (e.g. 10.0.0.0/8)
│   │   └── Subnet  (e.g. 10.0.0.0/16)
│   │       └── Neo4j VMSS
│   └── NSG  (attached to Neo4j subnet)
│       ├── Allow  7687, 7473, 7474, 7688  from Databricks CIDR  (priority 101-106)
│       ├── Deny   all                      from Databricks CIDR  (priority 200)
│       ├── Allow  22                       from Internet
│       └── Allow  6000, 7000              from VirtualNetwork
│
├── Resource Group: databricks-rg
│   ├── VNet  (e.g. 192.168.0.0/16)
│   │   ├── Subnet host       (e.g. 192.168.0.0/26)   + NSG (Databricks-managed)
│   │   └── Subnet container  (e.g. 192.168.64.0/26)  + NSG (Databricks-managed)
│   ├── NAT Gateway  (Standard SKU, static public IP)
│   └── Databricks Workspace  (Standard SKU, VNet injection, SCC enabled)
│
├── Resource Group: databricks-rg-managed  (Databricks-owned, not user-accessible)
│   └── Cluster VMs  (no public IPs)
│
└── VNet Peering  (bidirectional)
    ├── databricks-rg VNet  →  neo4j-rg VNet      [Connected]
    └── neo4j-rg VNet       →  databricks-rg VNet  [Connected]
```

The NSG on the Neo4j subnet is the primary access control boundary. The allow rules open the four Neo4j ports to the Databricks CIDR. The deny rule at priority 200 blocks all other traffic from that CIDR before the default `VirtualNetwork` allow rule at priority 65000 can pick it up. SSH on port 22 stays open to the Internet source tag for administrative access; the cluster inter-node ports (6000 and 7000) use the `VirtualNetwork` tag, which automatically covers the peered Databricks address space once both peering connections reach the Connected state.

---

## What Does Not Work

### NSG Rules Filter Traffic, They Do Not Route It

An NSG sits at the subnet boundary and evaluates packets that arrive there. It has no effect on routing. Two Azure VNets are isolated network boundaries; without a peering connection, traffic between them travels the public internet. A Databricks cluster node attempting to reach a Neo4j node sends packets out through the NAT gateway, across the public internet, and in through the Neo4j VM's public IP. The Neo4j NSG sees a public source address, not a Databricks CIDR, so any inbound rule scoped to the Databricks private range never matches.

VNet peering creates the route that makes private-CIDR NSG rules meaningful. Without it, those rules are unreachable. The architecture requires both: the peering establishes the path, and the NSG rules define which ports are permitted across it.

### Private Connectivity Requires VNet Injection at Workspace Creation

Standard VNet peering requires both VNets to be customer-owned. A Databricks workspace deployed with default settings places its compute plane in a Microsoft-managed VNet that is not accessible for standard peering. The only supported path to customer-controlled peering is VNet injection, which directs the workspace to place cluster VMs into a VNet the customer owns.

VNet injection must be specified when the workspace is created. Azure provides no migration path from a non-injected workspace. An existing workspace cannot be updated to use VNet injection; it must be deleted and recreated. The subnet CIDR blocks are equally fixed and cannot be changed after the workspace is deployed. If the Databricks VNet address space overlaps with the Neo4j VNet, peering fails, and the only resolution is to recreate both the VNet and workspace with a non-overlapping range. The Databricks VNet address space must also fall within the /16 to /24 range that Azure requires for VNet injection; a /16 leaves sufficient room for large clusters.

VNet injection also carries a same-subscription requirement: the customer-managed VNet must be in the same Azure subscription as the workspace. Cross-subscription VNet injection is not supported. Similarly, both VNets must be in the same Azure region; global (cross-region) VNet peering is supported by Azure but incurs additional cost and higher latency.

Private connectivity between Databricks and Neo4j is a day-zero decision. The VNet, subnets, NAT gateway, and workspace must all be deployed with the correct configuration before any clusters are created.

---

## Architecture in Detail

### Customer-Managed VNet

The Databricks VNet (192.168.0.0/16) contains two subnets with fixed roles. The host subnet (192.168.0.0/26) receives the Databricks driver nodes. The container subnet (192.168.64.0/26) receives the worker nodes. Both must be at least /26, and no other Azure resource may share either subnet. Azure enforces this through subnet delegation: once a subnet is delegated to `Microsoft.Databricks/workspaces`, only Databricks resources may land in it.

The delegation also means that Databricks manages the NSG rules on both subnets automatically. The service adds and modifies rules through the delegation mechanism to maintain cluster communication. However, Azure requires that an NSG resource be attached to each subnet at the time the workspace is created, even though the workspace will subsequently manage its own rules on those NSGs. Deploying the workspace without attached NSGs produces a `SubnetMissingNSG` error. The Bicep template creates two empty NSGs, attaches them, and leaves their rule management to the Databricks service thereafter.

The VNet address space (192.168.0.0/16) must not overlap with the Neo4j VNet (10.0.0.0/8). These ranges are non-overlapping. VNet peering fails if the address spaces intersect, and there is no way to change a VNet's address space without deleting and recreating it along with all dependent resources.

### NAT Gateway

After March 31, 2026, new Azure VNets no longer provide default outbound internet access. Cluster nodes need to reach the Databricks control plane endpoints to receive job instructions, register heartbeats, and download libraries. Without outbound access, clusters fail to start.

A NAT gateway (Standard SKU, static public IP) attached to both the host and container subnets provides this outbound path. All cluster-initiated outbound traffic routes through the NAT gateway's static IP before leaving the Azure region. Inbound connections to cluster nodes from outside the VNet are still blocked; the NAT gateway only handles outbound flows. This is the correct posture for nodes with Secure Cluster Connectivity enabled.

### Databricks Workspace with VNet Injection and Secure Cluster Connectivity

The workspace resource specifies `customVirtualNetworkId` pointing at the customer-managed VNet and names the host and container subnets by their resource names. These parameters direct Azure to place all cluster VMs into the customer-managed VNet rather than a Microsoft-managed one.

Secure Cluster Connectivity (also called No Public IP) removes public IP addresses from cluster nodes entirely. All communication from the Databricks control plane to cluster nodes flows over the Azure backbone through a relay service. Cluster nodes initiate the secure tunnel to the control plane outbound; inbound control traffic never touches a public address. The NAT gateway provides the outbound path for this tunnel. The Standard SKU workspace supports both VNet injection and Secure Cluster Connectivity, and both are active in this deployment.

The workspace itself creates a third resource group, named with a `-managed` suffix, to hold the cluster VMs. This group is owned and controlled by the Databricks service and is separate from the resource group where the workspace resource lives.

### Bidirectional VNet Peering

A single peering connection covers only one direction. A peering from the Databricks VNet to the Neo4j VNet puts the Databricks VNet in the Initiated state; Neo4j traffic cannot yet reach the Databricks VNet. Both peering connections must exist and both must show Connected before packets flow in either direction.

Because this deployment uses VNet injection, both VNets are customer-owned. The peering uses standard `Microsoft.Network/virtualNetworks/virtualNetworkPeerings` resources on each side. The `Microsoft.Databricks/workspaces/virtualNetworkPeerings` sub-resource is for the managed (non-injected) case where the workspace object proxies the peering into the Microsoft-managed VNet; it does not apply here.

The peering connections span two resource groups within the same subscription and region. The Bicep orchestrator template runs at subscription scope so it can write into both resource groups in a single deployment. After both connections reach Connected, the Neo4j VNet's `VirtualNetwork` source tag in existing NSG rules also covers the peered Databricks address space (192.168.0.0/16). The cluster inter-node ports (6000 and 7000) therefore do not require separate rule changes; they inherit coverage through the expanded VirtualNetwork tag.

### NSG Update on the Neo4j Side

The original Neo4j NSG opens the Bolt port (7687), the browser ports (7473 and 7474), and the routing connector port (7688) to the Internet source tag. Those rules accept connections from any public IP. After the peering is in place, those rules are replaced with rules that accept connections only from 192.168.0.0/16.

A deny-all rule from 192.168.0.0/16 at priority 200 sits below the allow rules for the Neo4j ports. Its purpose is to block inbound access from Databricks cluster nodes to any port on the Neo4j subnet other than the four Neo4j ports explicitly opened. Without this rule, any Databricks cluster node could reach any open port on the Neo4j VMs. The allow rules sit at priorities 101 through 106; the deny rule at 200 catches everything else from the peered range before the default VirtualNetwork allow rule at priority 65000 picks it up.

SSH (port 22) remains open to the Internet source tag so that administrative access to the Neo4j VMs does not depend on the Databricks peering being active. This is acceptable for a demonstration deployment; a production hardening would restrict SSH to a jump host or VPN.

After this NSG update, the browser UI at port 7474 and the Bolt endpoint at port 7687 are no longer reachable from the public internet. Access from a local workstation requires an SSH tunnel through the Neo4j VM.

---

## The Private Path in Practice

Once both peering connections show Connected and the NSG update is in place, a Databricks notebook running on any cluster in the workspace can open a Neo4j driver session using the Neo4j VMSS's private IP address. The driver targets `bolt://<neo4j-private-ip>:7687`. The packet leaves the cluster node's container subnet, traverses the VNet peering, arrives at the Neo4j subnet, passes the NSG inbound check (the source address falls within the Databricks CIDR, which the allow rule for port 7687 covers), and reaches the Neo4j process on the VM.

The full round-trip stays on the Microsoft network backbone. The NSG at the Neo4j subnet boundary is the only access control gate between the cluster and the database.

`driver.verify_connectivity()` succeeds over this path, and Cypher queries execute against the graph from the notebook without any traffic leaving the Microsoft network. The Neo4j private IP is the internal address assigned to the VMSS instance within its subnet; it is visible through `az vmss nic list` against the Neo4j resource group.

---

## Reference

**Virtual Network (VNet).** An isolated private network within Azure, defined by an IP address space in CIDR notation. Resources inside a VNet communicate with each other directly over private IPs. Resources in different VNets cannot communicate unless a peering or gateway connection bridges the two. A VNet's address space cannot be changed without recreating it.

**Subnet.** A range of IP addresses carved from a VNet's address space. Each subnet occupies a non-overlapping portion of the VNet CIDR. Resources like VMs and scale sets are placed in subnets, and NSGs are attached to subnets to filter inbound and outbound traffic at that boundary.

**VNet Peering.** A connection between two Azure Virtual Networks that routes traffic between them directly over the Microsoft backbone without traversing the public internet. Peering is not transitive: if VNet A is peered with VNet B and VNet B is peered with VNet C, traffic from VNet A cannot reach VNet C through VNet B. Each peering connection covers one direction; establishing private communication between two VNets requires one peering object on each side, both of which must reach the Connected state.

**VNet Injection.** A Databricks deployment mode in which the workspace places its cluster VMs into a customer-owned VNet rather than a Microsoft-managed one. VNet injection enables standard Azure VNet peering into the Databricks compute plane, which is what makes private connectivity from external networks possible. It must be configured at workspace creation and cannot be added to an existing workspace.

**Subnet Delegation.** An Azure mechanism that associates a subnet with a specific service, in this case `Microsoft.Databricks/workspaces`. Delegation restricts the subnet so that only resources from the delegated service can be placed in it, and it grants the service permission to modify NSG rules on the subnet automatically. Databricks uses delegation to manage the inbound and outbound rules that cluster communication requires.

**Network Security Group (NSG).** An Azure resource containing ordered inbound and outbound security rules. Each rule specifies a source address, destination address, port, protocol, and action (Allow or Deny). Rules are evaluated in priority order, lowest number first, and the first matching rule wins. An NSG controls traffic at the subnet boundary; it filters packets that arrive at or depart from the subnet, but it does not affect routing decisions. An NSG rule alone cannot route traffic between two VNets.

**NAT Gateway.** An Azure resource that provides outbound internet connectivity for resources in a subnet that carry no public IP address. Resources send outbound packets to the NAT gateway, which translates the source address to its own static public IP before forwarding the packet to the internet. Inbound connections initiated from the internet cannot traverse a NAT gateway. After March 31, 2026, new Azure VNets no longer provide default outbound internet access; a NAT gateway is required for any new subnet whose resources need to reach external endpoints.

**Secure Cluster Connectivity (SCC).** A Databricks workspace configuration (also called No Public IP or NPIP) in which cluster VMs are assigned no public IP addresses. All control-plane communication flows through a Databricks relay service over a secure tunnel that the cluster node initiates outbound. Inbound connections from the Databricks control plane never traverse a public network path. SCC requires a NAT gateway or equivalent outbound path for the cluster nodes to reach the Databricks relay endpoints.

**Bolt Protocol.** The binary wire protocol used by Neo4j drivers to communicate with Neo4j instances. Bolt runs over TCP on port 7687 by default. The `bolt://` URI scheme establishes a direct connection to a single Neo4j instance; the `neo4j://` URI scheme requests a routing table from the server and distributes reads and writes across cluster members. For a standalone single-node deployment, `bolt://` is appropriate. For a multi-node cluster behind a load balancer, `neo4j://` pointing at the load balancer's private frontend IP lets the driver discover and use all cluster members.

**Bicep Subscription Scope.** Azure Bicep templates normally deploy resources into a single resource group. Setting `targetScope = 'subscription'` at the top of a Bicep file allows the template to create resource groups and deploy resources across multiple resource groups in a single operation. The Databricks orchestrator template runs at subscription scope so it can create the Databricks resource group, deploy all Databricks infrastructure into it, and write both peering connections (one into the Databricks VNet and one into the Neo4j VNet) in a single coordinated deployment.

---

## Bicep Template Reference

The deployment is split across one orchestrator template and four modules. All files live under `infra/`.

### `databricks-main.bicep` — Orchestrator

Entry point for the entire Databricks peering deployment. Runs at subscription scope (`targetScope = 'subscription'`) so it can create the Databricks resource group and write resources into both the Databricks and Neo4j resource groups in a single deployment.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `location` | required | Azure region for all resources |
| `neo4jResourceGroup` | required | Name of the existing Neo4j resource group |
| `neo4jVnetId` | required | Full resource ID of the existing Neo4j VNet |
| `neo4jNsgName` | required | Name of the existing Neo4j NSG to update |
| `databricksResourceGroup` | required | Name of the new resource group to create for Databricks |
| `databricksWorkspaceName` | `neo4j-dbx` | Name for the Databricks workspace resource |
| `databricksVnetCidr` | `192.168.0.0/16` | Address space for the Databricks VNet |

Outputs: `databricksWorkspaceUrl`, `databricksVnetId`

Deployment sequence: creates the Databricks resource group, then deploys the VNet and workspace modules in parallel (both scoped to the Databricks resource group), then deploys both peering connections once the workspace is ready (each peering scoped to its respective resource group), with the NSG update running in parallel against the Neo4j resource group.

---

### `modules/databricks-vnet.bicep`

Creates the customer-managed VNet required for VNet injection. Provisions two empty NSGs (one per subnet), a static public IP, a Standard SKU NAT gateway, and the VNet itself with both subnets delegated to `Microsoft.Databricks/workspaces`. Subnet CIDRs are hardcoded to `192.168.0.0/26` (host) and `192.168.64.0/26` (container) within the default /16 address space.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `location` | required | Azure region |
| `resourceSuffix` | required | Unique suffix appended to all resource names |
| `vnetCidr` | `192.168.0.0/16` | VNet address space |

Outputs: `vnetId`, `vnetName`, `hostSubnetName`, `containerSubnetName`

---

### `modules/databricks-workspace.bicep`

Creates the Databricks workspace at Standard SKU with VNet injection and Secure Cluster Connectivity enabled. Uses API version `2024-05-01`, which sets `enableNoPublicIp` to `true` by default; the template sets it explicitly.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `workspaceName` | required | Workspace resource name |
| `location` | required | Azure region |
| `managedResourceGroupId` | required | Full resource ID of the Databricks-managed resource group (constructed by the orchestrator as `{databricksResourceGroup}-managed`) |
| `vnetId` | required | Resource ID of the customer-managed VNet |
| `hostSubnetName` | required | Name of the host (driver) subnet |
| `containerSubnetName` | required | Name of the container (worker) subnet |

Outputs: `workspaceId`, `workspaceUrl`

---

### `modules/vnet-peering.bicep`

Creates a single one-directional VNet peering connection. Called twice by the orchestrator with different scopes: once scoped to the Databricks resource group (peering from Databricks to Neo4j) and once scoped to the Neo4j resource group (peering from Neo4j to Databricks). Uses the `existing` keyword to reference the local VNet without redeploying it.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `localVnetName` | required | Name of the VNet on the initiating side of this peering |
| `remoteVnetId` | required | Full resource ID of the VNet on the receiving side |
| `peeringName` | required | Name for the peering connection resource |

Settings: `allowVirtualNetworkAccess: true`, gateway transit and forwarding disabled.

---

### `modules/neo4j-nsg-peering.bicep`

Redeploys the full Neo4j NSG rule set, replacing the original Internet-open inbound rules for the four Neo4j ports with rules scoped to the Databricks CIDR. Adds a deny-all rule from the Databricks CIDR at priority 200. SSH (port 22) and the cluster inter-node ports (6000, 7000) are preserved unchanged.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `nsgName` | required | Name of the existing Neo4j NSG to update |
| `location` | required | Azure region |
| `databricksCidr` | required | Databricks VNet CIDR used as the inbound source for Neo4j port allow rules |

NSG rules after update:

| Priority | Name | Port | Source | Action |
|----------|------|------|--------|--------|
| 100 | SSH | 22 | Internet | Allow |
| 101 | HTTPS | 7473 | `databricksCidr` | Allow |
| 102 | HTTP | 7474 | `databricksCidr` | Allow |
| 103 | Bolt | 7687 | `databricksCidr` | Allow |
| 104 | ClusterCommunication | 6000 | VirtualNetwork | Allow |
| 105 | ClusterRaft | 7000 | VirtualNetwork | Allow |
| 106 | BoltRouting | 7688 | `databricksCidr` | Allow |
| 200 | DenyDatabricks | * | `databricksCidr` | Deny |
