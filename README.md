# Neo4j Enterprise Edition - Azure Deployment Template

Sample Azure Bicep templates for deploying Neo4j Enterprise Edition on Azure VM Scale Sets. This project provides a deployment solution with automated provisioning, cluster configuration, and enterprise authentication support. It includes a Python-based CLI tool for managing the full deployment lifecycle—from initial setup through validation and cleanup.

> **Disclaimer:** This is a sample template provided as-is and is not officially supported. It requires full security hardening and review before use in any production environment.

## Features

- **Standalone or Cluster**: Deploy 1 node (standalone) or 3-10 nodes (cluster)
- **Neo4j 2025.x**: Latest Neo4j Enterprise (2025.12+) with APOC plugin
- **Load Balancer**: Automatic load balancer for clusters (3+ nodes)
- **Cloud-init**: VM provisioning via cloud-init (no custom script extensions)
- **Security**: NSG with proper port configuration, SSRF protection
- **M2M Bearer Token Authentication**: OAuth 2.0 machine-to-machine authentication via Keycloak or Microsoft Entra ID for secure service-to-service connectivity

## Prerequisites

- Azure CLI installed and logged in (`az login`)
- Bicep CLI (included with Azure CLI 2.20+)
- [uv](https://docs.astral.sh/uv/) package manager
- Python 3.12+

## Quick Start

```bash
# Navigate to deployments directory
cd deployments

# Install dependencies
uv sync

# Run interactive setup wizard
uv run neo4j-deploy setup

# Deploy standalone Neo4j
uv run neo4j-deploy deploy --scenario standalone-v2025

# Deploy 3-node cluster
uv run neo4j-deploy deploy --scenario cluster-v2025

# Check deployment status
uv run neo4j-deploy status

# Test the deployment
uv run neo4j-deploy test

# Validate M2M bearer token authentication (if configured during setup)
export NEO4J_CLIENT_SECRET="your-client-secret"
cd ../validate-bearer-token
uv run validate_bearer.py --scenario standalone-v2025

# Clean up resources
cd ../deployments
uv run neo4j-deploy cleanup --all --force
```

> **Note:** The setup wizard includes optional M2M (Machine-to-Machine) bearer token authentication configuration via Keycloak or Microsoft Entra ID. See [M2M Bearer Token Authentication](#m2m-bearer-token-authentication) for details.

## Commands

| Command | Description |
|---------|-------------|
| `setup` | Interactive setup wizard for configuration |
| `validate` | Validate Bicep templates without deploying |
| `deploy` | Deploy one or more scenarios to Azure |
| `test` | Test deployment connectivity and license |
| `status` | Show deployment status |
| `cleanup` | Delete Azure resources |
| `report` | Generate test report for deployments |

## Configuration

After running `uv run neo4j-deploy setup`, configuration files are created in `deployments/.arm-testing/config/`:

- `settings.yaml` - Main settings (Azure subscription, regions, cleanup modes)
- `scenarios.yaml` - Test scenario definitions

### Default Scenarios

The setup wizard creates two default scenarios:

1. **standalone-v2025** - Single-node Neo4j 2025 deployment
2. **cluster-v2025** - 3-node Neo4j 2025 cluster

You can modify `scenarios.yaml` to add custom scenarios.

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


## Project Structure

```
azure-ee-template/
├── infra/                      # Azure infrastructure (Bicep templates)
│   ├── main.bicep              # Main orchestrator template
│   ├── modules/
│   │   ├── network.bicep       # VNet, Subnet, NSG
│   │   ├── identity.bicep      # Managed Identity
│   │   ├── loadbalancer.bicep  # Load Balancer (conditional)
│   │   └── vmss.bicep          # VM Scale Set
│   ├── cloud-init/
│   │   ├── standalone.yaml     # Single-node configuration
│   │   └── cluster.yaml        # Multi-node cluster configuration
│   ├── bicepconfig.json        # Bicep linter rules
│   └── parameters.json         # Sample parameters
├── deployments/                # Deployment CLI (Python/Typer)
│   ├── neo4j_deploy.py         # Main CLI entry point
│   ├── pyproject.toml          # Package configuration
│   └── src/                    # Source modules
├── keycloak-infra/             # Keycloak on Azure Container Apps
│   ├── deploy.sh               # Deploy/status/test/cleanup script
│   ├── main.bicep              # Bicep orchestrator for Keycloak
│   ├── modules/                # Container App, identity, environment
│   ├── Dockerfile              # Keycloak image with baked-in realm
│   └── realm-export.json       # Pre-configured Neo4j realm
├── oauth-java/                 # Java JDBC bearer token test client
│   ├── run.sh                  # Reads deployment info, runs test
│   ├── build.gradle.kts        # Gradle build (neo4j-jdbc driver)
│   └── src/                    # KeycloakTestClient.java
├── validate-bearer-token/      # Python bearer token validation
│   └── validate_bearer.py      # Acquire token, connect to Neo4j
├── keycloak-test-client/       # Python Keycloak token test client
├── databricks-docs/            # Private Databricks-to-Neo4j connectivity docs
│   ├── p2p-architecture.md     # Architecture reference
│   ├── p2p-access-guide.md     # Deployment and access guide
│   └── p2p-setup-questions.md  # Customer pre-engagement questions
└── .deployments/               # Saved deployment details (gitignored)
```

## Outputs

After deployment, you'll receive:

- `Neo4jBrowserURL`: URL to access Neo4j Browser
- `Neo4jClusterBrowserURL`: Load balancer URL (clusters only)
- `Username`: Default username (`neo4j`)

## M2M Bearer Token Authentication

The setup wizard includes an optional step to configure M2M (Machine-to-Machine) authentication. This allows services, APIs, and automated processes to connect to Neo4j using OAuth 2.0 bearer tokens. Two OIDC providers are supported:

- **Keycloak** — Deploy Keycloak to Azure Container Apps, then the wizard reads OIDC values from the Keycloak deployment automatically
- **Microsoft Entra ID** — Create app registrations via Azure CLI (automatic) or enter values manually

During setup, the wizard presents three options:

1. No M2M authentication
2. Keycloak (reads from `keycloak-infra/.deployment.json`)
3. Entra ID (automatic or manual Azure AD app registration)

### Keycloak Setup

Deploy Keycloak first, then run the Neo4j deployment wizard:

```bash
# Deploy Keycloak to Azure Container Apps
cd keycloak-infra
./deploy.sh deploy

# Verify Keycloak is running and tokens work
./deploy.sh test

# Run Neo4j setup — select "Keycloak" at the M2M step
cd ../deployments
uv run neo4j-deploy setup

# Deploy Neo4j (OIDC config is injected into neo4j.conf via cloud-init)
uv run neo4j-deploy deploy --scenario standalone-v2025
```

The wizard reads `keycloak-infra/.deployment.json` and configures the OIDC provider, discovery URI, audience, role mapping, and client credentials automatically.

See [keycloak-infra/README.md](keycloak-infra/README.md) for Keycloak deployment details.

### Entra ID Setup

The wizard uses Azure CLI (`az`) commands to:

1. Detect your Azure tenant from your current `az login` session
2. Create an API app registration with Neo4j roles (`Neo4j.Admin`, `Neo4j.ReadWrite`, `Neo4j.ReadOnly`)
3. Create a client app registration and generate a client secret
4. Grant API permissions and assign roles

### Validating Bearer Token Authentication

**Python (both providers):**

```bash
cd validate-bearer-token
uv run validate_bearer.py --scenario standalone-v2025
```

The script detects the provider type from `.deployments/standalone-v2025.json` and acquires a token from the correct endpoint.

**Java JDBC (Keycloak):**

```bash
cd oauth-java
./run.sh
```

The script reads deployment info, acquires a Keycloak token, and connects to Neo4j using the [Neo4j JDBC driver](https://github.com/neo4j/neo4j-jdbc) with `authScheme=bearer`. It verifies the connection and displays the OIDC-mapped roles.

## Databricks Integration

The `databricks-docs/` directory covers private connectivity between a Databricks workspace and a deployed Neo4j instance over Azure VNet peering. All traffic travels over the Microsoft network backbone with no public exposure for database ports or cluster nodes.

| Document | Description |
|----------|-------------|
| [Architecture](databricks-docs/p2p-architecture.md) | How the private path is designed, what each component does, and what cannot be changed after deployment |
| [Deployment and Access Guide](databricks-docs/p2p-access-guide.md) | Step-by-step guide to deploy, verify, and test connectivity from a Databricks notebook |
| [Customer Setup Questions](databricks-docs/p2p-setup-questions.md) | Pre-engagement questions to confirm whether VNet injection and peering are feasible |

The integration requires two scenarios deployed in sequence: `standalone-v2025` deploys Neo4j and saves the VNet and NSG resource IDs, and `peer-databricks-v2025` reads those IDs to deploy the Databricks workspace, VNet, NAT gateway, and peering connections.

```bash
cd deployments
uv run neo4j-deploy deploy --scenario standalone-v2025
uv run neo4j-deploy deploy --scenario peer-databricks-v2025
```

See [databricks-docs/p2p-access-guide.md](databricks-docs/p2p-access-guide.md) for the full walkthrough, including peering verification, NSG confirmation, and a connectivity test from a Databricks notebook.

## License

Neo4j Enterprise Edition requires a valid license. Use `licenseType=Evaluation` for a 30-day trial.
