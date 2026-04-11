# Neo4j Connectivity Testing

`neo4j-connect` is an engine-agnostic CLI for verifying that a deployed Neo4j cluster is reachable — from within the Neo4j VNet and, when Databricks is peered, from inside the Databricks container subnet.

It reads `.deployments/{scenario}-bicep.json` or `.deployments/{scenario}-ansible.json`, so it works the same way regardless of which CLI deployed the infrastructure.

---

## Prerequisites

- `az login` session with Contributor on the subscription
- A completed deployment (bicep or ansible) for the scenario you want to test

No Databricks PAT is required. Databricks checks authenticate using an AAD token from the active `az login` session.

---

## Quick start

```bash
cd deployments

# Run all checks for a scenario
uv run neo4j-connect check --scenario peer-databricks-v2025

# List all deployment profiles
uv run neo4j-connect status
```

---

## Check suites

### VNet checks (`--checks vnet`)

Runs from the local machine via the Azure CLI and `az vmss run-command invoke`. No Databricks cluster is needed. Takes ~3–5 minutes.

| Check | What it verifies |
|---|---|
| VNet peering (neo4j→dbx) | Peering link is `Connected` and `FullyInSync` |
| VNet peering (dbx→neo4j) | Return-direction peering is also `Connected` |
| NSG Bolt rule | NSG has an `Allow` rule on port 7687 |
| NSG AzureLoadBalancerProbe rule | NSG allows the LB health probe source tag |
| VMSS instances state | All instances are in `Succeeded` provisioning state |
| LB probe configuration | No LB probe uses raw TCP on ports 7687 or 7688 |
| Neo4j service (per node) | `systemctl is-active neo4j` and ports 7474/7687 listening |
| LB HTTP 7474 | `curl http://LB_IP:7474/` returns HTTP 200 |
| LB TCP 7687 | TCP connection to `LB_IP:7687` succeeds |
| Bolt end-to-end | `cypher-shell RETURN 1` through the LB succeeds |

Checks 7–10 run from **inside the Neo4j VNet** via `az vmss run-command invoke`. For LB checks, all instances are tried in turn — any single success counts as pass. This avoids false failures from the Azure Standard ILB hairpin limitation (a VM cannot reliably reach its own LB frontend).

Peering checks are skipped automatically when the deployment has no Databricks resource group.

VNet checks are useful for **any cluster deployment**, not just Databricks-peered ones. Run them against `cluster-v2025` to verify the cluster is healthy before adding Databricks.

### Databricks checks (`--checks databricks`)

Submits a Python job to a fresh Databricks cluster that TCP-probes the Neo4j load balancer from inside the Databricks container subnet. This is the only test that proves the actual Databricks → Neo4j path. Takes ~5–8 minutes (cluster cold start).

| Check | What it verifies |
|---|---|
| Databricks workspace API | AAD token auth succeeds and workspace is reachable |
| Cross-VNet TCP 7687 | `socket.create_connection(lb_ip, 7687)` from Databricks job cluster |
| Cross-VNet TCP 7474 | `socket.create_connection(lb_ip, 7474)` from Databricks job cluster |

The job always uses a **new cluster**, not an existing interactive one. This matters: a cluster started before a VNet peering or NSG change may not have picked up the new routes. A fresh cluster is provisioned after all infrastructure is in place.

Databricks checks are skipped automatically when running `--checks all` against a profile that has no Databricks workspace. Running `--checks databricks` explicitly against such a profile exits with an error.

---

## Usage

```bash
cd deployments

# Run both check suites (Databricks checks skipped if no workspace in profile)
uv run neo4j-connect check --scenario peer-databricks-v2025

# VNet checks only — faster, no Databricks needed
uv run neo4j-connect check --scenario peer-databricks-v2025 --checks vnet

# Databricks checks only — cross-VNet TCP probe
uv run neo4j-connect check --scenario peer-databricks-v2025 --checks databricks

# List all deployment profiles across both engines
uv run neo4j-connect status
```

---

## Selecting a profile when both engines exist

When both `-bicep.json` and `-ansible.json` exist for a scenario, `neo4j-connect` uses the most recently modified file. To choose explicitly:

```bash
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine bicep
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine ansible
```

---

## Writing results to a markdown file

Pass `--update-doc` to insert a results table into a markdown file. On re-runs the previous section is replaced in place — results don't accumulate.

```bash
uv run neo4j-connect check --scenario peer-databricks-v2025 \
    --update-doc no-connect-v2.md
```

---

## Exit codes

`neo4j-connect check` exits `0` when all checks pass and `1` when any check fails. This makes it usable in CI or scripted workflows:

```bash
uv run neo4j-connect check --scenario peer-databricks-v2025 --checks vnet || exit 1
```
