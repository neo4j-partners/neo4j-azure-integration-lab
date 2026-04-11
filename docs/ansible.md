# Neo4j Enterprise — Ansible Playbooks

Ansible playbooks for deploying Neo4j Enterprise Edition on Azure VM Scale Sets. Supports standalone (single node) and cluster (three nodes) deployments.

All commands run from the **repo root** unless noted otherwise.

---

## One-time setup

Requires Python 3.12+ and the [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) installed and logged in.

```bash
az login
./playbooks/setup.sh
```

`setup.sh` installs Ansible, the `azure.azcollection`, its Python dependencies, and accepts the Neo4j Marketplace terms for your subscription.

Then configure the deployment CLI:

```bash
cd deployments
uv sync
uv run ansible-deploy setup
```

`uv sync` installs the Python dependencies (Ansible SDK, Databricks SDK, Typer, Rich, etc.) into an isolated virtual environment. `ansible-deploy setup` runs an interactive wizard that writes two files to `.arm-testing/config/`:

- `settings.yaml` — Azure subscription ID, default region, resource group prefix, owner email, password strategy, and cleanup behavior
- `scenarios.yaml` — not used by the Ansible CLI directly; the Ansible path reads scenario definitions from `playbooks/scenarios/*.yml` instead

The wizard only needs to run once per machine. Re-run it with `--force` if you need to change the subscription or region.

---

## Deploy

Pick one scenario and run it — each is a self-contained deployment.

```bash
cd deployments

# Single node — fastest to deploy, no load balancer, public access on all ports
uv run ansible-deploy deploy --scenario standalone-v2025

# Three-node cluster with internal load balancer, public access on all ports
uv run ansible-deploy deploy --scenario cluster-v2025

# Three-node cluster + Databricks workspace with private-only connectivity
uv run ansible-deploy deploy --scenario peer-databricks-v2025
```

Azure credentials are read automatically from your active `az login` session — no environment variables needed.

After deployment, connection details are printed and saved to `.deployments/{scenario}-ansible.json`.

```bash
uv run ansible-deploy status  --scenario standalone-v2025
uv run ansible-deploy cleanup --scenario standalone-v2025
```

---

## Scenarios

Scenario files live in `playbooks/scenarios/`. Each file is a YAML variable file passed to the playbook at deploy time.

| Scenario | File | Nodes | VM Size | Disk | Databricks |
|---|---|---|---|---|---|
| `standalone-v2025` | `playbooks/scenarios/standalone-v2025.yml` | 1 | Standard_D2s_v5 | 32 GB | No |
| `cluster-v2025` | `playbooks/scenarios/cluster-v2025.yml` | 3 | Standard_D4s_v5 | 100 GB | No |
| `peer-databricks-v2025` | `playbooks/scenarios/peer-databricks-v2025.yml` | 3 | Standard_D4s_v5 | 100 GB | Yes |

To create a custom scenario, copy the closest existing file and edit the values:

```bash
cp playbooks/scenarios/cluster-v2025.yml playbooks/scenarios/cluster-large-v2025.yml
```

```yaml
# playbooks/scenarios/cluster-large-v2025.yml
node_count: 5
vm_size: Standard_D8s_v5
graph_database_version: "2025"
license_type: Enterprise
disk_size: 256
```

To add Databricks peering to a custom scenario, set `databricks: true` — the CLI reads that flag to decide whether to run `databricks.yml`. Optionally set `databricks_resource_group_prefix` to give the Databricks resource group a stable name:

```yaml
databricks: true
databricks_resource_group_prefix: dbx-acme
```

---

## Connecting after deployment

Each VMSS instance gets a public DNS name derived from the resource group name:

```
vm000000.neo4j-{resource_suffix}.{location}.cloudapp.azure.com
```

The suffix is stable for a given resource group name — changing the name changes all resource names and the DNS hostname. The playbook prints the exact URL when it completes.

Log in to the Neo4j Browser with username `neo4j` and the password used at deploy time.

---

## Controlling resource group names

The CLI generates names as `{prefix}-ansible-{uuid}`. The prefix resolves in this order:

1. `--resource-group` flag on the deploy command (one-off override)
2. `resource_group_prefix` in the scenario file (permanent default for that scenario)
3. `resource_group_prefix` in `settings.yaml` (global fallback)

