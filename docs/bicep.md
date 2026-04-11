# Neo4j Enterprise — Bicep Deployment

Python CLI and Azure Bicep templates for deploying Neo4j Enterprise Edition on Azure VM Scale Sets. Supports standalone (1 node) or cluster (3–10 nodes) deployments.

## Quick Start

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

### 3-Node Neo4j EE with Load Balancer and Databricks

This flow deploys a 3-node Neo4j cluster behind an Azure load balancer, then peers a Databricks workspace into the same VNet. The two scenarios must be deployed in order — the first saves the VNet and NSG resource IDs that the second reads.

```bash
cd deployments

# Install dependencies and run the setup wizard
uv sync
uv run bicep-deploy setup

# 1. Deploy 3-node Neo4j cluster with load balancer
uv run bicep-deploy deploy --scenario cluster-v2025

# 2. Deploy Databricks workspace with VNet peering
uv run bicep-deploy deploy --scenario peer-databricks-v2025

# Create Databricks secrets scope and upload connectivity test notebook
uv run bicep-deploy setup-databricks --scenario peer-databricks-v2025

# Create NCC + Private Link Service endpoint for serverless compute connectivity
uv run bicep-deploy setup-ncc --scenario peer-databricks-v2025

# Run automated connectivity checks (VNet-internal + cross-VNet from Databricks)
uv run neo4j-connect check --scenario peer-databricks-v2025
# See docs/testing.md for the full reference

# Clean up all resources when done
uv run bicep-deploy cleanup --all --force
```

> **Note:** `cluster-v2025` deploys the load balancer automatically when `nodeCount >= 3`. The load balancer is internal — browser access requires an SSH tunnel.

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

The load balancer is provisioned automatically when `node_count >= 3`. It is internal — browser and Bolt access from a local machine requires an SSH tunnel to a cluster node. Saved to `.deployments/cluster-v2025-bicep.json` after deploy.

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

**This scenario must be deployed after `cluster-v2025`.** The Databricks VNet (`192.168.0.0/16`) is peered to the Neo4j VNet (`10.0.0.0/16`) in both directions. After peering, the Neo4j NSG is updated to restrict database ports (7473, 7474, 7687, 7688) to the Databricks CIDR only — browser access from outside the Databricks VNet requires an SSH tunnel. Saved to `.deployments/peer-databricks-v2025-bicep.json` after deploy.

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

## Databricks Integration

The integration requires two scenarios deployed in sequence: `cluster-v2025` deploys Neo4j and saves the VNet and NSG resource IDs, and `peer-databricks-v2025` reads those IDs to deploy the Databricks workspace, VNet, NAT gateway, and peering connections.

```bash
cd deployments
uv run bicep-deploy deploy --scenario cluster-v2025
uv run bicep-deploy deploy --scenario peer-databricks-v2025
```

See [databricks-validate.md](databricks-validate.md) for testing connectivity after deployment.
