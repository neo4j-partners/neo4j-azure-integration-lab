# Private Databricks-to-Neo4j: Deployment and Access Guide

This guide walks through deploying a three-node Neo4j Enterprise Edition cluster and a Databricks workspace connected over private VNet peering, verifying the peering, and testing the connectivity from a Databricks notebook. All Neo4j database ports are restricted to the Databricks VNet CIDR after the peering deployment; browser and Bolt access from a local machine requires an SSH tunnel.

The repository provides two equivalent deployment tools — Bicep templates under `infra/` and Ansible playbooks under `playbooks/` — exposed as two CLIs (`bicep-deploy` and `ansible-deploy`). Both produce the same Azure resources, write to `.deployments/{scenario}-{engine}.json` (e.g. `peer-databricks-v2025-bicep.json` or `peer-databricks-v2025-ansible.json`), and drive the same connectivity-test notebook. Pick one and use it consistently throughout this guide.

---

## Prerequisites

- Azure CLI installed and authenticated (`az login`)
- Databricks CLI installed (`databricks --version`)
- Python and `uv` installed
- Sufficient Azure subscription permissions: Contributor on the subscription (required to create resource groups and write peering connections across them)
- Repository cloned and dependencies installed:

```bash
cd deployments
uv sync
```

### Pick your deployment CLI

All commands in this guide use a `$CLI` shell variable so the same flow works with either tool. Set it once at the start of your session:

```bash
# Option A: Bicep
export CLI=bicep-deploy

# Option B: Ansible
export CLI=ansible-deploy
```

Configuration lives in `.arm-testing/config/settings.yaml` and is shared between both CLIs. Run the setup wizard if not already done:

```bash
uv run $CLI setup
```

---

## Step 1: Deploy Neo4j and the Databricks Peering

This step provisions the three-node Neo4j Enterprise Edition cluster (VMSS plus internal load balancer in their own resource group and VNet), the Databricks workspace with VNet injection and Secure Cluster Connectivity, and the bidirectional VNet peering between the two VNets. When it completes, all connection details — Neo4j password, LB private IP, Databricks workspace URL, and resource group names — are saved to `.deployments/peer-databricks-v2025-bicep.json` (Bicep) or `.deployments/peer-databricks-v2025-ansible.json` (Ansible).

**Ansible (single command):**

```bash
cd deployments
uv run ansible-deploy deploy --scenario peer-databricks-v2025
```

The playbook runs `neo4j.yml` and `databricks.yml` back-to-back and writes the merged state file at the end.

**Bicep (two commands):**

```bash
cd deployments
uv run bicep-deploy deploy --scenario cluster-v2025
uv run bicep-deploy deploy --scenario peer-databricks-v2025
```

The first command deploys Neo4j and writes `.deployments/cluster-v2025-bicep.json`. The second command deploys the Databricks workspace, establishes the peering, updates the NSG, and writes the merged `.deployments/peer-databricks-v2025-bicep.json` combining the Neo4j data from step one with the new Databricks workspace data.

The full deployment takes approximately 10 to 15 minutes. The Neo4j password is generated once and is not recoverable outside the saved JSON — note it from the terminal output or read it later with:

```bash
cat .deployments/peer-databricks-v2025-bicep.json | python3 -c "import json,sys; print(json.load(sys.stdin)['connection']['password'])"
```

---

## Step 2: Post-Deployment Setup

### Databricks secrets and connectivity notebook

Run `setup-databricks` to create the secrets scope and upload the connectivity test notebook to the workspace. This is used by **VNet-injected job clusters**.

```bash
cd deployments
uv run $CLI setup-databricks --scenario peer-databricks-v2025 --token <pat>
```

See [docs/databricks-validate.md](../docs/databricks-validate.md) for the full notebook walkthrough.

### Private Link for serverless notebooks (Bicep path only)

Run `setup-ncc` to create the Databricks Network Connectivity Configuration and the Private Link Service endpoint. This is the connectivity path for **serverless notebooks**, which run outside the injected VNet and cannot use VNet peering.

```bash
cd deployments
uv run bicep-deploy setup-ncc --scenario peer-databricks-v2025
```

This command creates an NCC named `neo4j-ncc`, attaches it to the workspace, creates a private endpoint rule pointing at the `pls-neo4j` Private Link Service, and approves the resulting endpoint connection — all from the current `az login` session. When it completes it prints the bolt URI to use from serverless notebooks:

```
bolt://neo4j.private:7687
```

