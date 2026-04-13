"""
Shared Databricks setup: secrets scope creation, notebook/script upload, and NCC setup.

Used by both the Ansible CLI (ansible_deploy.py) and the Bicep CLI (bicep_deploy.py).
Each CLI handles its own authentication and reads the deployment JSON — this module
only handles the Databricks API calls that are identical between the two paths.
"""

import base64
import io
import json as _json
import subprocess
import time
from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console()

# Notebooks live at the repo root, one level above deployments/
NOTEBOOKS_DIR = Path(__file__).parent.parent.parent / "notebooks"

# Permanent DBFS path for the classic probe script — reused by ansible-deploy test
DBFS_PROBE_PATH = "dbfs:/neo4j/neo4j_classic_probe.py"

# Workspace path for the serverless probe script (no DBFS — serverless cannot use DBFS)
WORKSPACE_SERVERLESS_PROBE_PATH = "/Shared/neo4j-serverless-probe.py"

# Classic probe script lives on disk alongside the serverless probe.
# Both are read at runtime to avoid duplicating content in this file.
_CLASSIC_PROBE_PATH = NOTEBOOKS_DIR / "neo4j_classic_probe.py"


def run_databricks_setup(
    *,
    workspace_host: str,
    bolt_uri: str,
    username: str,
    password: str,
    database: str,
    scope_name: str,
    notebook_path: str,
    client,
    dbfs_probe_path: str = DBFS_PROBE_PATH,
    serverless_probe_path: str = WORKSPACE_SERVERLESS_PROBE_PATH,
) -> None:
    """
    Create a Databricks secrets scope, upload the connectivity test notebook,
    and install the TCP probe script to DBFS for use by ansible-deploy test.

    Args:
        workspace_host: Databricks workspace hostname (no https://)
        bolt_uri: Bolt URI for the Neo4j load balancer, e.g. bolt://<ip>:7687
        username: Neo4j username
        password: Neo4j password
        database: Neo4j database name
        scope_name: Databricks secrets scope name to create
        notebook_path: Workspace path for the connectivity test notebook
        client: Authenticated databricks.sdk.WorkspaceClient instance
        dbfs_probe_path: DBFS path for the classic probe script (default: shared path)
        serverless_probe_path: Workspace path for the serverless probe script (default: shared path)

    Raises:
        FileNotFoundError: If the connectivity test notebook is not found
        Exception: Propagates any Databricks SDK errors so the caller can handle them
    """
    from databricks.sdk.service.workspace import ImportFormat, Language

    lb_ip = bolt_uri.removeprefix("bolt://").split(":")[0]

    secrets = {
        "bolt_uri": bolt_uri,
        "host": lb_ip,
        "username": username,
        "password": password,
        "database": database,
    }

    # --- Create secrets scope ---
    console.print(f"\n[bold]Creating secrets scope:[/bold] {scope_name}")
    try:
        client.secrets.create_scope(scope=scope_name, initial_manage_principal="users")
        console.print("[green]✓ Scope created[/green]")
    except Exception as e:
        if "already exists" in str(e).lower() or "RESOURCE_ALREADY_EXISTS" in str(e):
            console.print("[dim]Scope already exists, continuing...[/dim]")
        else:
            raise

    # --- Upload secrets ---
    console.print("\n[bold]Uploading secrets:[/bold]")
    for key, value in secrets.items():
        client.secrets.put_secret(scope=scope_name, key=key, string_value=value)
        console.print(f"  [green]✓[/green] {key}")

    # --- Upload connectivity test notebook (manual use) ---
    notebook_local = NOTEBOOKS_DIR / "neo4j_connectivity_test.ipynb"
    if not notebook_local.exists():
        raise FileNotFoundError(
            f"Connectivity test notebook not found: {notebook_local}\n"
            f"Expected at: {NOTEBOOKS_DIR}"
        )

    console.print(f"\n[bold]Uploading connectivity notebook to:[/bold] {notebook_path}")
    notebook_content = notebook_local.read_text().replace("SCOPE_NAME_PLACEHOLDER", scope_name)
    encoded = base64.b64encode(notebook_content.encode()).decode()
    client.workspace.import_(
        path=notebook_path,
        format=ImportFormat.JUPYTER,
        content=encoded,
        overwrite=True,
    )
    console.print("[green]✓ Connectivity notebook uploaded[/green]")

    # --- Upload classic probe script to DBFS (used by ansible-deploy test --phase 2) ---
    if not _CLASSIC_PROBE_PATH.exists():
        raise FileNotFoundError(
            f"Classic probe script not found: {_CLASSIC_PROBE_PATH}\n"
            f"Expected at: {NOTEBOOKS_DIR}"
        )
    console.print(f"\n[bold]Uploading classic probe script to DBFS:[/bold] {dbfs_probe_path}")
    probe_encoded = base64.b64encode(_CLASSIC_PROBE_PATH.read_bytes()).decode()
    client.dbfs.put(path=dbfs_probe_path, contents=probe_encoded, overwrite=True)
    console.print("[green]✓ Classic probe script uploaded[/green]")

    # --- Upload serverless probe script to workspace (DBFS unavailable on serverless) ---
    serverless_probe_local = NOTEBOOKS_DIR / "neo4j_serverless_probe.py"
    if not serverless_probe_local.exists():
        raise FileNotFoundError(
            f"Serverless probe script not found: {serverless_probe_local}\n"
            f"Expected at: {NOTEBOOKS_DIR}"
        )

    console.print(f"\n[bold]Uploading serverless probe script to:[/bold] {serverless_probe_path}")
    from databricks.sdk.service.workspace import ImportFormat, Language
    # Delete any existing object first — overwrite=True fails when the existing
    # object type (e.g. NOTEBOOK) differs from the type being uploaded (FILE).
    try:
        client.workspace.delete(path=serverless_probe_path, recursive=False)
    except Exception:
        pass
    serverless_probe_encoded = base64.b64encode(serverless_probe_local.read_bytes()).decode()
    client.workspace.import_(
        path=serverless_probe_path,
        format=ImportFormat.SOURCE,
        language=Language.PYTHON,
        content=serverless_probe_encoded,
        overwrite=True,
    )
    console.print("[green]✓ Serverless probe script uploaded[/green]")

    # --- Upload serverless connectivity test notebook (manual use) ---
    if "-connectivity-test" in notebook_path:
        serverless_notebook_path = notebook_path.replace(
            "-connectivity-test", "-serverless-connectivity-test"
        )
    else:
        serverless_notebook_path = notebook_path + "-serverless-connectivity-test"

    serverless_notebook_local = NOTEBOOKS_DIR / "neo4j_serverless_connectivity_test.ipynb"
    if not serverless_notebook_local.exists():
        raise FileNotFoundError(
            f"Serverless connectivity test notebook not found: {serverless_notebook_local}\n"
            f"Expected at: {NOTEBOOKS_DIR}"
        )

    console.print(f"\n[bold]Uploading serverless connectivity notebook to:[/bold] {serverless_notebook_path}")
    serverless_notebook_content = serverless_notebook_local.read_text().replace(
        "SCOPE_NAME_PLACEHOLDER", scope_name
    )
    serverless_notebook_encoded = base64.b64encode(serverless_notebook_content.encode()).decode()
    client.workspace.import_(
        path=serverless_notebook_path,
        format=ImportFormat.JUPYTER,
        content=serverless_notebook_encoded,
        overwrite=True,
    )
    console.print("[green]✓ Serverless connectivity notebook uploaded[/green]")


