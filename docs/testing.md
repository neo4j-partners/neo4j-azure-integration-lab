# Neo4j Connectivity Testing

`neo4j-connect` is a CLI for verifying that a deployed Neo4j cluster is reachable — from within the Neo4j VNet and, when Databricks is peered, from inside the Databricks container subnet.

It reads `.deployments/{scenario}-{engine}.json`. The `--engine` flag is required on every command: pass `bicep` or `ansible` to match the CLI that deployed the infrastructure.

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
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine ansible
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine bicep

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

Submits a Python probe job that TCP- and Bolt-tests the Neo4j load balancer from inside Databricks compute. Two independent compute paths are covered: classic (VNet peering) and serverless (NCC Private Link). Each path is tested separately because a working VNet peer tells you nothing about whether the Private Link route is live.

#### Classic compute checks

Runs from a fresh job cluster provisioned into the Databricks-managed VNet that is peered with the Neo4j VNet. Takes ~8–10 minutes (cluster cold start plus PyPI installation).

| Check | What it verifies |
|---|---|
| Databricks workspace API | AAD token auth succeeds and workspace is reachable |
| Cross-VNet TCP 7687 | TCP socket to LB IP on port 7687 from inside the Databricks VNet |
| Cross-VNet TCP 7474 | TCP socket to LB IP on port 7474 from inside the Databricks VNet |
| Bolt driver (from Databricks classic) | Neo4j driver authenticates and `RETURN 1` executes through the VNet peer |
| Cluster topology (from Databricks classic) | `SHOW SERVERS` returns at least one enabled node |

The job always uses a **new cluster**, not an existing interactive one. A cluster started before a VNet peering or NSG change may not have picked up the new routes. A fresh cluster is provisioned after all infrastructure is in place.

#### Serverless compute checks

Runs from Databricks serverless compute, which has no access to the customer VNet and reaches Neo4j exclusively through the NCC Private Link route. Requires `setup-ncc` to have been run first. Takes ~3–5 minutes (serverless cold start is shorter than classic).

| Check | What it verifies |
|---|---|
| Databricks workspace API | AAD token auth succeeds and workspace is reachable |
| DNS resolution (from Databricks serverless) | Private endpoint domain name resolves inside the Databricks serverless subnet |
| TCP 7687 (from Databricks serverless) | TCP socket reaches Neo4j port 7687 via the Private Link route |
| TCP 7474 (from Databricks serverless) | TCP socket reaches Neo4j port 7474 via the Private Link route |
| Bolt driver (from Databricks serverless) | Neo4j driver authenticates over the Private Link path |
| Cluster topology (from Databricks serverless) | `SHOW SERVERS` returns at least one enabled node |

Both probe scripts ([`notebooks/neo4j_classic_probe.py`](../notebooks/neo4j_classic_probe.py) and [`notebooks/neo4j_serverless_probe.py`](../notebooks/neo4j_serverless_probe.py)) are uploaded automatically before each run, so they always reflect the current version on disk. `setup-databricks` is still required to create the secrets scope and upload the interactive connectivity notebooks — [`notebooks/neo4j_connectivity_test.ipynb`](../notebooks/neo4j_connectivity_test.ipynb) for classic compute and [`notebooks/neo4j_serverless_connectivity_test.ipynb`](../notebooks/neo4j_serverless_connectivity_test.ipynb) for serverless — to the workspace. `setup-ncc` is still required to provision the NCC and Private Link route.

#### Auto-detection

By default, `neo4j-connect check` runs all available paths. It runs classic checks when `databricks_workspace_host` is present in the deployment profile and adds serverless checks when `serverless.ncc_configured` is set (written by `setup-ncc`). Use `--compute` to override.

When both classic and serverless are active (`--compute both` or auto-detected), both jobs are submitted concurrently. Progress messages from both jobs interleave in the terminal; results are displayed in order after both complete. Combined run time is bounded by the slower path (~8–10 minutes rather than ~13–15 minutes sequential).

Databricks checks are skipped automatically when running `--checks all` against a profile that has no Databricks workspace. Running `--checks databricks` explicitly against such a profile exits with an error.

---

## Usage

```bash
cd deployments

# Run all checks — Databricks classic and serverless auto-detected from deployment profile
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine ansible
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine bicep

# VNet checks only — faster, no Databricks needed
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine ansible --checks vnet

# Databricks checks only — classic and serverless auto-detected
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine ansible --checks databricks

# Classic compute only (VNet peering path — does not require setup-ncc)
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine ansible --compute classic

# Serverless only (requires setup-ncc to have been run)
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine ansible --compute serverless

# Run both paths explicitly
uv run neo4j-connect check --scenario peer-databricks-v2025 --engine ansible --compute both

# List all deployment profiles across both engines
uv run neo4j-connect status
```

---

## Selecting an engine

`--engine` is required on every command. Pass `bicep` or `ansible` to match the CLI that deployed the infrastructure — the deployment file read is `.deployments/{scenario}-{engine}.json`.

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

---

## Unit tests

Unit tests cover the pure Python logic in the deployment tools. No Azure session, subscription, or deployed infrastructure is required.

```bash
cd deployments
uv run pytest
```

### What is covered

**`tests/test_deployment_output.py`** — Tests the shared JSON schema used by both the Bicep and Ansible engines.

| Area | What is verified |
|---|---|
| Model defaults | Each field in `ConnectionJSON`, `SSHJSON`, `ConfigurationJSON`, `NetworkJSON` initialises to the correct default |
| Nested model isolation | Two `DeploymentJSON` instances do not share nested model objects |
| `write_deployment_json` | Output file is valid JSON, is pretty-printed, creates missing parent directories, and overwrites cleanly on re-runs |
| Null field serialisation | Optional fields serialise to `null` rather than being omitted, so the schema stays consistent across engines |
| `display_connection_info` | Renderer handles all deployment states without raising: standalone, cluster, partial, Databricks peering, serverless Bolt URI |

**`tests/test_password.py`** — Tests password generation, environment variable reading, and prompt validation.

| Area | What is verified |
|---|---|
| Generated passwords | Always 24 characters, contain all four complexity categories, use only allowed characters |
| Caching | The same password is returned across multiple `get_password` calls; `clear_cache` resets state |
| Environment strategy | Reads `NEO4J_ADMIN_PASSWORD`; raises `ValueError` when the variable is unset or empty; warns when shorter than 12 characters |
| Prompt validation | Raises on empty, too-short (< 12), and too-long (> 72) input; rejects passwords with fewer than three complexity categories; accepts passwords at the exact boundaries (12 and 72 characters) |

### When to run

Run unit tests after any change to `src/deployment_output.py` or `src/password.py`, and before opening a pull request. They complete in under a second and require only `uv sync`.

The connectivity checks in the sections above require a completed deployment and an active Azure session. Unit tests have no such dependency and can run offline.
