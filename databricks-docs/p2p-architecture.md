# Private Databricks-to-Neo4j Connectivity on Azure

## Overview

This architecture establishes a private network path between a Databricks analytics workspace and a three-node Neo4j Enterprise Edition cluster on Azure. Databricks cluster nodes reach Neo4j through a bidirectional VNet peering connection that routes all traffic between the two systems directly over the Microsoft backbone. The Neo4j database ports accept inbound connections from the Databricks address space only. Cluster nodes carry no public IP addresses. Every component spans two customer-owned resource groups and is orchestrated through a single subscription-scoped deployment.

The design centers on VNet injection: placing the Databricks compute plane into a customer-managed Virtual Network rather than the Microsoft-managed default. That single decision is what makes standard Azure VNet peering possible between the two networks. The peering creates the private route; an NSG update on the Neo4j side defines exactly which ports are accessible across it; and a NAT gateway gives cluster nodes the outbound path they need to reach the Databricks control plane.

**Major components:**

- A customer-managed Azure Virtual Network (192.168.0.0/16) with two dedicated subnets and empty NSGs on each, satisfying Azure's subnet delegation requirements for Databricks
- A NAT gateway (Standard SKU, static public IP) attached to both subnets, providing mandatory outbound internet access for the Databricks control plane
- A Databricks workspace (Standard SKU) deployed with VNet injection into the customer-managed VNet, with Secure Cluster Connectivity enabled so cluster nodes carry no public IP addresses
- Bidirectional VNet peering connecting the Neo4j VNet (10.0.0.0/8) and the Databricks VNet (192.168.0.0/16) across two resource groups in the same subscription and region
- A three-node Neo4j Enterprise Edition cluster deployed as a VM Scale Set with cluster discovery enabled
- A Standard SKU internal load balancer (ILB) fronting the three-node cluster, with a private frontend IP allocated from the Neo4j subnet and no public IP. An ILB is reachable only from within the same VNet or through an explicitly established private path; it is not accessible from the internet.
- An NSG update on the Neo4j side that replaces the Internet-open inbound rules for ports 7473, 7474, 7687, and 7688 with rules scoped to the Databricks CIDR, plus a deny-all rule from that CIDR at priority 200
- *(Optional, required only for serverless compute)* A Private Link Service (PLS) attached to the ILB frontend, deployed with a NAT IP on a dedicated `pls-subnet` (`10.1.0.0/28`), that exposes the ILB to private endpoint connections from Databricks-managed serverless infrastructure without requiring a VNet peering on the Databricks side
- *(Optional, required only for serverless compute)* A Network Connectivity Configuration (NCC), an account-level Azure Databricks resource that provisions a private endpoint from Databricks-managed infrastructure to the PLS and routes serverless compute traffic through it

The result is a Databricks workspace where VNet-injected job clusters reach Neo4j directly over the peering, and serverless notebooks reach the same cluster through the Private Link tunnel. Both paths terminate at the same ILB and the same three-node VMSS backend pool.

VNet-injected job clusters require no configuration beyond what the components above already provide. The PLS and NCC are optional and needed only when serverless compute must reach Neo4j.

Serverless is a significant part of the Databricks platform. SQL warehouses — the default compute behind Databricks SQL and interactive notebook queries — run on serverless infrastructure, as do serverless jobs, Lakeflow Spark Declarative Pipelines, and model serving endpoints. Model serving is the infrastructure that Mosaic AI agent deployments run on: an AI agent that queries Neo4j from a Databricks serving endpoint executes on serverless, not on a VNet-injected job cluster. Data quality monitoring and predictive optimization also run on serverless infrastructure. Without the PLS and NCC, none of those workloads can reach the Neo4j cluster over a private path. The PLS and NCC together create the private tunnel that serverless requires and are provisioned by `uv run bicep-deploy setup-ncc` or `uv run ansible-deploy setup-ncc` after the cluster and Databricks workspace are deployed.

