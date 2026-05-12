# Neo4j Enterprise Edition - Azure Deployment Template

Deployment tooling for Neo4j Enterprise Edition on Azure VM Scale Sets. Two deployment paths are available: a Bicep path using Azure Bicep templates backed by a `bicep-deploy` Python CLI, and an Ansible path using Ansible playbooks backed by an `ansible-deploy` CLI. Both paths support standalone (1 node) and cluster (3–10 nodes) topologies and share the same command interface: setup, deploy, test, status, and cleanup, covering the full deployment lifecycle.

Three optional integration layers extend the base deployment:

- **Private Databricks connectivity**: deploys an Azure Databricks workspace with VNet injection and connects it to the Neo4j cluster over two private network layers: Azure VNet peering for VNet-injected job clusters, and Azure Private Link + a Databricks Network Connectivity Configuration (NCC) for serverless compute. NSG rules are scoped so Bolt traffic never traverses the public internet on either path
- **M2M bearer token authentication**: configures Neo4j's OIDC provider using either Keycloak (deployed to Azure Container Apps) or Microsoft Entra ID, enabling service-to-service connections without static credentials
- **Databricks connectivity validation**: provisions a Databricks secrets scope and uploads a test notebook that validates TCP connectivity, Bolt authentication, and cluster topology in sequence

> **Production hardening required:** The default configuration is tuned for evaluation and testing. Before deploying to production, review the hardening guidance for your deployment path: [Bicep: Production Security Hardening](docs/bicep.md#production-security-hardening) or [Ansible: Production Security Hardening](docs/ansible.md#production-security-hardening).

> **Disclaimer:** This is a sample template provided as-is and is not officially supported by Neo4j.

## Features

- **Two deployment paths**: Bicep path (`bicep-deploy`) using Azure Bicep templates; Ansible path (`ansible-deploy`) using Ansible playbooks; both share the same CLI interface
- **Standalone or Cluster**: Deploy 1 node (standalone) or 3-10 nodes (cluster)
- **Neo4j 2025.x**: Latest Neo4j Enterprise (2025.12+) with APOC plugin
- **Load Balancer**: Automatic internal load balancer for clusters (3+ nodes)
- **Cloud-init**: VM provisioning via cloud-init (no custom script extensions)
- **Private Databricks connectivity**: Databricks workspace with VNet injection; VNet peering for classic job-cluster connectivity and Azure Private Link + NCC for serverless compute connectivity; NSG rules scoped to the Databricks CIDR on all Neo4j ports
- **Security**: NSG with proper port configuration, SSRF protection
- **M2M Bearer Token Authentication**: OAuth 2.0 machine-to-machine authentication via Keycloak or Microsoft Entra ID

## Prerequisites

- Azure CLI installed and logged in (`az login`)
- Bicep CLI (included with Azure CLI 2.20+)
- [uv](https://docs.astral.sh/uv/) package manager
- Python 3.12+

## Databricks connectivity: two deployment layers

The `peer-databricks-v2025` scenario deploys Databricks connectivity in two distinct layers:

**Layer 1: VNet peering (classic compute, required)**
Deployed as part of `bicep-deploy deploy --scenario peer-databricks-v2025` or `ansible-deploy deploy --scenario peer-databricks-v2025`. Establishes bidirectional VNet peering between the Databricks workspace VNet and the Neo4j VNet, and restricts Neo4j NSG rules to the Databricks CIDR. VNet-injected job clusters reach Neo4j directly over the peering.

**Layer 2: Private Link + NCC (serverless compute, optional)**
Deployed via `setup-ncc`. Creates an Azure Private Link Service on the Neo4j load balancer and a Databricks Network Connectivity Configuration that routes serverless compute traffic through it. Required only for serverless workloads.

Serverless compute covers more of the Databricks platform than the name suggests. SQL warehouses (the default compute for Databricks SQL and interactive queries), serverless jobs, Lakeflow Spark Declarative Pipelines, and Mosaic AI model serving endpoints all run on serverless infrastructure. An AI agent that queries Neo4j from a Databricks serving endpoint runs on serverless, not on a VNet-injected job cluster. Without Layer 2, none of those workloads can reach Neo4j over a private path.

See [databricks-docs/p2p-architecture.md](databricks-docs/p2p-architecture.md) for full architecture details.

---

## Bicep Deployment

Python CLI (`bicep-deploy`) backed by Azure Bicep templates. Covers setup, deploy, test, status, and cleanup commands for standalone and cluster scenarios. Also supports optional Databricks VNet peering via a `peer-databricks-v2025` scenario.

See [docs/bicep.md](docs/bicep.md) for full details.

> **Production use:** Review the [Production Security Hardening](docs/bicep.md#production-security-hardening) section before deploying to a production environment.

---

## Ansible Deployment

Ansible playbooks for deploying Neo4j on Azure VM Scale Sets. Supports standalone, cluster, and cluster-with-Databricks scenarios via a lightweight `ansible-deploy` CLI.

See [docs/ansible.md](docs/ansible.md) for full details.

> **Production use:** Review the [Production Security Hardening](docs/ansible.md#production-security-hardening) section before deploying to a production environment.

---

## Databricks Deployment

The `peer-databricks-v2025` scenario extends the base cluster deployment to include an Azure Databricks workspace with VNet injection and private connectivity to Neo4j. Both the Bicep and Ansible CLIs support this scenario.

The deployment provisions two resource groups: one for the 3-node Neo4j cluster (VNet, NSG, internal load balancer, VMSS) and one for Databricks (NAT gateway, delegated VNet, workspace). After both are provisioned, VNet peering is established in both directions and the Neo4j NSG rules are replaced with Databricks-scoped rules; the only inbound path to Neo4j ports is from the Databricks container subnet.

See [databricks-docs/p2p-architecture.md](databricks-docs/p2p-architecture.md) for architecture details and [databricks-docs/p2p-access-guide.md](databricks-docs/p2p-access-guide.md) for the deployment walkthrough.

---

## M2M Bearer Token Authentication

Optional OAuth 2.0 machine-to-machine authentication configured during the setup wizard. Supports Keycloak (deployed to Azure Container Apps) and Microsoft Entra ID. When enabled, the OIDC configuration is injected into Neo4j via cloud-init.

See [docs/m2m.md](docs/m2m.md) for full details.

---

## Project Structure

```
neo4j-azure-integration-lab/
├── docs/                       # Deployment and feature guides
│   ├── bicep.md                # Bicep CLI reference
│   ├── ansible.md              # Ansible playbooks reference
│   └── m2m.md                  # M2M bearer token authentication
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
├── notebooks/                  # Shared Databricks notebooks
│   └── neo4j_connectivity_test.ipynb  # Connectivity test (used by both CLIs)
├── deployments/                # Deployment CLI (Python/Typer)
│   ├── bicep_deploy.py         # Bicep CLI entry point
│   ├── ansible_deploy.py       # Ansible CLI entry point
│   ├── pyproject.toml          # Package configuration
│   └── src/                    # Source modules
│       └── databricks_setup.py # Shared Databricks secrets + notebook upload
├── playbooks/                  # Ansible playbooks and scenario files
│   ├── neo4j.yml               # Neo4j deployment playbook
│   ├── databricks.yml          # Databricks deployment playbook
│   └── scenarios/              # Per-scenario variable files
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

## License

Neo4j Enterprise Edition requires a valid license. Use `licenseType=Evaluation` for a 30-day trial.
