# Neo4j Enterprise: Bicep Deployment

Python CLI and Azure Bicep templates for deploying Neo4j Enterprise Edition on Azure VM Scale Sets. Supports standalone (1 node) or cluster (3–10 nodes) deployments.

## Deploy Neo4j EE

Deploys Neo4j Enterprise Edition only, with no Databricks workspace. For the combined Neo4j + Databricks deployment, see [Deploy Neo4j EE and Databricks](#deploy-neo4j-ee-and-databricks).

```bash
# Navigate to deployments directory
cd deployments

# Install dependencies
uv sync

# Run interactive setup wizard
uv run bicep-deploy setup

# Deploy standalone Neo4j
uv run bicep-deploy deploy --scenario standalone-v2025

# Deploy 3-node cluster
uv run bicep-deploy deploy --scenario cluster-v2025

# Check deployment status
uv run bicep-deploy status

# Verify database connectivity and license
uv run bicep-deploy verify

# Validate M2M bearer token authentication (if configured during setup)
export NEO4J_CLIENT_SECRET="your-client-secret"
cd ../validate-bearer-token
uv run validate_bearer.py --scenario standalone-v2025

# Clean up resources
cd ../deployments
uv run bicep-deploy cleanup --all --force
```

> **Note:** The setup wizard includes optional M2M (Machine-to-Machine) bearer token authentication configuration via Keycloak or Microsoft Entra ID. See [m2m.md](m2m.md) for details.

---

## Deploy Neo4j EE and Databricks

This flow deploys a 3-node Neo4j cluster behind an Azure internal load balancer, then peers a Databricks workspace into the same VNet. The two scenarios must be deployed in order: `cluster-v2025` saves the VNet and NSG resource IDs that `peer-databricks-v2025` reads.

```bash
cd deployments
uv sync
uv run bicep-deploy setup
```

#### Step 1: Deploy the Neo4j cluster

Provisions a 3-node Neo4j cluster on a VMSS with an internal Azure Standard load balancer. The load balancer is deployed automatically when `nodeCount >= 3`. Browser and Bolt access from a local machine requires an SSH tunnel because the ILB has no public IP.

```bash
uv run bicep-deploy deploy --scenario cluster-v2025
```

#### Step 2: Deploy the Databricks workspace

Provisions a Databricks workspace with VNet injection and Secure Cluster Connectivity, establishes bidirectional VNet peering between the Databricks VNet and the Neo4j VNet, and updates the Neo4j NSG to restrict database ports to the Databricks CIDR.

```bash
uv run bicep-deploy deploy --scenario peer-databricks-v2025
```

#### Step 3: Set up Databricks secrets and notebooks

Creates a Databricks secrets scope pre-loaded with Neo4j connection credentials and uploads connectivity test notebooks for both classic and serverless compute. Required before running notebooks.

```bash
uv run bicep-deploy setup-databricks --scenario peer-databricks-v2025
```

Once complete, the notebooks are in the workspace and credentials are in the secrets scope. See [Neo4j + Databricks Overview](#neo4j--databricks-overview) below for how to run them.

#### Step 4: Set up NCC for serverless compute (optional)

Creates a Databricks Network Connectivity Configuration, attaches it to the workspace, and creates a private endpoint rule pointing at the Neo4j Private Link Service. Required only for serverless compute; classic compute uses the VNet peering path and does not need this step.

```bash
uv run bicep-deploy setup-ncc --scenario peer-databricks-v2025 --account-profile <databricks-account-admin-profile>
```

#### Step 5: Run connectivity checks

Validates end-to-end connectivity. Without `--compute`, the command auto-detects which compute paths are available from the deployment profile.

```bash
# All available checks (auto-detected)
uv run neo4j-connect check --scenario peer-databricks-v2025

# Classic compute only (VNet peering path — does not require setup-ncc)
uv run neo4j-connect check --scenario peer-databricks-v2025 --compute classic
```

See [testing.md](testing.md) for the full `neo4j-connect` reference.

#### Cleanup

```bash
uv run bicep-deploy cleanup --all --force
```

## Neo4j + Databricks Overview

### Classic compute connectivity

`setup-databricks` uploads [`notebooks/neo4j_connectivity_test.ipynb`](../notebooks/neo4j_connectivity_test.ipynb) to `/Shared/neo4j-peer-databricks-v2025-connectivity-test` in the workspace and stores Neo4j credentials in a Databricks secrets scope. To run it:

1. In the Databricks workspace, create or start a VNet-injected job cluster using any current LTS runtime.
2. Open `/Shared/neo4j-peer-databricks-v2025-connectivity-test` and attach the cluster.
3. Run all cells. A successful run prints `PASS` for TCP connectivity, driver authentication, and cluster topology.

The notebook connects via the internal load balancer private IP using `neo4j://` to fetch a routing table, which distributes reads and writes across all three cluster nodes. Credentials are read from the Databricks secrets scope, with no hardcoded values.

### Serverless compute connectivity

`setup-databricks` also uploads [`notebooks/neo4j_serverless_connectivity_test.ipynb`](../notebooks/neo4j_serverless_connectivity_test.ipynb) to `/Shared/neo4j-peer-databricks-v2025-serverless-connectivity-test`. To run it:

1. Open `/Shared/neo4j-peer-databricks-v2025-serverless-connectivity-test` in the workspace.
2. Switch the compute selector to **Serverless**. No cluster attachment is needed.
3. Run all cells. A successful run prints `PASS` for TCP connectivity, driver authentication, and cluster topology (`SHOW SERVERS` returns all three nodes).

Serverless compute has no route through the VNet peering and cannot reach the ILB private IP directly. Connectivity goes through a Private Link Service on the Neo4j ILB wired to a Databricks Network Connectivity Configuration; the driver reaches Neo4j via the hostname `neo4j.private`, configured by `setup-ncc`. Use `neo4j://neo4j.private:7687` for full cluster-aware routing across all three nodes; `bolt://neo4j.private:7687` also works as a direct fallback.

See [testing.md](testing.md) for the full `neo4j-connect` check reference.

## Commands

| Command | Description |
|---------|-------------|
| `setup` | Interactive setup wizard for configuration |
| `validate` | Validate Bicep templates without deploying |
| `deploy` | Deploy one or more scenarios to Azure |
| `verify` | Verify database connectivity and license via Bolt |
| `status` | Show deployment status |
| `setup-databricks` | Create Databricks secrets scope and upload connectivity test notebook |
| `setup-ncc` | Create Databricks NCC and Private Link Service endpoint for serverless compute |
| `cleanup` | Delete Azure resources |
| `report` | Generate test report for deployments |

## Configuration

After running `uv run bicep-deploy setup`, configuration files are created in `deployments/.arm-testing/config/`:

- `settings.yaml` - Main settings (Azure subscription, regions, cleanup modes)
- `scenarios.yaml` - Test scenario definitions

### Default Scenarios

The setup wizard creates three default scenarios in `.arm-testing/config/scenarios.yaml`.

#### standalone-v2025

A single-node Neo4j 2025 deployment on a public-IP VM.

| Parameter | Value |
|-----------|-------|
| `deployment_type` | `vm` |
| `node_count` | `1` |
| `vm_size` | `Standard_D2s_v5` |
| `disk_size` | `32` GB |
| `graph_database_version` | `2025` |
| `license_type` | `Evaluation` |

No load balancer is provisioned for single-node deployments. The Neo4j browser and Bolt endpoints are available directly on the VM's public IP. Saved to `.deployments/standalone-v2025-bicep.json` after deploy.

#### cluster-v2025

A 3-node Neo4j 2025 cluster behind an Azure Standard internal load balancer.

| Parameter | Value |
|-----------|-------|
| `deployment_type` | `vm` |
| `node_count` | `3` |
| `vm_size` | `Standard_D2s_v5` |
| `disk_size` | `32` GB |
| `graph_database_version` | `2025` |
| `license_type` | `Evaluation` |

The load balancer is provisioned automatically when `node_count >= 3`. It is internal; browser and Bolt access from a local machine requires an SSH tunnel to a cluster node. Saved to `.deployments/cluster-v2025-bicep.json` after deploy.

#### peer-databricks-v2025

A Databricks workspace with VNet injection and private VNet peering to an existing Neo4j cluster. Reads the VNet and NSG resource IDs saved by `cluster-v2025` and builds on that deployment.

| Parameter | Value |
|-----------|-------|
| `deployment_type` | `databricks-peering` |
| `source_scenario` | `cluster-v2025` |
| `node_count` | `3` (inherited from source) |
| `vm_size` | `Standard_D2s_v5` |
| `disk_size` | `32` GB |
| `graph_database_version` | `2025` |
| `license_type` | `Evaluation` |
| `databricks_workspace_name` | `neo4j-dbx` |
| `databricks_vnet_cidr` | `192.168.0.0/16` |

**This scenario must be deployed after `cluster-v2025`.** The Databricks VNet (`192.168.0.0/16`) is peered to the Neo4j VNet (`10.0.0.0/16`) in both directions. After peering, the Neo4j NSG is updated to restrict database ports (7473, 7474, 7687, 7688) to the Databricks CIDR only, so browser access from outside the Databricks VNet requires an SSH tunnel. Saved to `.deployments/peer-databricks-v2025-bicep.json` after deploy.

You can modify `scenarios.yaml` to add custom scenarios or adjust any of these defaults.

## Template Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `adminUsername` | SSH username | `neo4j` |
| `adminPassword` | Neo4j admin password | (required) |
| `vmSize` | Azure VM size | `Standard_D2s_v5` |
| `nodeCount` | Number of nodes (1, 3-10) | `1` |
| `diskSize` | Data disk size in GB | `32` |
| `graphDatabaseVersion` | Neo4j version | `2025` |
| `licenseType` | `Enterprise` or `Evaluation` | `Evaluation` |
| `location` | Azure region | Resource group location |

> **Note:** Neo4j 2025.x requires Java 21 or later. The VM image includes the required Java version.

## Environment Variables

Configure these in your `.env` file:

| Variable | Description | Default |
|----------|-------------|---------|
| `NEO4J_ADMIN_PASSWORD` | Neo4j admin password | Auto-generated |
| `AZURE_LOCATION` | Azure region | `eastus` |
| `NEO4J_NODE_COUNT` | Number of nodes (1, 3-10) | `1` |
| `NEO4J_VM_SIZE` | Azure VM size | `Standard_D2s_v5` |
| `NEO4J_DISK_SIZE` | Data disk size in GB | `32` |
| `NEO4J_VERSION` | Neo4j version | `2025` |
| `NEO4J_LICENSE_TYPE` | `Enterprise` or `Evaluation` | `Evaluation` |

## Cleanup Modes

| Mode | Description |
|------|-------------|
| `immediate` | Delete resources immediately after deployment |
| `on-success` | Delete only if tests pass (keep failures for debugging) |
| `manual` | Never auto-delete (requires explicit cleanup) |
| `scheduled` | Tag resources to expire after N hours |

## Password Management

Three password strategies are available:

1. **Generate** (default) - Random secure password per deployment
2. **Environment** - Read from `NEO4J_ADMIN_PASSWORD` environment variable
3. **Prompt** - Interactive prompt each time

## Ports

| Port | Protocol | Description |
|------|----------|-------------|
| 22 | TCP | SSH |
| 7473 | TCP | HTTPS (Neo4j Browser) |
| 7474 | TCP | HTTP (Neo4j Browser) |
| 7687 | TCP | Bolt (Neo4j Driver) |
| 7688 | TCP | Bolt Routing (Cluster) |
| 6000 | TCP | Cluster Communication |
| 7000 | TCP | Raft Consensus |

## Outputs

After deployment, you'll receive:

- `Neo4jBrowserURL`: URL to access Neo4j Browser (single-node public IP; cluster deployments use an internal load balancer and require an SSH tunnel for browser access)
- `Username`: Default username (`neo4j`)

## Load balancer and NSG design notes

These notes capture non-obvious behaviors that were confirmed during live deployments. They are relevant whenever the load balancer or NSG configuration is modified.

**All LB probes use HTTP on port 7474, not TCP on the Bolt ports**

The `inbound7687` and `inbound7688` load-balancing rules both reference `httpprobe` (HTTP GET on port 7474) rather than separate TCP probes on 7687/7688. This was an explicit design choice. Neo4j 2026.x completes the TCP handshake on port 7687 before sending a Bolt protocol banner, so a raw TCP probe would technically record a SYN-ACK and pass. However, using `httpprobe` for all three rules gives a single, unambiguous health gate: if Neo4j is serving HTTP on 7474 it is ready to serve Bolt, and all three rules flip together. Keeping separate TCP probes on 7687/7688 adds probe surface area with no benefit.

**`enableTcpReset: true` and `errno 111`**

Every LB rule has `enableTcpReset: true`. When a probe fails and the LB marks all backends unhealthy, it sends a TCP RST to clients rather than silently dropping packets. Clients receive `errno 111` (Connection refused) instead of a timeout. This is faster feedback, but a transient probe failure (such as a brief gap when the NSG ruleset is replaced during Databricks peering) produces a hard error rather than a slow one. The Bicep deployment keeps the `AzureLoadBalancerProbe` NSG allow rule across all NSG states so this window is minimised.

**`AzureLoadBalancerProbe` NSG rule must always be present**

The Standard ILB health probe traffic originates from the `AzureLoadBalancer` service tag. If the NSG does not have an Allow rule for this source, probes never reach the VMSS instances. The LB then marks all backends unhealthy and RSTs every inbound connection. This rule must survive any full NSG replacement during Databricks peering; verify that the updated NSG still contains an Allow rule for `AzureLoadBalancer` before testing connectivity.

**Databricks Serverless compute requires Private Link Service and NCC**

Databricks Serverless compute runs on Databricks-managed infrastructure outside the customer subscription; it is not VNet-injected and has no route to private RFC-1918 addresses. The VNet peering used by classic compute does not carry traffic for serverless workloads. To reach the Neo4j ILB from Serverless compute, the deployment includes a `pls-subnet` and a Private Link Service (`pls-neo4j`) attached to the ILB frontend. Run `setup-ncc` to create a Databricks Network Connectivity Configuration, attach it to the workspace, and create a private endpoint rule pointing at the PLS. Once attached, Databricks Serverless resolves `neo4j.private` to the private endpoint IP and opens a TCP path through the PLS into the ILB. Use `neo4j://neo4j.private:7687` from serverless notebooks for full cluster-aware routing; `bolt://neo4j.private:7687` also works as a direct fallback.

## Production Security Hardening

The default configuration is tuned for demo and testing use. Before moving to production, review the following.

### Network access after `peer-databricks-v2025`

When `peer-databricks-v2025` is deployed, the Neo4j NSG is replaced to restrict public access:

| Port | After peering |
|------|--------------|
| 22 (SSH) | Open to Internet |
| 7473 (HTTPS / Neo4j Browser) | Open to Internet |
| 7474 (HTTP / Neo4j Browser) | Databricks VNet only |
| 7687 (Bolt) | Databricks VNet only |
| 7688 (Bolt Routing) | Databricks VNet only |

Bolt is intentionally blocked from public access; connections from outside the Databricks VNet must use an SSH tunnel. Browser access via HTTPS (7473) remains public. Decide whether this is the correct posture for your environment before deploying; if you need to restrict the browser UI as well, change the `HTTPS` rule in `infra/modules/neo4j-nsg-peering.bicep` to use `sourceAddressPrefix: databricksCidr`.

### SSH access

Port 22 is open to `Internet` in all NSG configurations, including after `peer-databricks-v2025`. For production, restrict the SSH source to a known CIDR using the `sshSourceCidr` parameter in `infra/main.bicep` and `infra/databricks-main.bicep`:

```bicep
// infra/main.bicep — restrict SSH to a corporate IP range
param sshSourceCidr string = '203.0.113.0/24'
```

Alternatively, deploy [Azure Bastion](https://learn.microsoft.com/azure/bastion/bastion-overview) in a dedicated subnet and set `sshSourceCidr` to the Bastion subnet CIDR so only Bastion can SSH to the nodes.

### VMSS public IPs in cluster deployments

Each VMSS instance is assigned a public IP and DNS label even in cluster deployments that sit behind the internal load balancer. This allows direct SSH access to individual nodes but also exposes them to the Internet (subject to the NSG). For production cluster deployments, set `publicIpEnabled: false` in `infra/main.bicep` to remove per-VM public IPs:

```bicep
// infra/main.bicep — remove per-node public IPs for cluster
param publicIpEnabled bool = false
```

When `publicIpEnabled` is `false`, the `Neo4jBrowserURL`, `sshHostname`, and `sshCommand` outputs return empty strings. Access to individual nodes requires Azure Bastion or an SSH tunnel through the load balancer. The load balancer and Private Link Service are unaffected.

### HTTP connector (port 7474)

The cloud-init scripts explicitly enable `server.http.listen_address=0.0.0.0:7474`. The NSG allows this port from the Internet in standalone and cluster scenarios (it is restricted to the Databricks VNet after peering). For production, disable the HTTP connector and serve the Neo4j Browser exclusively over HTTPS (7473) using the `enableHttp` parameter in `infra/main.bicep`:

```bicep
// infra/main.bicep — disable unencrypted HTTP connector
param enableHttp bool = false
```

When `enableHttp` is `false`, `server.http.enabled=false` is written to `neo4j.conf` at boot. The Neo4j Browser remains accessible on port 7473 (HTTPS). The deployment readiness check automatically falls back to a TCP check on Bolt (7687) when HTTP is unavailable.

**Not safe for cluster deployments yet.** The load balancer health probe (`httpprobe`) does an HTTP GET on port 7474. When `enableHttp` is `false`, that request gets no response, the probe fails for every backend, and the LB marks all instances unhealthy; Bolt connections on 7687 receive TCP RST even though Neo4j is running. Two items need to be addressed before `enableHttp=false` is usable in a cluster deployment:

1. **LB probe compatibility**: Add an HTTPS probe on port 7473 (or a dedicated health endpoint) and update the `inbound7687` and `inbound7688` rules to reference it when HTTP is disabled. The current `httpprobe` only works when the HTTP connector is on.

`enableHttp=false` is safe for **standalone** deployments where no load balancer is involved.

### SELinux set to permissive

The cloud-init scripts set SELinux to `permissive` mode (`setenforce 0`) to simplify the Neo4j installation. In permissive mode, SELinux logs policy violations but does not enforce them. For production deployments on RHEL/CentOS-based images, configure a proper Neo4j SELinux policy and restore `enforcing` mode rather than leaving the VM without mandatory access controls.

### Private Link Service visibility

The PLS is deployed with `visibility.subscriptions: ['*']`, which allows any Azure subscription to request a private endpoint connection. Manual approval is required (no auto-approval is configured), so no unauthorized connections can be established without an explicit `az network private-link-service connection update` approval. For tighter production controls, change the visibility to the specific Databricks subscription ID(s) that need access (`infra/modules/loadbalancer.bicep`, `privateLinkService` resource).

### Private Link Service NAT IP capacity

One NAT IP is configured on the PLS. Azure supports up to 8 NAT IPs per PLS, each providing additional TCP port capacity. For high-concurrency production workloads, add NAT IP configurations to `infra/modules/loadbalancer.bicep`. The `pls-subnet` (`10.0.2.0/28`) has space for up to 14 addresses.

### TCP keepalives for connections through Private Link

The Azure Private Link Service has a platform-level idle connection timeout of approximately 5 minutes that cannot be changed in Bicep. The load balancer rules are set to a 4-minute idle timeout. Connections that are idle longer than 4 minutes will be silently reset. Configure the Neo4j driver with a connection liveness check or TCP keepalive shorter than 4 minutes:

```python
# Python driver example
driver = GraphDatabase.driver(
    bolt_uri,
    auth=("neo4j", password),
    liveness_check_timeout=120,   # seconds — check idle connections every 2 min
)
```

### License type

All default scenarios use `licenseType: Evaluation`. Evaluation licenses are time-limited and not permitted for production use. Set `license_type: Enterprise` in `scenarios.yaml` and provide a valid Neo4j Enterprise license before deploying to production.

### VM patching

The VMSS upgrade policy is `Manual`. OS and Neo4j package updates must be applied manually via a rolling instance upgrade. For production, establish a patching schedule and test upgrades in a lower environment first; Neo4j cluster upgrades must follow a specific rolling procedure to avoid downtime.