See [docs/databricks-validate.md](../docs/databricks-validate.md#serverless-compute-connectivity-private-link) for the notebook snippet.

---

## Step 3: Verify the Deployment

### Peering status

Both VNet peering connections must show `Connected` before notebook traffic can reach Neo4j.

```bash
NEO4J_RG=$(cat .deployments/peer-databricks-v2025-bicep.json | python3 -c "import json,sys; print(json.load(sys.stdin)['resource_group'])")
DBX_RG=$(cat .deployments/peer-databricks-v2025-bicep.json | python3 -c "import json,sys; print(json.load(sys.stdin)['databricks_resource_group'])")

NEO4J_VNET=$(az network vnet list -g "$NEO4J_RG" --query '[0].name' -o tsv)
DBX_VNET=$(az network vnet list -g "$DBX_RG" --query '[0].name' -o tsv)

echo "--- Neo4j side ---"
az network vnet peering list --resource-group "$NEO4J_RG" --vnet-name "$NEO4J_VNET" -o table

echo "--- Databricks side ---"
az network vnet peering list --resource-group "$DBX_RG" --vnet-name "$DBX_VNET" -o table
```

Both rows should show `Connected` under the peering state column and `Succeeded` under provisioning state. If either shows `Initiated`, the other side of the peering has not yet been created; wait for the deployment to complete and recheck.

### NSG rules

Confirm the Neo4j NSG was updated to restrict database ports to the Databricks CIDR:

```bash
NSG_NAME=$(az network nsg list -g "$NEO4J_RG" --query '[0].name' -o tsv)
az network nsg rule list --nsg-name "$NSG_NAME" -g "$NEO4J_RG" \
  --query '[].{name:name, priority:priority, src:sourceAddressPrefix, port:destinationPortRange, access:access}' \
  -o table
```

Ports 7473, 7474, 7687, and 7688 should list the Databricks CIDR as the source. Port 22 should list `Internet`. A `DenyDatabricks` rule at priority 200 should be present with `Deny` access and the Databricks CIDR as the source.

---

## Access Neo4j

### SSH

SSH access uses the public hostname of an individual cluster node output by the Neo4j deployment (e.g. `vm0.neo4j-...`). The username is `neo4j`.

```bash
SSH_CMD=$(cat .deployments/peer-databricks-v2025-bicep.json | python3 -c "import json,sys; print(json.load(sys.stdin)['ssh']['command'])")
$SSH_CMD
```

### Browser UI via SSH tunnel

After the peering NSG update, ports 7473, 7474, 7687, and 7688 no longer accept connections from the public internet. Use an SSH tunnel to reach the browser and Bolt endpoint from a local machine.

```bash
SSH_HOST=$(cat .deployments/peer-databricks-v2025-bicep.json | python3 -c "import json,sys; print(json.load(sys.stdin)['ssh']['hostname'])")
ssh -L 7474:localhost:7474 -L 7687:localhost:7687 neo4j@"$SSH_HOST"
```

The SSH hostname (`vm0.neo4j-...`) is an individual cluster node's public address, not the internal load balancer. The internal LB handles only database ports 7473, 7474, 7687, and 7688; SSH traffic bypasses the LB entirely.

With the tunnel open, the Neo4j browser is available at `http://localhost:7474/`. Use the username `neo4j` and the password from the deployment output.

### LB private frontend IP (for Databricks connectivity)

The internal load balancer is assigned a private frontend IP within the Neo4j subnet. Databricks notebooks use this IP to connect through the load balancer to all three cluster nodes. It is saved to the deployment JSON:

```bash
cat .deployments/peer-databricks-v2025-bicep.json | python3 -c "import json,sys; print(json.load(sys.stdin)['connection']['lb_private_ip'])"
```

Use this LB private frontend IP as the host in the `neo4j://` URI from Databricks notebooks. The deployment JSON also exposes a ready-made `connection.databricks_bolt_uri` field (`bolt://<lb-ip>:7687`) that can be used directly.

---

## Access Databricks

The workspace URL is saved to `.deployments/peer-databricks-v2025-bicep.json` (or `-ansible.json`) after the peering deployment completes.

```bash
cat .deployments/peer-databricks-v2025-bicep.json | python3 -c "import json,sys; print(json.load(sys.stdin)['connection']['databricks_workspace_url'])"
```

Open that URL in a browser. Authentication uses the Azure Active Directory credentials associated with the subscription.

---

## Test Connectivity from a Databricks Notebook

### Create a cluster

In the Databricks workspace, create a single-node cluster using any current LTS runtime. With SCC enabled, cluster nodes carry no public IP addresses.

Alternatively, create one from the CLI:

```bash
DBX_HOST=$(cat .deployments/peer-databricks-v2025-bicep.json | python3 -c "import json,sys; print(json.load(sys.stdin)['connection']['databricks_workspace_url'])")

DATABRICKS_HOST="$DBX_HOST" databricks clusters create --json '{
  "cluster_name": "neo4j-test",
  "spark_version": "16.4.x-scala2.13",
  "node_type_id": "Standard_D2ads_v6",
  "num_workers": 0,
  "spark_conf": {
    "spark.databricks.cluster.profile": "singleNode",
    "spark.master": "local[*]"
  },
  "autotermination_minutes": 30
}'
```

### Run the connectivity test

Once the cluster is in the `RUNNING` state, import and run the following notebook. Replace `<lb-private-ip>` with the LB private frontend IP retrieved in the previous section, and `<password>` with the password from `.deployments/peer-databricks-v2025-bicep.json` (or `-ansible.json`).

```python
# Cell 1
%pip install neo4j -q
```

```python
# Cell 2
from neo4j import GraphDatabase

NEO4J_URI      = "neo4j://<lb-private-ip>:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "<password>"

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
driver.verify_connectivity()

with driver.session() as session:
    result = session.run("RETURN 'Connected via private VNet peering' AS msg, datetime() AS ts")
    record = result.single()
    print(record["msg"], record["ts"])

driver.close()
print("TEST PASSED")
```

The `neo4j://` scheme requests a routing table from the cluster through the LB so the driver discovers and uses all three nodes for reads and writes.

A successful run confirms the peering is active, the NSG allows traffic from the Databricks CIDR to port 7687, and the Neo4j driver session completes over the private path.

### Submit via CLI (optional)

To run the test without opening the workspace UI, import the notebook and submit it as a one-time job:

```bash
DATABRICKS_HOST="$DBX_HOST" databricks workspace import /neo4j-connectivity-test \
  --file neo4j_connectivity_test.py \
  --language PYTHON \
  --format SOURCE \
  --overwrite

CLUSTER_ID=$(DATABRICKS_HOST="$DBX_HOST" databricks clusters list --query "clusters[?cluster_name=='neo4j-test'].cluster_id | [0]" -o tsv 2>/dev/null || \
  DATABRICKS_HOST="$DBX_HOST" databricks clusters list | awk 'NR==2 {print $1}')

DATABRICKS_HOST="$DBX_HOST" databricks jobs submit --json "{
  \"run_name\": \"neo4j-p2p-test\",
  \"tasks\": [{
    \"task_key\": \"connectivity-test\",
    \"existing_cluster_id\": \"$CLUSTER_ID\",
    \"notebook_task\": { \"notebook_path\": \"/neo4j-connectivity-test\" },
    \"libraries\": [{\"pypi\": {\"package\": \"neo4j\"}}]
  }]
}"
```

---

## Deployment State Files

Both CLIs write to the same JSON schema under `.deployments/`, differentiated by an engine suffix.

| File | Contents |
|------|----------|
| `.deployments/peer-databricks-v2025-bicep.json` | Bicep — merged state after both Neo4j and Databricks deployments: Neo4j connection (URI, password, LB private IP), SSH info, resource group names, VNet and NSG resource IDs, and the Databricks workspace URL |
| `.deployments/peer-databricks-v2025-ansible.json` | Ansible — same schema, written by `ansible-deploy deploy --scenario peer-databricks-v2025` |
| `.deployments/cluster-v2025-bicep.json` | Bicep-only intermediate file written after step one of the two-command Bicep flow. Ansible does not produce this file because its deploy command is single-step |
| `.arm-testing/state/active-deployments.json` | Deployment IDs and statuses used by the cleanup command |

Key fields in `peer-databricks-v2025-{engine}.json`:

```
resource_group                        # Neo4j resource group
neo4j_resource_group                  # alias for resource_group
databricks_resource_group             # Databricks workspace RG
databricks_managed_resource_group     # Databricks-owned managed RG
connection.neo4j_uri                  # bolt:// or neo4j:// URI to the public address
connection.password                   # Neo4j admin password
connection.lb_private_ip              # internal LB frontend IP
connection.databricks_bolt_uri        # bolt://<lb-private-ip>:7687
connection.databricks_workspace_url   # https://<workspace>.azuredatabricks.net
connection.databricks_workspace_host  # workspace host without scheme
ssh.hostname / ssh.command            # public SSH target for admin access
network.vnet_id / network.nsg_id      # Neo4j VNet and NSG resource IDs
network.databricks_vnet_id            # Databricks VNet resource ID
```

---

## Cleanup

The two CLIs expose slightly different cleanup flags. Ansible cleans up by scenario; Bicep cleans up by deployment ID or via `--all`.

**Ansible:**

```bash
cd deployments
uv run ansible-deploy cleanup --scenario peer-databricks-v2025 --force
```

**Bicep:**

```bash
cd deployments
uv run bicep-deploy cleanup --all --force
```

Either command deletes all resource groups tracked in the deployment state file, including the Neo4j resource group, the Databricks resource group, and the Databricks-managed resource group. If the managed resource group fails to delete because the workspace deletion did not complete, delete it manually:

```bash
az group delete --name "<databricks-rg>-managed" --yes
```
