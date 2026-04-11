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

It then uploads `notebooks/neo4j_connectivity_test.ipynb` (repo root) to `/Shared/neo4j-peer-databricks-v2025-connectivity-test` in the workspace, with the scope name pre-filled.

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

`neo4j-connect` provides a CLI-based alternative that covers both VNet-internal health and the cross-VNet Databricks path without a PAT or an interactive notebook:

```bash
cd deployments
uv run neo4j-connect check --scenario peer-databricks-v2025
```

See [testing.md](testing.md) for the full reference.