# ---------------------------------------------------------------------------
# NCC (Network Connectivity Configuration) setup — account-scoped
# ---------------------------------------------------------------------------

def _approve_pending_connection(
    *,
    resource_group: str,
    pls_name: str,
    timeout_seconds: int = 600,
) -> None:
    """
    Poll the Private Link Service for a Pending endpoint connection, then approve it.

    Databricks provisions the private endpoint asynchronously after the PE rule is
    created — this function waits up to timeout_seconds for the Pending state to appear
    and then approves it via the Azure CLI session that is already authenticated.

    If a connection is already Approved (e.g. setup-ncc is re-run after a successful
    prior run), this function returns immediately without re-polling.
    """
    # Short-circuit: if a connection is already approved, nothing to do.
    check = subprocess.run(
        [
            "az", "network", "private-link-service", "show",
            "--resource-group", resource_group,
            "--name", pls_name,
            "--query",
            "privateEndpointConnections[?privateLinkServiceConnectionState.status=='Approved'].name",
            "--output", "json",
        ],
        capture_output=True, text=True, check=True,
    )
    already_approved: list[str] = _json.loads(check.stdout)
    if already_approved:
        console.print(f"[dim]PE connection already approved: {already_approved[0]}[/dim]")
        return

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = subprocess.run(
            [
                "az", "network", "private-link-service", "show",
                "--resource-group", resource_group,
                "--name", pls_name,
                "--query",
                "privateEndpointConnections[?privateLinkServiceConnectionState.status=='Pending'].name",
                "--output", "json",
            ],
            capture_output=True, text=True, check=True,
        )
        pending: list[str] = _json.loads(result.stdout)
        if pending:
            conn_name = pending[0]
            subprocess.run(
                [
                    "az", "network", "private-link-service", "connection", "update",
                    "--resource-group", resource_group,
                    "--service-name", pls_name,
                    "--name", conn_name,
                    "--connection-status", "Approved",
                ],
                check=True,
            )
            console.print(f"[green]✓ Approved PE connection: {conn_name}[/green]")
            return
        console.print("[dim]  Waiting for pending connection...[/dim]")
        time.sleep(15)
    raise TimeoutError(
        f"No pending PE connection appeared on '{pls_name}' within {timeout_seconds}s. "
        "Check the Private Link Service in the Azure portal and approve manually if needed."
    )


