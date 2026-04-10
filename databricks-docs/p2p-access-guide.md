# Private Databricks-to-Neo4j: Deployment and Access Guide

This guide walks through deploying a Neo4j standalone instance and a Databricks workspace connected over private VNet peering, verifying the peering, and testing the connectivity from a Databricks notebook. All Neo4j database ports are restricted to the Databricks VNet CIDR after the peering deployment; browser and Bolt access from a local machine requires an SSH tunnel.

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

- Configuration set up in `.arm-testing/config/settings.yaml` with a valid subscription ID, region, and resource group prefix. Run the setup wizard if not already done:

```bash
uv run neo4j-deploy setup
```

---

## Step 1: Deploy Neo4j

Deploy the standalone Neo4j instance. This creates a VMSS in its own resource group and VNet, and saves the VNet and NSG resource IDs to `.deployments/standalone-v2025.json` for use by the Databricks deployment.

```bash
cd deployments
uv run neo4j-deploy deploy --scenario standalone-v2025
```

When the deployment completes, connection details are printed to the terminal and saved to `.deployments/standalone-v2025.json`. Note the password from that output; it is generated once and not recoverable without accessing the file.

Confirm the network IDs were saved:

```bash
cat .deployments/standalone-v2025.json | python3 -m json.tool | grep -A3 '"network"'
```

The output should contain `vnet_id` and `nsg_id` fields. If either is missing, the Databricks deployment cannot proceed.

---

## Step 2: Deploy Databricks Peering

Deploy the Databricks workspace and VNet peering. This runs at subscription scope and writes into two resource groups: the existing Neo4j resource group (to add the peering connection and update the NSG) and a new Databricks resource group (to create the VNet, NAT gateway, and workspace).

```bash
uv run neo4j-deploy deploy --scenario peer-databricks-v2025
```

The deployment takes approximately 5 to 10 minutes. When it completes, the Databricks workspace URL is saved to `.deployments/peer-databricks-v2025.json`.

---

## Step 3: Verify the Deployment

### Peering status

Both VNet peering connections must show `Connected` before notebook traffic can reach Neo4j. Run the following after the deployment completes:

```bash
NEO4J_RG=$(cat .deployments/standalone-v2025.json | python3 -c "import json,sys; print(json.load(sys.stdin)['resource_group'])")
NEO4J_VNET=$(cat .deployments/standalone-v2025.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['network']['vnet_id'].split('/')[-1])")

DBX_RG="${NEO4J_RG}-dbx"
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
NSG_NAME=$(cat .deployments/standalone-v2025.json | python3 -c "import json,sys; print(json.load(sys.stdin)['network']['nsg_id'].split('/')[-1])")
az network nsg rule list --nsg-name "$NSG_NAME" -g "$NEO4J_RG" \
  --query '[].{name:name, priority:priority, src:sourceAddressPrefix, port:destinationPortRange, access:access}' \
  -o table
```

Ports 7473, 7474, 7687, and 7688 should list the Databricks CIDR as the source. Port 22 should list `Internet`. A `DenyDatabricks` rule at priority 200 should be present with `Deny` access and the Databricks CIDR as the source.

---

## Access Neo4j

### SSH

SSH access uses the public hostname output by the Neo4j deployment. The username is `neo4j`.

```bash
SSH_CMD=$(cat .deployments/standalone-v2025.json | python3 -c "import json,sys; print(json.load(sys.stdin)['ssh']['command'])")
$SSH_CMD
```

### Browser UI via SSH tunnel

After the peering NSG update, ports 7473, 7474, 7687, and 7688 no longer accept connections from the public internet. Use an SSH tunnel to reach the browser and Bolt endpoint from a local machine.

```bash
SSH_HOST=$(cat .deployments/standalone-v2025.json | python3 -c "import json,sys; print(json.load(sys.stdin)['ssh']['hostname'])")
ssh -L 7474:localhost:7474 -L 7687:localhost:7687 neo4j@"$SSH_HOST"
```

With the tunnel open, the Neo4j browser is available at `http://localhost:7474/`. Use the username `neo4j` and the password from the deployment output.

### Neo4j private IP (for Databricks connectivity)

The Neo4j VMSS is assigned a private IP within its subnet. Retrieve it with:

```bash
NEO4J_RG=$(cat .deployments/standalone-v2025.json | python3 -c "import json,sys; print(json.load(sys.stdin)['resource_group'])")
VMSS_NAME=$(az vmss list -g "$NEO4J_RG" --query '[0].name' -o tsv)
az vmss nic list --resource-group "$NEO4J_RG" --vmss-name "$VMSS_NAME" \
  --query '[0].ipConfigurations[0].privateIPAddress' -o tsv
```

Use this IP as the host in the Bolt URI from Databricks notebooks.

---

## Access Databricks

The workspace URL is saved to `.deployments/peer-databricks-v2025.json` after the peering deployment completes.

```bash
cat .deployments/peer-databricks-v2025.json | python3 -c "import json,sys; print(json.load(sys.stdin)['databricks']['workspace_url'])"
```

Open that URL in a browser. Authentication uses the Azure Active Directory credentials associated with the subscription.

---

## Test Connectivity from a Databricks Notebook

### Create a cluster

In the Databricks workspace, create a single-node cluster using any current LTS runtime. With SCC enabled, cluster nodes carry no public IP addresses.

Alternatively, create one from the CLI:

```bash
DBX_HOST=$(cat .deployments/peer-databricks-v2025.json | python3 -c "import json,sys; print('https://' + json.load(sys.stdin)['databricks']['workspace_url'])")

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

Once the cluster is in the `RUNNING` state, import and run the following notebook. Replace `<neo4j-private-ip>` with the private IP retrieved in the previous section, and `<password>` with the password from `.deployments/standalone-v2025.json`.

```python
# Cell 1
%pip install neo4j -q
```

```python
# Cell 2
from neo4j import GraphDatabase

NEO4J_URI      = "bolt://<neo4j-private-ip>:7687"
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

| File | Contents |
|------|----------|
| `.deployments/standalone-v2025.json` | Neo4j connection details, SSH info, VNet and NSG resource IDs |
| `.deployments/peer-databricks-v2025.json` | Databricks workspace URL and VNet resource ID |
| `.arm-testing/state/active-deployments.json` | Deployment IDs and statuses used by the cleanup command |

---

## Cleanup

```bash
cd deployments
uv run neo4j-deploy cleanup --all --force
```

This deletes all resource groups tracked in the active deployments state file, including the Neo4j resource group, the Databricks resource group, and the Databricks-managed resource group. If the managed resource group fails to delete because the workspace deletion did not complete, delete it manually:

```bash
az group delete --name "<databricks-rg>-managed" --yes
```
