# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Azure Bicep templates and Python CLI for deploying Neo4j Enterprise Edition on Azure VM Scale Sets. Supports standalone (1 node) or cluster (3-10 nodes) deployments with optional M2M bearer token authentication via Microsoft Entra ID.

## Commands

### Deployment CLI (from `deployments/` directory)

```bash
cd deployments
uv sync                                          # Install dependencies
uv run bicep-deploy setup                        # Interactive configuration wizard
uv run bicep-deploy deploy --scenario standalone-v2025    # Deploy standalone
uv run bicep-deploy deploy --scenario cluster-v2025       # Deploy 3-node cluster
uv run bicep-deploy setup-databricks --scenario peer-databricks-v2025  # Databricks secrets + notebook
uv run bicep-deploy setup-ncc --scenario peer-databricks-v2025        # NCC + Private Link (serverless)
uv run bicep-deploy status                       # Check deployment status
uv run bicep-deploy test                         # Test most recent deployment
uv run bicep-deploy cleanup --all --force        # Delete all resources
```

### Connectivity Tests (from `deployments/` directory)

```bash
uv run neo4j-connect check --scenario cluster-v2025 --checks vnet              # VNet-internal checks only
uv run neo4j-connect check --scenario peer-databricks-v2025 --checks vnet      # Peering + NSG checks
uv run neo4j-connect check --scenario peer-databricks-v2025 --compute classic  # Databricks classic compute
uv run neo4j-connect check --scenario peer-databricks-v2025 --compute serverless # Serverless via NCC/PLS
uv run neo4j-connect check --scenario peer-databricks-v2025 --compute both     # Classic + serverless
uv run neo4j-connect status                                                     # List all deployment profiles
```

### Bicep Validation

```bash
az bicep build --file infra/main.bicep           # Compile Bicep to ARM JSON
uv run bicep-deploy validate --scenario standalone-v2025  # Validate with what-if
```

### Bearer Token Validation (from `validate-bearer-token/` directory)

```bash
cd validate-bearer-token
export NEO4J_CLIENT_SECRET="your-secret"
uv run validate_bearer.py --scenario standalone-v2025
```

## Architecture

### Infrastructure (`infra/`)

- `main.bicep` - Orchestrator template, conditionally deploys load balancer for clusters (nodeCount >= 3)
- `modules/network.bicep` - VNet, Subnet, NSG with Neo4j ports (7473, 7474, 7687, 7688, 6000, 7000)
- `modules/vmss.bicep` - VM Scale Set with data disk and cloud-init
- `modules/loadbalancer.bicep` - Public load balancer for cluster deployments
- `modules/identity.bicep` - User-assigned managed identity
- `cloud-init/standalone.yaml` - Single-node Neo4j configuration
- `cloud-init/cluster.yaml` - Multi-node cluster configuration with discovery

Cloud-init templates use placeholder variables (`${admin_password}`, `${node_count}`, etc.) that are substituted at deployment time via Bicep's `replace()` function.

### Deployment CLI (`deployments/`)

Typer-based CLI with these key modules:
- `bicep_deploy.py` - Main entry point, all CLI commands
- `src/deployment.py` - DeploymentEngine for parameter generation and Azure deployment
- `src/orchestrator.py` - DeploymentOrchestrator for submission and output extraction
- `src/monitor.py` - DeploymentMonitor for live status tracking
- `src/config.py` - ConfigManager for settings/scenarios in `.arm-testing/config/`
- `src/m2m_setup.py` - M2M authentication setup via Azure CLI
- `src/cleanup.py` - Resource group deletion with cleanup modes

### Configuration Files

- `.arm-testing/config/settings.yaml` - Azure subscription, region, cleanup behavior
- `.arm-testing/config/scenarios.yaml` - Test scenario definitions
- `.deployments/{scenario}-{engine}.json` - Saved connection info after successful deployment (e.g. `peer-databricks-v2025-bicep.json`)

### M2M Authentication Flow

When M2M is enabled, the setup wizard creates Entra ID app registrations (API + Client) and the OIDC config is passed to cloud-init as a JSON blob in the `oidcConfig` parameter, which configures Neo4j's OIDC provider settings.

## Key Patterns

- Passwords are base64-encoded in Bicep to avoid shell escaping issues in cloud-init (NOT for security)
- Deployment state is tracked in `.arm-testing/state/active-deployments.json`
- Resource groups are tagged with scenario, deployment ID, owner, and cleanup mode
- Load balancer is only deployed when `nodeCount >= 3`
