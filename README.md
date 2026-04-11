# Neo4j Enterprise Edition - Azure Deployment Template

Deployment tooling for Neo4j Enterprise Edition on Azure VM Scale Sets. Two deployment paths are available: a Bicep path using Azure Bicep templates backed by a `bicep-deploy` Python CLI, and an Ansible path using Ansible playbooks backed by an `ansible-deploy` CLI. Both paths support standalone (1 node) and cluster (3–10 nodes) topologies and share the same command interface — setup, deploy, test, status, and cleanup — covering the full deployment lifecycle.

Three optional integration layers extend the base deployment:

- **Private Databricks connectivity**: deploys an Azure Databricks workspace with VNet injection, connects it to the Neo4j cluster via Azure VNet peering (job clusters) and Azure Private Link (serverless notebooks), and scopes NSG rules so Bolt traffic travels only on the private network
- **M2M bearer token authentication**: configures Neo4j's OIDC provider using either Keycloak (deployed to Azure Container Apps) or Microsoft Entra ID, enabling service-to-service connections without static credentials
- **Databricks connectivity validation**: provisions a Databricks secrets scope and uploads a test notebook that validates TCP connectivity, Bolt authentication, and cluster topology in sequence

> **Disclaimer:** This is a sample template provided as-is and is not officially supported. It requires full security hardening and review before use in any production environment.

## Features

- **Two deployment paths**: Bicep path (`bicep-deploy`) using Azure Bicep templates; Ansible path (`ansible-deploy`) using Ansible playbooks — both share the same CLI interface
- **Standalone or Cluster**: Deploy 1 node (standalone) or 3-10 nodes (cluster)
- **Neo4j 2025.x**: Latest Neo4j Enterprise (2025.12+) with APOC plugin
- **Load Balancer**: Automatic internal load balancer for clusters (3+ nodes)
- **Cloud-init**: VM provisioning via cloud-init (no custom script extensions)
- **Private Databricks connectivity**: Databricks workspace with VNet injection, connected to Neo4j via Azure VNet peering with NSG rules scoped to the Databricks container subnet
- **Security**: NSG with proper port configuration, SSRF protection
- **M2M Bearer Token Authentication**: OAuth 2.0 machine-to-machine authentication via Keycloak or Microsoft Entra ID

## Prerequisites

- Azure CLI installed and logged in (`az login`)
- Bicep CLI (included with Azure CLI 2.20+)
- [uv](https://docs.astral.sh/uv/) package manager
- Python 3.12+

---

## Bicep Deployment

Python CLI (`bicep-deploy`) backed by Azure Bicep templates. Covers setup, deploy, test, status, and cleanup commands for standalone and cluster scenarios. Also supports optional Databricks VNet peering via a `peer-databricks-v2025` scenario.

See [docs/bicep.md](docs/bicep.md) for full details.

### Testing Databricks Connectivity

After deploying the `peer-databricks-v2025` scenario:

- Run `setup-databricks` to provision secrets and upload the connectivity test notebook (VNet-injected job cluster path).
- Run `setup-ncc` to configure the Databricks NCC and Azure Private Link endpoint (serverless notebook path).

See [docs/databricks-validate.md](docs/databricks-validate.md) for the full walkthrough of both paths.

---

## Ansible Deployment

Ansible playbooks for deploying Neo4j on Azure VM Scale Sets. Supports standalone, cluster, and cluster-with-Databricks scenarios via a lightweight `ansible-deploy` CLI.

See [docs/ansible.md](docs/ansible.md) for full details.

### Testing Databricks Connectivity

After deploying the `peer-databricks-v2025` scenario, run `setup-databricks` to provision secrets and upload the connectivity test notebook.

See [docs/databricks-validate.md](docs/databricks-validate.md) for the full walkthrough.

---

## Databricks Deployment

The `peer-databricks-v2025` scenario extends the base cluster deployment to include an Azure Databricks workspace with VNet injection and private connectivity to Neo4j. Both the Bicep and Ansible CLIs support this scenario.

The deployment provisions two resource groups: one for the 3-node Neo4j cluster (VNet, NSG, internal load balancer, VMSS) and one for Databricks (NAT gateway, delegated VNet, workspace). After both are provisioned, VNet peering is established in both directions and the Neo4j NSG rules are replaced with Databricks-scoped rules — the only inbound path to Neo4j ports is from the Databricks container subnet.

See [databricks-docs/p2p-architecture.md](databricks-docs/p2p-architecture.md) for architecture details and [databricks-docs/p2p-access-guide.md](databricks-docs/p2p-access-guide.md) for the deployment walkthrough.

---

## M2M Bearer Token Authentication

Optional OAuth 2.0 machine-to-machine authentication configured during the setup wizard. Supports Keycloak (deployed to Azure Container Apps) and Microsoft Entra ID. When enabled, the OIDC configuration is injected into Neo4j via cloud-init.

See [docs/m2m.md](docs/m2m.md) for full details.

---

## Project Structure

```
azure-ee-template/
├── docs/                       # Deployment and feature guides
│   ├── bicep.md                # Bicep CLI reference
│   ├── ansible.md              # Ansible playbooks reference
│   ├── databricks-validate.md  # Databricks connectivity testing
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