def setup_ncc(
    *,
    pls_resource_id: str,
    workspace_id: int,
    workspace_region: str,
    resource_group: str,
    pls_name: str,
    domain_names: list[str],
    token: str,
    ncc_name: str = "neo4j-ncc",
    account_host: str = "https://accounts.azuredatabricks.net",
    account_profile: Optional[str] = None,
) -> None:
    """
    Create or reuse a Databricks NCC, attach it to the workspace, create a PE rule
    pointing at the Private Link Service, and approve the resulting endpoint connection.

    Uses the same AAD token as the workspace client — no additional credentials needed.
    group_id is intentionally omitted: per Databricks API docs it is "not used by
    customer-managed private endpoint services".
    domain_names must contain the hostname(s) the Neo4j driver will use to connect.
    ncc_name controls which NCC is created/reused — use a scenario-scoped name when
    multiple deployments in the same region need isolated NCCs.
    account_profile: if set, creates AccountClient from this ~/.databrickscfg profile
    (reads host, account_id, and auth from the profile) instead of using token+host.
    """
    from databricks.sdk import AccountClient

    if account_profile:
        account_client = AccountClient(profile=account_profile)
    else:
        account_client = AccountClient(host=account_host, token=token)

    from databricks.sdk.service.settings import CreateNetworkConnectivityConfiguration, CreatePrivateEndpointRule
    from databricks.sdk.service.provisioning import Workspace

    # --- Create or reuse NCC ---
    nccs = list(account_client.network_connectivity.list_network_connectivity_configurations())
    ncc = next((n for n in nccs if n.name == ncc_name), None)
    if ncc is None:
        ncc = account_client.network_connectivity.create_network_connectivity_configuration(
            CreateNetworkConnectivityConfiguration(name=ncc_name, region=workspace_region),
        )
        console.print(f"[green]✓ Created NCC: {ncc_name} (region: {workspace_region})[/green]")
    else:
        console.print(f"[dim]Reusing existing NCC: {ncc_name}[/dim]")

    # --- Attach NCC to workspace ---
    account_client.workspaces.update(
        workspace_id=workspace_id,
        customer_facing_workspace=Workspace(
            network_connectivity_config_id=ncc.network_connectivity_config_id,
        ),
    )
    console.print("[green]✓ NCC attached to workspace[/green]")

    # --- Create PE rule (idempotent) ---
    # group_id omitted: "not used by customer-managed private endpoint services"
    # domain_names required: hostname(s) driver uses; Databricks creates DNS routing internally
    existing_rules = list(account_client.network_connectivity.list_private_endpoint_rules(
        network_connectivity_config_id=ncc.network_connectivity_config_id,
    ))
    pe_rule = next((r for r in existing_rules if r.resource_id == pls_resource_id), None)
    if pe_rule is None:
        account_client.network_connectivity.create_private_endpoint_rule(
            network_connectivity_config_id=ncc.network_connectivity_config_id,
            private_endpoint_rule=CreatePrivateEndpointRule(
                resource_id=pls_resource_id,
                domain_names=domain_names,
            ),
        )
        console.print(
            f"[green]✓ PE rule created[/green] [dim](domain_names={domain_names})[/dim]\n"
            "[dim]  Databricks is provisioning the private endpoint — this takes a few minutes...[/dim]"
        )
    else:
        connection_state = getattr(pe_rule, "connection_state", "unknown")
        console.print(
            f"[dim]PE rule already exists (connection_state: {connection_state}), continuing...[/dim]"
        )

    # --- Poll and approve ---
    _approve_pending_connection(resource_group=resource_group, pls_name=pls_name)