Two deployment tools in this repository produce this architecture: Bicep templates under `infra/` and Ansible playbooks under `playbooks/`. They are equivalent; pick whichever fits your operational preference. See the [Deployment](#deployment) section at the bottom of this document for the file layout of each path.

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
│   │       ├── Internal Load Balancer (ILB)  (Standard SKU, private frontend IP, no public IP)
│   │       └── Neo4j VMSS  (3 nodes: vm0, vm1, vm2)
│   └── NSG  (attached to Neo4j subnet)
│       ├── Allow  7687, 7473, 7474, 7688  from Databricks CIDR  (priority 101-106)
│       ├── Allow  *                        from AzureLoadBalancer  (priority 110)
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

The delegation also means that Databricks manages the NSG rules on both subnets automatically. The service adds and modifies rules through the delegation mechanism to maintain cluster communication. However, Azure requires that an NSG resource be attached to each subnet at the time the workspace is created, even though the workspace will subsequently manage its own rules on those NSGs. Deploying the workspace without attached NSGs produces a `SubnetMissingNSG` error. The deployment creates two empty NSGs, attaches them, and leaves their rule management to the Databricks service thereafter.

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

The peering connections span two resource groups within the same subscription and region. The deployment runs at subscription scope so it can write into both resource groups in a single coordinated run. After both connections reach Connected, the Neo4j VNet's `VirtualNetwork` source tag in existing NSG rules also covers the peered Databricks address space (192.168.0.0/16). The cluster inter-node ports (6000 and 7000) therefore do not require separate rule changes; they inherit coverage through the expanded VirtualNetwork tag.

### NSG Update on the Neo4j Side

The original Neo4j NSG opens the Bolt port (7687), the browser ports (7473 and 7474), and the routing connector port (7688) to the Internet source tag. Those rules accept connections from any public IP. After the peering is in place, those rules are replaced with rules that accept connections only from 192.168.0.0/16.

A deny-all rule from 192.168.0.0/16 at priority 200 sits below the allow rules for the Neo4j ports. Its purpose is to block inbound access from Databricks cluster nodes to any port on the Neo4j subnet other than the four Neo4j ports explicitly opened. Without this rule, any Databricks cluster node could reach any open port on the Neo4j VMs. The allow rules sit at priorities 101 through 106; the deny rule at 200 catches everything else from the peered range before the default VirtualNetwork allow rule at priority 65000 picks it up.

SSH (port 22) remains open to the Internet source tag so that administrative access to the Neo4j VMs does not depend on the Databricks peering being active. This is acceptable for a demonstration deployment; a production hardening would restrict SSH to a jump host or VPN.

After this NSG update, the browser UI at port 7474 and the Bolt endpoint at port 7687 are no longer reachable from the public internet. Access from a local workstation requires an SSH tunnel through the Neo4j VM.

### Inbound Allowlist Scope

The allow rules on ports 7473, 7474, 7687, and 7688 match on `192.168.0.0/16`, the Databricks VNet CIDR. This is a private address range, and the NSG evaluates it against the private source addresses of Databricks cluster nodes. VNet peering is what makes those private addresses visible at the Neo4j subnet boundary: without peering, a packet from a cluster node arrives at the Neo4j NSG carrying the NAT gateway's public source IP, and the CIDR match fails.

Several properties follow from using a private CIDR as the source.

**The allowlist is a single entry per port, stable for the life of the workspace.** Each rule names the Databricks VNet address space chosen at deployment time. That CIDR is fixed when the VNet is created and stays constant until the VNet and workspace are recreated, so the allowlist is set once and holds as clusters scale up, scale down, restart, or recycle nodes.

**Databricks-published public IP ranges stay out of the configuration.** Cluster nodes run with Secure Cluster Connectivity, so every node's traffic originates from a private IP inside 192.168.0.0/26 (host subnet) or 192.168.64.0/26 (container subnet), both of which fall within 192.168.0.0/16. The regional Databricks control-plane IP ranges apply to the SCC relay tunnel, which flows from the control plane to cluster nodes and terminates inside the Databricks-managed VNet. Those ranges appear only as source addresses on the SCC tunnel, never at the Neo4j NSG.

**The NSG governs inbound traffic only.** The Neo4j VMSS originates no connections to Databricks. The Bolt driver in a Databricks notebook is the client; Neo4j is the server. All traffic between the two systems arrives at the Neo4j subnet as inbound, so the four allow rules plus the deny-all at priority 200 fully describe the security boundary between Databricks and Neo4j.

**The Databricks side needs no matching configuration.** The Databricks-managed NSGs on the host and container subnets are delegated to `Microsoft.Databricks/workspaces`, and the default rule set managed by the service permits outbound traffic across the peering. Once both peering connections reach Connected, the effective routes on the Databricks subnets include the Neo4j VNet range, and the Bolt client in a notebook reaches the Neo4j private IP directly with no additional Databricks-side rules, routes, or allowlists.

### Serverless Compute and the Peering Gap

VNet peering connects two customer-owned Virtual Networks over the Microsoft backbone. A Databricks job cluster running with VNet injection sits inside the customer-managed VNet (`192.168.0.0/16`); the peering gives it a route to `10.0.0.0/8`, and the NSG allow rules open the Neo4j ports to that CIDR. The path works because the cluster VM has an address inside a network the customer controls.

Databricks serverless compute does not run inside a customer VNet. Serverless notebooks and SQL warehouses execute on Databricks-managed infrastructure in a Databricks-owned network isolated from the customer's Azure subscription. There is no customer-owned VNet to peer with and no route from the serverless compute plane to the Neo4j ILB's private frontend IP. A connection attempt from a serverless notebook to `10.0.0.4:7687` sends a TCP SYN into a network the Microsoft backbone cannot route from Databricks-managed infrastructure; the ILB never sees it.

Azure Private Link bridges these two network domains without requiring a customer-owned VNet on the Databricks side. The mechanism has two components: a Private Link Service deployed in the Neo4j resource group and a Network Connectivity Configuration provisioned through the Databricks account API.

### Private Link Service

A Private Link Service (`Microsoft.Network/privateLinkServices`) wraps the Standard ILB frontend and exposes it for private endpoint connections from outside the customer VNet, including Databricks-managed infrastructure. When a private endpoint connects to the PLS, Azure builds a tunnel on the Azure backbone between the endpoint's network interface and the PLS. All traffic through that tunnel stays on the Microsoft network; it does not traverse the public internet.

The PLS requires a dedicated subnet where `privateLinkServiceNetworkPolicies` is set to `Disabled`. This flag exempts traffic associated with Private Link resources from NSG and UDR enforcement on that subnet. Microsoft recommends a `/28` for this purpose. This template adds a second subnet to the Neo4j VNet (`10.1.0.0/28`, named `pls-subnet`) and allocates one NAT IP configuration from it. Traffic arriving at the Neo4j VMSS through the Private Link path carries the NAT IP as its source address; the existing default `VirtualNetwork` allow rule at priority 65000 covers it, because `10.1.0.0/28` falls within the Neo4j VNet. No new NSG rules are required.

The Standard SKU ILB is the prerequisite for hosting a Private Link Service. The PLS attaches to the existing ILB frontend and shares the backend pool and health probe rules with the VNet-peered path. No additional ILB configuration is required.

The PLS `visibility.subscriptions` controls which Azure subscriptions can see and request a connection. The current deployment sets this to `['*']`, which allows any Azure subscription to initiate a connection request. Azure Private Link does support scoping visibility to a named list of subscription IDs. Restricting to the specific Databricks-managed subscription that creates the private endpoint would tighten the posture, but that subscription ID is not published by Databricks and must be captured from a live connection (see `worklog/serverless-testing.md`). Regardless of visibility setting, no connection carries traffic until approved: the approval step is the access control gate, not visibility.

### Network Connectivity Configuration

A Network Connectivity Configuration is an account-level Azure Databricks resource that defines the network policy for serverless compute. It is separate from VNet injection, which governs where job cluster VMs are placed; an NCC governs where serverless compute sends its traffic.

NCCs are created at the Databricks account level and are region-scoped: an NCC created in East US 2 creates private endpoint connections in East US 2. Each Azure Databricks account supports up to ten NCCs per region. A single NCC can attach to up to fifty workspaces. The private endpoints that an NCC creates are dedicated to the Databricks account and accessible only from workspaces the NCC is attached to.

When a Private Endpoint Rule is added to an NCC, Databricks provisions a private endpoint from its managed infrastructure to the target Azure resource. For a custom Private Link Service (a `Microsoft.Network/privateLinkServices` resource, as opposed to a first-party service like Storage or SQL), the rule specifies the PLS ARM resource ID. The private endpoint request appears on the PLS as a connection in `Pending` state. It stays in that state until the customer approves it, at which point the NCC rule transitions to `ESTABLISHED` and serverless compute can use the path.

### Provisioning Sequence

Private Link connectivity for serverless requires two phases corresponding to the two deployment steps in this repository.

The first phase runs during the Neo4j cluster deployment. When `nodeCount >= 3`, both deployment tools create the `pls-subnet` (`10.1.0.0/28`) and the `pls-neo4j` Private Link Service alongside the Standard ILB. The PLS resource ID is written to the deployment output JSON for use in the second phase.

The second phase runs via `uv run bicep-deploy setup-ncc` or `uv run ansible-deploy setup-ncc`. This command reads the PLS resource ID from the deployment JSON, then: creates or reuses an NCC scoped to the workspace region; attaches the NCC to the Databricks workspace; creates a Private Endpoint Rule pointing at the PLS resource ID; polls the PLS for a connection in `Pending` state; approves the connection programmatically via the Azure CLI; and waits for the NCC rule to reach `ESTABLISHED`. The entire sequence is unattended and idempotent: re-running it after a partial failure skips steps already completed.

The approval step requires the Azure CLI identity to have write access on the Neo4j resource group. It is programmatic but deliberate: no traffic flows through the Private Link path until a customer with resource-group write access explicitly approves the Databricks-initiated connection. This holds true regardless of the PLS visibility setting.

### Driver Protocol for Serverless Compute

Serverless notebooks connecting through the Private Link path must use `bolt://` (direct mode). `neo4j://` (routing mode) does not work from serverless: the routing table returned by Neo4j lists the individual VMSS node IPs, which are not routable from Databricks-managed infrastructure. `bolt://` skips the routing table and sends all queries directly to the ILB. End-to-end validation over the Private Link path — DNS, TCP 7687/7474, Bolt driver, and cluster topology — passes with `bolt://neo4j.private:7687`. Whether `neo4j://` can ever succeed over this path (e.g. if Neo4j advertises the ILB IP in the routing table) remains unvalidated; see `worklog/serverless-testing.md`.

The constraint arises from how routing mode works. A `neo4j://` connection triggers a ROUTE request, and the cluster responds with a routing table listing the advertised addresses of each cluster member: the private IPs assigned to the three VMSS instances (`10.0.0.x`). The driver then opens separate direct TCP connections to those addresses to distribute reads and writes. From serverless infrastructure, only the ILB frontend IP is reachable through the Private Link tunnel. The VMSS node IPs are not routable from Databricks-managed infrastructure, so the direct connections fail and the driver cannot complete session establishment.

`bolt://<lb-frontend-ip>:7687` skips the routing table entirely. The driver sends all queries over a single connection to the ILB, which distributes them across the VMSS backend pool. The tradeoff is that the driver has no cluster topology awareness: it does not direct writes to the leader or reads to followers, and it does not perform client-side failover.

VNet-injected job clusters are not subject to this constraint. The peering makes the entire `10.0.0.0/8` address space routable from the Databricks container subnet, so a job cluster can fetch the routing table, receive the individual VMSS node IPs, and open direct connections to each. Classic compute uses `neo4j://` and gets full cluster-aware driver behavior. Serverless compute uses `bolt://` and treats the ILB as a single endpoint.

---

## The Private Path in Practice

Once both peering connections show Connected and the NSG update is in place, a Databricks notebook running on any cluster in the workspace can open a Neo4j driver session using the ILB's private frontend IP. The driver targets `neo4j://<lb-private-ip>:7687`. The packet leaves the cluster node's container subnet, traverses the VNet peering, arrives at the Neo4j subnet, passes the NSG inbound check (the source address falls within the Databricks CIDR, which the allow rule for port 7687 covers), reaches the ILB frontend, and is forwarded by the ILB backend rule to one of the three Neo4j cluster nodes.

The full round-trip stays on the Microsoft network backbone. The NSG at the Neo4j subnet boundary is the only access control gate between the cluster and the database.

`driver.verify_connectivity()` succeeds over this path, and Cypher queries execute against the graph from the notebook without any traffic leaving the Microsoft network. The ILB private frontend IP is retrievable via `az network lb frontend-ip list` against the Neo4j resource group.

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

**Internal Load Balancer (ILB).** An Azure Standard Load Balancer configured with a private frontend IP allocated from a VNet subnet rather than a public IP. An ILB is not reachable from the internet; it accepts connections only from within the same VNet or from a network that has an explicitly established private path to it, such as a VNet peering or a Private Link Service. This is the correct choice for Neo4j cluster access because the database should never be exposed directly to the internet. An external (internet-facing) load balancer would carry a public IP and bypass the NSG boundary that restricts access to the Databricks address space. Azure requires the Standard SKU for ILBs that host a Private Link Service; the Basic SKU does not support it.

**Bolt Protocol.** The binary wire protocol used by Neo4j drivers to communicate with Neo4j instances. Bolt runs over TCP on port 7687 by default. The `bolt://` URI scheme establishes a direct connection to a single Neo4j instance; the `neo4j://` URI scheme requests a routing table from the server and distributes reads and writes across cluster members. For a VNet-injected job cluster with access to the full Neo4j VNet, use `neo4j://` pointed at the ILB's private frontend IP. The driver sends an initial routing request through the ILB, receives a routing table listing all three cluster members, and then distributes reads and writes directly to those members. For serverless compute connecting through the Private Link path, use `bolt://` pointed at the ILB's private frontend IP; the individual VMSS node IPs in the routing table are not routable from serverless infrastructure, so routing mode cannot be used.

---

## Deployment

This architecture is implemented twice in the repository, once in Bicep and once in Ansible. Both paths produce the same Azure resources (same VNet CIDRs, same subnet layout, same NSG rule set, same workspace configuration), write connection details to `.deployments/{scenario}-{engine}.json` (e.g. `peer-databricks-v2025-bicep.json` or `peer-databricks-v2025-ansible.json`), and drive the same connectivity-test notebook in `notebooks/neo4j_connectivity_test.ipynb`. Pick whichever tool matches your operational preference.

Both CLIs read configuration from `.arm-testing/config/settings.yaml` and support the same scenarios (`standalone-v2025`, `cluster-v2025`, `peer-databricks-v2025`).

### Bicep — `infra/`

| File | Role |
|------|------|
| `infra/databricks-main.bicep` | Subscription-scoped orchestrator; creates the Databricks resource group and coordinates the VNet, workspace, peering, and NSG-update modules in a single deployment |
| `infra/modules/databricks-vnet.bicep` | Customer-managed VNet (192.168.0.0/16), delegated host and container subnets, empty NSGs, Standard SKU NAT gateway with static public IP |
| `infra/modules/databricks-workspace.bicep` | Standard SKU Databricks workspace with VNet injection and Secure Cluster Connectivity enabled |
| `infra/modules/vnet-peering.bicep` | One-directional peering connection; called twice (once per side) by the orchestrator to establish bidirectional peering |
| `infra/modules/neo4j-nsg-peering.bicep` | Replaces the Neo4j NSG rule set with CIDR-scoped allow rules for the four Neo4j ports plus the deny-all at priority 200 |
| `infra/main.bicep` | Entry point for the Neo4j side; deploys the VNet, identity, VMSS, and (for clusters) the internal load balancer |

Driven by `uv run bicep-deploy deploy --scenario peer-databricks-v2025`.

### Ansible — `playbooks/`

| File | Role |
|------|------|
| `playbooks/databricks.yml` | Orchestrator play; coordinates the Databricks network, workspace, peering, and NSG-update tasks |
| `playbooks/tasks/databricks_network.yml` | Customer-managed VNet (192.168.0.0/16), delegated host and container subnets, empty NSGs, Standard SKU NAT gateway with static public IP |
| `playbooks/tasks/databricks_workspace.yml` | Standard SKU Databricks workspace with VNet injection and Secure Cluster Connectivity enabled |
| `playbooks/tasks/vnet_peering.yml` | Bidirectional VNet peering (both directions in one task file) |
| `playbooks/tasks/nsg_update.yml` | Replaces the Neo4j NSG rule set with CIDR-scoped allow rules for the four Neo4j ports plus the deny-all at priority 200 |
| `playbooks/neo4j.yml` | Entry point for the Neo4j side; runs the network, identity, VMSS, and (for clusters) load balancer tasks |
| `playbooks/tasks/network.yml` | Neo4j VNet, subnet, NSG; also creates `pls-subnet` (`10.1.0.0/28`, policies disabled) for cluster deployments |
| `playbooks/tasks/loadbalancer.yml` | Internal load balancer; also creates `pls-neo4j` Private Link Service attached to the ILB frontend for cluster deployments |

Driven by `uv run ansible-deploy deploy --scenario peer-databricks-v2025`.

### Resulting NSG Rule Set on the Neo4j Subnet

Both paths produce the same rules:

| Priority | Name | Port | Source | Action |
|----------|------|------|--------|--------|
| 100 | SSH | 22 | Internet | Allow |
| 101 | HTTPS | 7473 | Databricks CIDR | Allow |
| 102 | HTTP | 7474 | Databricks CIDR | Allow |
| 103 | Bolt | 7687 | Databricks CIDR | Allow |
| 104 | ClusterCommunication | 6000 | VirtualNetwork | Allow |
| 105 | ClusterRaft | 7000 | VirtualNetwork | Allow |
| 106 | BoltRouting | 7688 | Databricks CIDR | Allow |
| 110 | AzureLoadBalancerProbe | * | AzureLoadBalancer | Allow |
| 200 | DenyDatabricks | * | Databricks CIDR | Deny |
