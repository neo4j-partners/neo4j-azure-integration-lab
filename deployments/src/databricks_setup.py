"""
Shared Databricks setup: secrets scope creation and notebook/script upload.

Used by both the Ansible CLI (ansible_deploy.py) and the Bicep CLI (bicep_deploy.py).
Each CLI handles its own authentication and reads the deployment JSON — this module
only handles the Databricks API calls that are identical between the two paths.
"""

import base64
from pathlib import Path

from rich.console import Console

console = Console()

# Notebooks live at the repo root, one level above deployments/
NOTEBOOKS_DIR = Path(__file__).parent.parent.parent / "notebooks"

# Permanent DBFS path for the TCP probe script — reused by ansible-deploy test
DBFS_PROBE_PATH = "dbfs:/neo4j/neo4j_tcp_probe.py"

# Plain Python TCP probe — no Spark, no dbutils, runs as SparkPythonTask
# Takes lb_ip as sys.argv[1]. Outputs PASS:PORT or FAIL:PORT:msg lines.
_TCP_PROBE_SCRIPT = """\
import socket
import sys

lb_ip = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
for port in [7687, 7474]:
    try:
        s = socket.create_connection((lb_ip, port), timeout=10)
        s.close()
        print(f"PASS:{port}")
    except Exception as e:
        print(f"FAIL:{port}:{e}")
"""


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

    Raises:
        FileNotFoundError: If the connectivity test notebook is not found
        Exception: Propagates any Databricks SDK errors so the caller can handle them
    """
    from databricks.sdk.service.workspace import ImportFormat

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

    # --- Upload TCP probe script to DBFS (used by ansible-deploy test --phase 2) ---
    console.print(f"\n[bold]Uploading TCP probe script to DBFS:[/bold] {DBFS_PROBE_PATH}")
    probe_encoded = base64.b64encode(_TCP_PROBE_SCRIPT.encode()).decode()
    client.dbfs.put(path=DBFS_PROBE_PATH, contents=probe_encoded, overwrite=True)
    console.print("[green]✓ TCP probe script uploaded[/green]")
