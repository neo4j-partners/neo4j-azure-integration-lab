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

### Post-deployment: Databricks secrets and notebooks

After a `peer-databricks-v2025` deployment, run `setup-databricks` to create the Databricks secrets scope and upload the connectivity test notebooks — including the serverless probe script used by `neo4j-connect check --compute serverless`:

```bash
uv run ansible-deploy setup-databricks --scenario peer-databricks-v2025
```

### Post-deployment: Private Link for serverless compute

To enable connectivity from Databricks **serverless** notebooks and SQL warehouses (which run outside the VNet-injected compute plane), run `setup-ncc` after deployment. This wires the Private Link Service on the Neo4j ILB to a Databricks Network Connectivity Configuration so serverless compute can reach Neo4j over a private path without VNet peering.

```bash
uv run ansible-deploy setup-ncc --scenario peer-databricks-v2025 --account-profile <databricks-account-admin-profile>
```

### Run connectivity checks

```bash
# Run all checks — Databricks classic and serverless auto-detected from deployment profile
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine ansible
```

See [testing.md](testing.md) for the full reference. See the [Neo4j + Databricks](#neo4j--databricks-private-connectivity) section below for full details on both commands, the `bolt://neo4j.private:7687` URI, and per-compute-mode check commands.

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

### Deployment overview

The `peer-databricks-v2025` scenario deploys a 3-node Neo4j cluster and an Azure Databricks workspace in the same region, wires them together with VNet peering, and replaces the Internet-open NSG rules with Databricks-scoped ones. After deployment the only path into the Neo4j cluster is from the Databricks container subnet — port 22 from the internet remains open for SSH.

The two playbooks run sequentially as a single `deploy` invocation:

1. `neo4j.yml` — VNet, NSG, identity, internal load balancer, 3-node VMSS
2. `databricks.yml` — Databricks NSGs, NAT gateway, delegated VNet, workspace (VNet injection + NPIP), VNet peering in both directions, NSG update

Partial state is written after step 1 so `cleanup` can find both resource groups even if step 2 fails mid-run.

### Classic compute connectivity

Classic compute uses VNet-injected job clusters that have a direct route to the Neo4j VNet via the peering. Run `setup-databricks` first to create the secrets scope and upload the connectivity notebooks — it uses an AAD token from the active `az login` session, no PAT required:

```bash
cd deployments
uv run ansible-deploy setup-databricks --scenario peer-databricks-v2025
```

Connect using the LB private IP printed in the `status` output. Use `neo4j://` so the driver fetches a routing table through the LB and discovers all three cluster nodes:

```python
from neo4j import GraphDatabase
driver = GraphDatabase.driver("neo4j://<lb-private-ip>:7687", auth=("neo4j", "<password>"))
driver.verify_connectivity()
```

Verify with the automated check:

```bash
cd deployments
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine ansible --compute classic
```

See [databricks-validate.md](databricks-validate.md) for the manual notebook workflow.

### Serverless compute connectivity

Serverless compute runs on Databricks-managed infrastructure outside the customer subscription and has no route through the VNet peering. Connectivity requires a Private Link Service on the Neo4j ILB (provisioned by the deployment) wired to a Databricks Network Connectivity Configuration. Run `setup-ncc` to create the NCC, attach it to the workspace, create a private endpoint rule pointing at the PLS, and approve the resulting connection:

```bash
cd deployments
uv run ansible-deploy setup-ncc --scenario peer-databricks-v2025 --account-profile <databricks-account-admin-profile>
```

From serverless notebooks, use `bolt://neo4j.private:7687`. Do not use `neo4j://` — the routing protocol returns VMSS node IPs that are not reachable from serverless infrastructure (see implementation notes).

Verify with the automated check:

```bash
cd deployments
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine ansible --compute serverless
```

See [testing.md](testing.md) for the full connectivity check reference.

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

**Uploading Python scripts to the workspace: `ImportFormat.AUTO`, not `SOURCE + language`**

`workspace.import_()` with `format=ImportFormat.SOURCE` and `language=Language.PYTHON` always creates an `ObjectType.NOTEBOOK` — not a plain file. The Databricks API docs confirm this: the `language` field is defined as "set only if the object type is `NOTEBOOK`". `SparkPythonTask` on serverless compute calls `open()` on the workspace file path; notebook objects return `ENOTSUP` (errno 95) when opened as binary. To upload a Python script that `SparkPythonTask` can execute, use `format=ImportFormat.AUTO` with no `language` argument. `AUTO` analyzes the file's content header: files that do not begin with `# Databricks notebook source` are imported as `ObjectType.FILE`. Additionally, `overwrite=True` does not work when the existing object type differs from the type being uploaded — delete the existing object first before re-uploading if the type may have changed.

**ansible-core 2.20: dict keys are not templated**

In ansible-core 2.20 and later, a YAML dict key containing a Jinja2 expression — `"{{ var }}": {}` — is passed as a literal string, not resolved. This breaks the Azure Managed Identity body, which requires `{ "<resource-id>": {} }` as the `userAssignedIdentities` value. The fix is to build the dict in a `set_fact` task using a Jinja2 dict expression where the variable is the key:

```yaml
- ansible.builtin.set_fact:
    vmss_user_assigned_identities: "{{ {neo4j_identity_id: {}} }}"
```

Any task body that needs a dynamic dict key requires the same pattern.

**bolt.advertised_address must be the private IP in peered deployments**

After the NSG update replaces the Internet-open rules with the Databricks CIDR, the Neo4j cluster returns its bolt routing table to connecting drivers. If `bolt.advertised_address` is set to the public DNS hostname, Databricks receives routing entries that its driver cannot reach — the NSG blocks everything except the four Neo4j ports from `192.168.0.0/16`. The `cluster.yaml.j2` cloud-init template sets `bolt.advertised_address` to `${PRIVATE_IP}:7687` for this reason. Do not change it to the public hostname.

**All LB probes use HTTP on port 7474, not TCP on the Bolt ports**

The `inbound7687` and `inbound7688` load-balancing rules both reference `httpprobe` (HTTP GET on port 7474) rather than separate TCP probes on 7687/7688. Neo4j 2026.x completes the TCP handshake on port 7687 before sending a Bolt banner, so a raw TCP probe would technically pass — but HTTP/7474 gives a single health gate for all three rules. With `enable_tcp_reset: true` on every rule, a failed probe sends TCP RST to clients (`errno 111`) rather than timing out silently.

**NSG `purge_rules` race condition during Databricks peering**

`nsg_update.yml` uses `purge_rules: true`, which replaces the full NSG ruleset in one Azure PUT. Azure's processing may briefly omit the `AzureLoadBalancerProbe` allow rule during the transition. With `numberOfProbes: 1` on the LB probe, a single missed probe is enough to mark all backends unhealthy — the LB then RSTs all inbound connections via `enableTcpReset: true`. Any Databricks notebook or driver connecting to the LB during this ~5-second window receives `errno 111`. A 30-second pause after the NSG PUT (`nsg_update.yml`, `Wait for LB probes to recover`) gives probes time to re-establish before deployment is declared complete.

**Databricks Serverless compute requires Private Link Service and NCC**

Databricks Serverless compute runs on Databricks-managed infrastructure outside the customer subscription — it is not VNet-injected and has no route to private RFC-1918 addresses. The VNet peering used by classic compute does not carry traffic for serverless workloads. To reach the Neo4j ILB from Serverless compute, the deployment must include:

1. A dedicated `pls-subnet` (`10.1.0.0/28`) with `privateLinkServiceNetworkPolicies: Disabled` (created by `tasks/network.yml` on cluster deployments)
2. A Private Link Service (`pls-neo4j`) attached to the ILB frontend IP, using `pls-subnet` for NAT IP allocation (created by `tasks/loadbalancer.yml`)
3. A Databricks Network Connectivity Configuration (NCC) attached to the workspace, with a Private Endpoint rule pointing at the PLS (provisioned by `ansible-deploy setup-ncc`)

Once the NCC is attached, Databricks Serverless compute resolves `neo4j.private` to the private endpoint IP and opens a TCP path through the PLS into the ILB. Classic compute connectivity via VNet peering is unaffected. Use `bolt://neo4j.private:7687` (not `neo4j://`) for Serverless connections — `neo4j://` triggers a routing table request that returns the VMSS node IPs, which are not reachable from Serverless infrastructure.

The automated `neo4j-connect check --compute classic` command uses a fresh VNet-injected job cluster. `neo4j-connect check --compute serverless` uses Serverless compute and requires `setup-ncc` to have been run first.

**Databricks managed resource group deletion order**

Azure Databricks attaches a system deny assignment to the managed resource group that prevents direct deletion while the workspace exists. Deleting the managed resource group directly returns `DenyAssignmentAuthorizationFailed`. The correct order is workspace resource group first — Azure then removes the deny assignment and auto-deletes the managed group. The CLI enforces this order.

**`initial.dbms.default_primaries_count` must equal `node_count` on cluster deployments**

Neo4j 5.x defaults this setting to `1`, meaning the `neo4j` user database is allocated to a single node at first startup. The `neo4j://` routing protocol hides the gap — the driver queries the routing table and selects a node that actually hosts the database. `bolt://` direct connections do not route, so two of three LB backends return `DatabaseNotFound`. The `cluster.yaml.j2` template sets `initial.dbms.default_primaries_count=${NODE_COUNT}` for this reason. The `initial.*` prefix means the setting is read once at first startup on each node — adding it to `neo4j.conf` on a running cluster has no effect. To reallocate the database on a live cluster, run `ALTER DATABASE neo4j SET TOPOLOGY 3 PRIMARIES` against the `system` database.

---

## Production Security Hardening

The default configuration is tuned for demo and testing use. Before moving to production, review the following.

### Network access

The `standalone-v2025` and `cluster-v2025` scenarios leave all Neo4j ports open to the Internet. The `peer-databricks-v2025` scenario replaces the NSG ruleset after peering, restricting database ports to the Databricks VNet:

| Port | standalone / cluster | After peer-databricks peering |
|------|----------------------|-------------------------------|
| 22 (SSH) | Open to Internet | Open to Internet |
| 7473 (HTTPS / Neo4j Browser) | Open to Internet | Open to Internet |
| 7474 (HTTP / Neo4j Browser) | Open to Internet | Databricks VNet only |
| 7687 (Bolt) | Open to Internet | Databricks VNet only |
| 7688 (Bolt Routing) | Open to Internet | Databricks VNet only |

If you need to restrict the browser UI as well, update the NSG rule for port 7473 in `tasks/nsg_update.yml` to use `databricks_vnet_cidr` as the source address prefix.

### SSH access

Port 22 is open to `Internet` in all NSG configurations. Set `ssh_source_cidr` in your scenario file to restrict SSH to a known CIDR:

```yaml
ssh_source_cidr: '203.0.113.0/24'
```

Alternatively, deploy [Azure Bastion](https://learn.microsoft.com/azure/bastion/bastion-overview) in a dedicated subnet and set `ssh_source_cidr` to the Bastion subnet CIDR so only Bastion can SSH to the nodes.

### VMSS public IPs in cluster deployments

Each VMSS instance is assigned a public IP and DNS label even in cluster deployments that sit behind the internal load balancer. For production cluster deployments, set `public_ip_enabled: false` in your scenario file:

```yaml
public_ip_enabled: false
```

When `public_ip_enabled` is `false`, individual nodes are accessible only via Azure Bastion or an SSH tunnel through another host. The internal load balancer is unaffected.

### Private Link Service visibility

The Private Link Service (`pls-neo4j`) is deployed with `visibility.subscriptions: ['*']`, which allows any Azure subscription to request a private endpoint connection to the PLS. This is acceptable for demo and testing use. In production, restrict visibility to the specific Databricks platform subscription ID:

1. Run `ansible-deploy setup-ncc` once on a test deployment.
2. After the PE connection is approved, retrieve the Databricks-side subscription ID:
   ```bash
   az network private-link-service show \
     --resource-group <neo4j-rg> \
     --name pls-neo4j \
     --query "privateEndpointConnections[*].{name:name, peId:privateEndpoint.id}" \
     --output json
   ```
3. Extract the subscription ID embedded in the `peId` value (the segment after `/subscriptions/`).
4. In `playbooks/tasks/loadbalancer.yml`, replace `"*"` in `visibility.subscriptions` with that subscription ID.

### NCC attachment side effects

`ansible-deploy setup-ncc` attaches a Network Connectivity Configuration to the Databricks workspace, which changes the workspace's serverless networking mode. In production, be aware of the following:

- Attaching an NCC takes up to 10 minutes to propagate. Restart any running serverless services in the workspace after attachment.
- Each Azure Databricks account supports up to 10 NCCs per region, with 100 private endpoints per region distributed across those NCCs.
- Use separate NCCs for different environments (dev, staging, production) rather than sharing one NCC across all workspaces.
- After attaching an NCC, re-run the classic compute VNet checks (`neo4j-connect check --checks databricks --compute classic`) to confirm the VNet peering path is unaffected by the networking mode change.

### HTTP connector (port 7474)

The cloud-init templates explicitly enable `server.http.listen_address=0.0.0.0:7474`. For production, disable the HTTP connector and serve the Neo4j Browser exclusively over HTTPS (7473) by setting `enable_http: false` in your scenario file:

```yaml
enable_http: false
```

**Not safe for cluster deployments yet.** The load balancer health probe (`httpprobe`) does an HTTP GET on port 7474. When `enable_http` is `false`, the probe gets no response, the LB marks all backends unhealthy, and Bolt connections on 7687 receive TCP RST even though Neo4j is running. `enable_http: false` is safe for **standalone** deployments where no load balancer is involved. For clusters, an HTTPS probe on port 7473 (or a dedicated health endpoint) would need to be added before this is usable.

### Browser and Bloom authentication bypass

The cloud-init templates set `dbms.security.http_auth_allowlist=/,/browser.*,/bloom.*`, which allows the Neo4j Browser UI and Bloom to be accessed without authentication headers. This is intended for evaluation use. For production, remove this line from both `playbooks/templates/standalone.yaml.j2` and `playbooks/templates/cluster.yaml.j2`.

### SELinux set to permissive

The cloud-init scripts set SELinux to `permissive` mode (`setenforce 0`) to simplify the Neo4j installation. In permissive mode, SELinux logs policy violations but does not enforce them. For production deployments on RHEL/CentOS-based images, configure a proper Neo4j SELinux policy and restore `enforcing` mode.

### License type

All default scenarios use `license_type: Enterprise`. Confirm that you have a valid Neo4j Enterprise license before deploying to production — Evaluation licenses are time-limited and not permitted for production use.

### VM patching

The VMSS upgrade policy is `Manual`. OS and Neo4j package updates must be applied manually via a rolling instance upgrade. For production, establish a patching schedule and test upgrades in a lower environment first — Neo4j cluster upgrades must follow a specific rolling procedure to avoid downtime.