To give a scenario a stable prefix, set it in the scenario file:

```yaml
# playbooks/scenarios/cluster-acme-v2025.yml
resource_group_prefix: neo4j-acme
node_count: 3
...
```

This produces `neo4j-acme-ansible-{uuid}` every time that scenario is deployed. The `peer-databricks-v2025` scenario uses this approach — it sets both `resource_group_prefix: neo4j-p2p` and `databricks_resource_group_prefix: dbx-p2p` so both resource groups have identifiable prefixes.

For a one-off name override without editing the scenario file:

```bash
uv run ansible-deploy deploy --scenario cluster-v2025 --resource-group neo4j-acme
```

---

## Neo4j + Databricks (private connectivity)

The `peer-databricks-v2025` scenario deploys a 3-node Neo4j cluster and an Azure Databricks workspace in the same region, wires them together with VNet peering, and replaces the Internet-open NSG rules with Databricks-scoped ones. After deployment the only path into the Neo4j cluster is from the Databricks container subnet — port 22 from the internet remains open for SSH.

The two playbooks run sequentially as a single `deploy` invocation:

1. `neo4j.yml` — VNet, NSG, identity, internal load balancer, 3-node VMSS
2. `databricks.yml` — Databricks NSGs, NAT gateway, delegated VNet, workspace (VNet injection + NPIP), VNet peering in both directions, NSG update

Partial state is written after step 1 so `cleanup` can find both resource groups even if step 2 fails mid-run.

Connect from a Databricks notebook using the LB private IP printed in the `status` output. Use the `neo4j://` scheme so the driver fetches a routing table through the LB and discovers all three cluster nodes:

```python
from neo4j import GraphDatabase
driver = GraphDatabase.driver("neo4j://<lb-private-ip>:7687", auth=("neo4j", "<password>"))
driver.verify_connectivity()
```

Run automated connectivity checks after deployment:

```bash
cd deployments
uv run neo4j-connect check --scenario peer-databricks-v2025
```

See [testing.md](testing.md) for the full reference, and [databricks-validate.md](databricks-validate.md) for the manual notebook workflow.

---

## Cleanup

```bash
cd deployments
uv run ansible-deploy cleanup --scenario standalone-v2025
```

For combined scenarios the CLI deletes the Databricks workspace resource group first, then the Databricks managed resource group, then the Neo4j resource group. Azure auto-deletes the managed resource group when the workspace is removed; the second deletion is a no-op that handles partial-state cases where the workspace was never provisioned.

Or directly (Neo4j-only scenarios):

```bash
az group delete -n rg-neo4j-demo --yes
```

---

## Implementation notes

These are non-obvious behaviors discovered during development that affect anyone extending the playbooks.

**ansible-core 2.20: dict keys are not templated**

In ansible-core 2.20 and later, a YAML dict key containing a Jinja2 expression — `"{{ var }}": {}` — is passed as a literal string, not resolved. This breaks the Azure Managed Identity body, which requires `{ "<resource-id>": {} }` as the `userAssignedIdentities` value. The fix is to build the dict in a `set_fact` task using a Jinja2 dict expression where the variable is the key:

```yaml
- ansible.builtin.set_fact:
    vmss_user_assigned_identities: "{{ {neo4j_identity_id: {}} }}"
```

Any task body that needs a dynamic dict key requires the same pattern.

**bolt.advertised_address must be the private IP in peered deployments**

After the NSG update replaces the Internet-open rules with the Databricks CIDR, the Neo4j cluster returns its bolt routing table to connecting drivers. If `bolt.advertised_address` is set to the public DNS hostname, Databricks receives routing entries that its driver cannot reach — the NSG blocks everything except the four Neo4j ports from `192.168.0.0/16`. The `cluster.yaml.j2` cloud-init template sets `bolt.advertised_address` to `${PRIVATE_IP}:7687` for this reason. Do not change it to the public hostname.

**Databricks managed resource group deletion order**

Azure Databricks attaches a system deny assignment to the managed resource group that prevents direct deletion while the workspace exists. Deleting the managed resource group directly returns `DenyAssignmentAuthorizationFailed`. The correct order is workspace resource group first — Azure then removes the deny assignment and auto-deletes the managed group. The CLI enforces this order.
