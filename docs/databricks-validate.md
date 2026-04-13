# Testing Databricks Connectivity

The `setup-databricks` command provisions Databricks Secrets and uploads a connectivity test notebook in a single step. Both the Bicep and Ansible CLIs share the same command interface. Run it after `deploy` completes.

---

## 1. Generate a Personal Access Token (PAT)

In the Databricks workspace: **User Settings → Developer → Access tokens → Generate new token**

Select the **Other APIs** tab under **Scope** and add two API scopes:

- `secrets` — required to create the secrets scope and upload the five keys
- `workspace` — required to import the connectivity test notebook

The **Generate** button stays disabled until at least one scope is selected.

---

## 2. Run the command

```bash
cd deployments

# Bicep path
uv run bicep-deploy setup-databricks --scenario peer-databricks-v2025 --token <pat>

# Ansible path
uv run ansible-deploy setup-databricks --scenario peer-databricks-v2025 --token <pat>
```

This creates a secrets scope named `neo4j-peer-databricks-v2025` containing five keys:

| Key | Value |
|---|---|
| `bolt_uri` | `bolt://<lb-private-ip>:7687` |
| `host` | `<lb-private-ip>` |
| `username` | `neo4j` |
| `password` | deployment password |
| `database` | `neo4j` |

It then uploads three artifacts to the workspace:

| Artifact | Workspace path | Purpose |
|---|---|---|
| `notebooks/neo4j_connectivity_test.ipynb` | `/Shared/neo4j-peer-databricks-v2025-connectivity-test` | Interactive classic compute test notebook |
| `notebooks/neo4j_serverless_connectivity_test.ipynb` | `/Shared/neo4j-peer-databricks-v2025-serverless-connectivity-test` | Interactive serverless compute test notebook |
| `notebooks/neo4j_classic_probe.py` | `dbfs:/neo4j/neo4j_classic_probe.py` | Probe script run by `neo4j-connect check` (classic path) |
| `notebooks/neo4j_serverless_probe.py` | `/Shared/neo4j-serverless-probe.py` | Probe script run by `neo4j-connect check` (serverless path) |

The classic notebook has the scope name pre-filled. The serverless notebook includes a widget for the private domain name (set by `setup-ncc`, default `neo4j.private`).

---

## 3. Run the notebook

Open the notebook in the Databricks workspace and run all cells. It validates three layers in order:

1. **TCP socket** to the internal load balancer on port 7687 — confirms VNet peering is active and the NSG allows Bolt traffic
2. **Python driver** — authenticates via Bolt and executes `RETURN 1`, confirming end-to-end connectivity
3. **Cluster topology** — runs `SHOW SERVERS` and passes if all three nodes report `ENABLED`

All cells are self-contained: each section prints `Status: PASS`, `Status: WARN`, or `Status: FAIL` with a description of what to check on failure.

---

## Options

```bash
# Use an existing ~/.databrickscfg profile instead of a token
uv run bicep-deploy setup-databricks --scenario peer-databricks-v2025 --profile <name>
uv run ansible-deploy setup-databricks --scenario peer-databricks-v2025 --profile <name>

# Upload the notebook to a custom workspace path
uv run bicep-deploy setup-databricks --scenario peer-databricks-v2025 --token <token> \
    --notebook-path /Users/me@example.com/neo4j-test
uv run ansible-deploy setup-databricks --scenario peer-databricks-v2025 --token <token> \
    --notebook-path /Users/me@example.com/neo4j-test
```

See [../playbook_validate.md](../playbook_validate.md) in the repo root for the full infrastructure verification checklist and a record of a completed test run.

---

## Automated connectivity checks

`neo4j-connect` provides a CLI-based alternative that covers both VNet-internal health and the Databricks path without a PAT or an interactive notebook. It auto-detects which compute paths to test from the deployment profile:

```bash
cd deployments

# Auto-detects classic and serverless based on deployment profile
uv run neo4j-connect check --scenario peer-databricks-v2025

# Classic VNet peering path only
uv run neo4j-connect check --scenario peer-databricks-v2025 --compute classic

# Serverless Private Link path only (requires setup-ncc to have run)
uv run neo4j-connect check --scenario peer-databricks-v2025 --compute serverless
```

After `setup-ncc` completes it writes NCC configuration to the deployment JSON. `neo4j-connect check` reads this and automatically adds the serverless test suite alongside the classic checks — no extra flags needed.

See [testing.md](testing.md) for the full reference.

---

## Serverless Compute Connectivity (Private Link)

VNet-injected job clusters connect to Neo4j via VNet peering. Serverless notebooks take a different network path — they run outside the injected VNet and require a separate private channel. `setup-ncc` provisions that channel using Azure Private Link.

### Run the command

```bash
cd deployments

uv run bicep-deploy setup-ncc --scenario peer-databricks-v2025
```

This single command:

1. Creates (or reuses) a Databricks Network Connectivity Configuration (NCC) named `neo4j-ncc` in the workspace region
2. Attaches the NCC to the workspace
3. Creates a private endpoint rule pointing at the `pls-neo4j` Private Link Service on the Neo4j load balancer
4. Polls for the Pending endpoint connection on the Azure PLS and approves it via the current `az login` session
5. Writes the NCC configuration (`domain_name`, `bolt_uri`, `ncc_configured: true`) to the deployment JSON so `neo4j-connect check` can auto-detect the serverless path on subsequent runs

No portal steps are required. The command prints the bolt URI to use when it completes:

```
bolt://neo4j.private:7687
```

### Connect from a serverless notebook

Open a **serverless** notebook in the Databricks workspace and run:

```python
%pip install neo4j -q
```

```python
from neo4j import GraphDatabase

driver = GraphDatabase.driver("neo4j://neo4j.private:7687", auth=("neo4j", "<password>"))
with driver.session() as s:
    print(s.run("RETURN 1 AS n").single()["n"])
driver.close()
```

Use `neo4j://` for full cluster-aware routing across all three nodes; `bolt://neo4j.private:7687` also works as a direct fallback. The hostname `neo4j.private` must match the `--domain-name` value used when `setup-ncc` was run (default: `neo4j.private`). Databricks resolves this hostname internally through the private endpoint; no external DNS is required.

### Options

```bash
# Use a different domain name for the private endpoint rule
uv run bicep-deploy setup-ncc --scenario peer-databricks-v2025 --domain-name my-neo4j.internal

# Use a different Private Link Service resource name (default: pls-neo4j)
uv run bicep-deploy setup-ncc --scenario peer-databricks-v2025 --pls-name my-pls
```

The command is idempotent — re-running it after a partial failure skips steps that already completed.
