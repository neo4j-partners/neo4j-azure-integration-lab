#!/usr/bin/env python3
"""
Neo4j Azure Deployment Tools (Ansible)

Entry point for deploying Neo4j Enterprise via Ansible playbooks.
"""

import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from typing_extensions import Annotated

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.config import ConfigManager
from src.password import PasswordManager
from src.setup import SetupWizard
from src.utils import find_deployment_file, run_command

DEPLOYMENTS_DIR = Path(__file__).parent.parent / ".deployments"
PLAYBOOKS_DIR = Path(__file__).parent.parent / "playbooks"
NOTEBOOKS_DIR = Path(__file__).parent.parent / "notebooks"
MANAGED_BY_TAG = "ansible-deploy"

app = typer.Typer(
    name="ansible-deploy",
    help="Neo4j Azure Deployment Tools (Ansible) - Deploy Neo4j Enterprise via Ansible playbooks",
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()


def check_initialized() -> ConfigManager:
    config_manager = ConfigManager()
    if not config_manager.is_initialized():
        console.print(
            "[yellow]Deployment tools not initialized. Running setup wizard...[/yellow]\n"
        )
        wizard = SetupWizard()
        success = wizard.run()
        if not success:
            console.print("[red]Setup failed or was cancelled.[/red]")
            raise typer.Exit(1)
    return config_manager


def _get_vmss_public_ip(resource_group: str) -> Optional[str]:
    """Query Azure for the first VMSS instance public IP in the resource group."""
    result = run_command(
        ["az", "vmss", "list", "-g", resource_group, "--query", "[0].name", "-o", "tsv"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None

    vmss_name = result.stdout.strip()

    result = run_command(
        [
            "az", "vmss", "list-instance-public-ips",
            "-g", resource_group,
            "-n", vmss_name,
            "--query", "[0].ipAddress",
            "-o", "tsv",
        ],
        check=False,
    )
    if result.returncode != 0:
        return None

    ip = result.stdout.strip()
    return ip if ip else None


def _get_neo4j_vnet_id(resource_group: str) -> Optional[str]:
    """Query Azure for the Neo4j VNet resource ID."""
    result = run_command(
        ["az", "network", "vnet", "list", "-g", resource_group, "--query", "[0].id", "-o", "tsv"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _get_neo4j_nsg_id(resource_group: str) -> Optional[str]:
    """Query Azure for the Neo4j NSG resource ID."""
    result = run_command(
        ["az", "network", "nsg", "list", "-g", resource_group, "--query", "[0].id", "-o", "tsv"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _get_databricks_vnet_id(resource_group: str) -> Optional[str]:
    """Query Azure for the Databricks VNet resource ID."""
    result = run_command(
        ["az", "network", "vnet", "list", "-g", resource_group, "--query", "[0].id", "-o", "tsv"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _get_lb_private_ip(resource_group: str) -> Optional[str]:
    """Query Azure for the internal load balancer's private frontend IP."""
    name_result = run_command(
        ["az", "network", "lb", "list", "-g", resource_group, "--query", "[0].name", "-o", "tsv"],
        check=False,
    )
    if name_result.returncode != 0 or not name_result.stdout.strip():
        return None
    lb_name = name_result.stdout.strip()

    ip_result = run_command(
        [
            "az", "network", "lb", "frontend-ip", "list",
            "-g", resource_group,
            "--lb-name", lb_name,
            "--query", "[0].privateIPAddress",
            "-o", "tsv",
        ],
        check=False,
    )
    if ip_result.returncode != 0:
        return None
    ip = ip_result.stdout.strip()
    return ip if ip else None


def _get_databricks_workspace_url(resource_group: str) -> Optional[str]:
    """Query Azure for the Databricks workspace URL."""
    name_result = run_command(
        [
            "az", "resource", "list",
            "-g", resource_group,
            "--resource-type", "Microsoft.Databricks/workspaces",
            "--query", "[0].name",
            "-o", "tsv",
        ],
        check=False,
    )
    if name_result.returncode != 0 or not name_result.stdout.strip():
        return None
    workspace_name = name_result.stdout.strip()

    url_result = run_command(
        [
            "az", "resource", "show",
            "-g", resource_group,
            "--resource-type", "Microsoft.Databricks/workspaces",
            "-n", workspace_name,
            "--query", "properties.workspaceUrl",
            "-o", "tsv",
        ],
        check=False,
    )
    if url_result.returncode != 0:
        return None
    url = url_result.stdout.strip()
    return url if url else None


def _delete_resource_group(name: str) -> bool:
    """Delete a resource group and wait for completion."""
    console.print(f"[yellow]Deleting {name}...[/yellow]")
    result = run_command(
        ["az", "group", "delete", "-n", name, "--yes"],
        check=False,
    )
    if result.returncode == 0:
        console.print(f"[green]✓ {name} deleted[/green]")
        return True
    elif "ResourceGroupNotFound" in result.stderr:
        console.print(f"[dim]{name} not found — already deleted[/dim]")
        return True
    else:
        console.print(f"[red]✗ Failed to delete {name}: {result.stderr}[/red]")
        return False


def _save_partial_state(
    scenario_name: str,
    neo4j_resource_group: str,
    databricks_resource_group: str,
    databricks_managed_resource_group: str,
) -> Path:
    """Save state after neo4j.yml succeeds so cleanup can find all RGs if databricks.yml fails."""
    DEPLOYMENTS_DIR.mkdir(exist_ok=True)
    state = {
        "scenario": scenario_name,
        "engine": "ansible",
        "state": "partial",
        "resource_group": neo4j_resource_group,
        "neo4j_resource_group": neo4j_resource_group,
        "databricks_resource_group": databricks_resource_group,
        "databricks_managed_resource_group": databricks_managed_resource_group,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    file_path = DEPLOYMENTS_DIR / f"{scenario_name}-ansible.json"
    with open(file_path, "w") as f:
        json.dump(state, f, indent=2)
    return file_path


def _save_deployment_details(
    scenario_name: str,
    resource_group: str,
    password: str,
    node_count: int,
    public_ip: Optional[str],
    license_type: str,
    lb_private_ip: Optional[str] = None,
    neo4j_vnet_id: Optional[str] = None,
    neo4j_nsg_id: Optional[str] = None,
    databricks_resource_group: Optional[str] = None,
    databricks_managed_resource_group: Optional[str] = None,
    databricks_workspace_url: Optional[str] = None,
    databricks_vnet_id: Optional[str] = None,
) -> Path:
    """Save deployment details to .deployments/{scenario}-ansible.json."""
    DEPLOYMENTS_DIR.mkdir(exist_ok=True)

    if public_ip:
        neo4j_uri = (
            f"bolt://{public_ip}:7687" if node_count == 1 else f"neo4j://{public_ip}:7687"
        )
        browser_url = f"http://{public_ip}:7474"
        ssh_command = f"ssh neo4j@{public_ip}"
    else:
        neo4j_uri = browser_url = ssh_command = "unavailable - query Azure portal for IP"

    connection: dict = {
        "neo4j_uri": neo4j_uri,
        "browser_url": browser_url,
        "username": "neo4j",
        "password": password,
        "neo4j_database": "neo4j",
        "lb_private_ip": lb_private_ip,
        "databricks_workspace_url": (
            f"https://{databricks_workspace_url}" if databricks_workspace_url else None
        ),
    }
    if lb_private_ip:
        connection["databricks_bolt_uri"] = f"bolt://{lb_private_ip}:7687"
    if databricks_workspace_url:
        connection["databricks_workspace_host"] = databricks_workspace_url

    network: dict = {
        "vnet_id": neo4j_vnet_id or "",
        "nsg_id": neo4j_nsg_id or "",
        "lb_private_ip": lb_private_ip or "",
    }
    if databricks_vnet_id:
        network["databricks_vnet_id"] = databricks_vnet_id

    details = {
        "scenario": scenario_name,
        "engine": "ansible",
        "state": "complete",
        "resource_group": resource_group,
        "neo4j_resource_group": resource_group,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "connection": connection,
        "ssh": {
            "hostname": public_ip,
            "username": "neo4j",
            "command": ssh_command,
        },
        "configuration": {
            "license_type": license_type,
            "node_count": node_count,
        },
        "network": network,
    }

    if databricks_resource_group:
        details["databricks_resource_group"] = databricks_resource_group
        details["databricks_managed_resource_group"] = databricks_managed_resource_group

    file_path = DEPLOYMENTS_DIR / f"{scenario_name}-ansible.json"
    with open(file_path, "w") as f:
        json.dump(details, f, indent=2)

    return file_path


def _display_connection_info(details: dict, scenario_name: str) -> None:
    """Display connection info panel."""
    conn = details.get("connection", {})
    ssh = details.get("ssh", {})
    cfg = details.get("configuration", {})
    is_partial = details.get("state") == "partial"

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Label", style="cyan")
    table.add_column("Value", style="white")

    if is_partial:
        table.add_row("State", "[yellow]partial — Databricks deployment did not complete[/yellow]")
        table.add_row("Neo4j RG", details.get("neo4j_resource_group", ""))
        table.add_row("Databricks RG", details.get("databricks_resource_group", ""))
    else:
        table.add_row("Browser URL", conn.get("browser_url", ""))
        table.add_row("Neo4j URI", conn.get("neo4j_uri", ""))
        table.add_row("Username", conn.get("username", "neo4j"))
        table.add_row("Password", conn.get("password", ""))

        if conn.get("lb_private_ip"):
            table.add_row("LB Private IP", conn["lb_private_ip"])
        if conn.get("databricks_workspace_url"):
            table.add_row("Databricks URL", conn["databricks_workspace_url"])
        if ssh.get("command"):
            table.add_row("SSH Command", ssh["command"])

        table.add_row("License", cfg.get("license_type", ""))
        if cfg.get("node_count", 1) > 1:
            table.add_row("Cluster Size", f"{cfg['node_count']} nodes")

    panel = Panel(
        table,
        title=f"[bold green]{scenario_name} - Connection Details[/bold green]",
        border_style="green",
    )
    console.print(panel)


@app.command()
def setup(
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Force re-running setup even if already configured"),
    ] = False,
) -> None:
    """
    Run the interactive setup wizard to configure the testing environment.

    This will guide you through configuring:
    - Azure subscription and region settings
    - Resource naming conventions
    - Cleanup behavior
    - Password management strategy
    """
    config_manager = ConfigManager()

    if config_manager.is_initialized() and not force:
        console.print("[yellow]Deployment tools are already configured.[/yellow]")
        if not typer.confirm("Re-run setup wizard?", default=False):
            console.print("[cyan]Setup cancelled.[/cyan]")
            raise typer.Exit(0)

    wizard = SetupWizard()
    success = wizard.run()

    if success:
        raise typer.Exit(0)
    else:
        console.print("[red]Setup failed or was cancelled.[/red]")
        raise typer.Exit(1)


@app.command()
def deploy(
    scenario: Annotated[
        str,
        typer.Option("--scenario", "-s", help="Scenario name (e.g., standalone-v2025, peer-databricks-v2025)"),
    ],
    location: Annotated[
        Optional[str],
        typer.Option("--location", "-l", help="Azure region (overrides settings default)"),
    ] = None,
    resource_group_prefix: Annotated[
        Optional[str],
        typer.Option("--resource-group", "-g", help="Prefix for the Neo4j resource group (overrides settings and scenario)"),
    ] = None,
) -> None:
    """
    Deploy Neo4j Enterprise via Ansible playbook.

    For Neo4j-only scenarios, runs playbooks/neo4j.yml.
    For scenarios with databricks: true, runs neo4j.yml
    then databricks.yml as two sequential steps. Partial state is saved after
    neo4j.yml so cleanup works even if databricks.yml fails.
    """
    config_manager = check_initialized()
    settings = config_manager.load_settings()

    # Resolve and validate scenario file
    scenario_file = PLAYBOOKS_DIR / "scenarios" / f"{scenario}.yml"
    if not scenario_file.exists():
        console.print(f"[red]Scenario not found: {scenario}[/red]")
        scenarios_dir = PLAYBOOKS_DIR / "scenarios"
        if scenarios_dir.exists():
            available = sorted(f.stem for f in scenarios_dir.glob("*.yml"))
            if available:
                console.print(f"[dim]Available: {', '.join(available)}[/dim]")
        raise typer.Exit(1)

    with open(scenario_file) as f:
        scenario_vars = yaml.safe_load(f)

    node_count = scenario_vars.get("node_count", 1)
    license_type = scenario_vars.get("license_type", "Enterprise")
    has_databricks = scenario_vars.get("databricks", False)

    # Resolve runtime values
    location = location or settings.default_region
    uuid_suffix = str(uuid.uuid4())[:8]

    # Neo4j RG prefix: CLI arg > scenario file > settings
    neo4j_prefix = resource_group_prefix or scenario_vars.get("resource_group_prefix") or settings.resource_group_prefix
    neo4j_rg = f"{neo4j_prefix}-ansible-{uuid_suffix}"

    password_manager = PasswordManager(settings)
    password = password_manager.get_password(scenario)

    # Build shared Ansible env (az CLI credentials, resolved once for both playbooks)
    env = os.environ.copy()
    env["ANSIBLE_AZURE_AUTH_SOURCE"] = "cli"
    if "AZURE_SUBSCRIPTION_ID" not in env:
        sub_result = run_command(
            ["az", "account", "show", "--query", "id", "-o", "tsv"],
            check=False,
        )
        if sub_result.returncode == 0 and sub_result.stdout.strip():
            env["AZURE_SUBSCRIPTION_ID"] = sub_result.stdout.strip()

    # --- Create Neo4j resource group ---
    console.print(f"\n[bold]Creating Neo4j resource group:[/bold] {neo4j_rg}")
    rg_result = run_command(
        [
            "az", "group", "create",
            "-n", neo4j_rg,
            "-l", location,
            "--tags",
            f"managed-by={MANAGED_BY_TAG}",
            f"scenario={scenario}",
            f"owner={settings.owner_email}",
        ],
        check=False,
    )
    if rg_result.returncode != 0:
        console.print(f"[red]Failed to create Neo4j resource group:\n{rg_result.stderr}[/red]")
        raise typer.Exit(1)
    console.print("[green]✓ Neo4j resource group created[/green]")

    # --- Create Databricks resource group (combined scenarios only) ---
    dbx_rg = None
    dbx_managed_rg = None
    if has_databricks:
        dbx_rg = f"{neo4j_prefix}-dbx-ansible-{uuid_suffix}"
        dbx_managed_rg = f"{dbx_rg}-managed"

        console.print(f"[bold]Creating Databricks resource group:[/bold] {dbx_rg}")
        dbx_rg_result = run_command(
            [
                "az", "group", "create",
                "-n", dbx_rg,
                "-l", location,
                "--tags",
                f"managed-by={MANAGED_BY_TAG}",
                f"scenario={scenario}",
                f"owner={settings.owner_email}",
            ],
            check=False,
        )
        if dbx_rg_result.returncode != 0:
            console.print(f"[red]Failed to create Databricks resource group:\n{dbx_rg_result.stderr}[/red]")
            raise typer.Exit(1)
        console.print("[green]✓ Databricks resource group created[/green]\n")

    # --- Run neo4j.yml ---
    extra_vars_neo4j = json.dumps({
        "resource_group": neo4j_rg,
        "location": location,
        "admin_password": password,
    })

    cmd_neo4j = [
        "ansible-playbook",
        str(PLAYBOOKS_DIR / "neo4j.yml"),
        "--extra-vars", f"@{scenario_file}",
        "--extra-vars", extra_vars_neo4j,
    ]

    console.print(f"[bold]Running Neo4j playbook:[/bold] {scenario} ({node_count} node(s))\n")
    result = subprocess.run(cmd_neo4j, env=env)

    if result.returncode != 0:
        console.print(f"\n[red]✗ Neo4j playbook failed (exit {result.returncode})[/red]")
        console.print(f"[dim]Resource group preserved for debugging: {neo4j_rg}[/dim]")
        if has_databricks:
            _save_partial_state(scenario, neo4j_rg, dbx_rg, dbx_managed_rg)
            console.print(f"[dim]Partial state saved. Run cleanup --scenario {scenario} to remove all resources.[/dim]")
        raise typer.Exit(1)

    console.print("\n[green]✓ Neo4j playbook completed[/green]\n")

    # --- Neo4j-only path: save and display ---
    if not has_databricks:
        console.print("[dim]Querying Azure for connection details...[/dim]")
        public_ip = _get_vmss_public_ip(neo4j_rg)
        if not public_ip:
            console.print(
                "[yellow]Warning: Could not retrieve public IP automatically. "
                "Check the Azure portal for connection details.[/yellow]"
            )
        neo4j_vnet_id = _get_neo4j_vnet_id(neo4j_rg)
        neo4j_nsg_id = _get_neo4j_nsg_id(neo4j_rg)
        lb_private_ip = _get_lb_private_ip(neo4j_rg) if node_count >= 3 else None
        details_file = _save_deployment_details(
            scenario_name=scenario,
            resource_group=neo4j_rg,
            password=password,
            node_count=node_count,
            public_ip=public_ip,
            license_type=license_type,
            lb_private_ip=lb_private_ip,
            neo4j_vnet_id=neo4j_vnet_id,
            neo4j_nsg_id=neo4j_nsg_id,
        )
        with open(details_file) as f:
            details = json.load(f)
        _display_connection_info(details, scenario)
        console.print(f"[dim]Saved to: {details_file}[/dim]\n")
        return

    # --- Save partial state before Databricks phase ---
    _save_partial_state(scenario, neo4j_rg, dbx_rg, dbx_managed_rg)
    console.print(f"[dim]Partial state saved to .deployments/{scenario}-ansible.json[/dim]\n")

    # --- Query Neo4j outputs needed by databricks.yml ---
    console.print("[dim]Querying Neo4j VNet details...[/dim]")
    neo4j_vnet_id = _get_neo4j_vnet_id(neo4j_rg)
    if not neo4j_vnet_id:
        console.print("[red]✗ Could not retrieve Neo4j VNet ID. Cannot proceed with Databricks deployment.[/red]")
        raise typer.Exit(1)

    # resource_suffix is deterministic: same SHA-1 logic used by the playbook
    resource_suffix = hashlib.sha1(neo4j_rg.encode()).hexdigest()[:13]
    neo4j_nsg_name = f"nsg-neo4j-{location}-{resource_suffix}"

    # --- Run databricks.yml ---
    extra_vars_dbx = json.dumps({
        "resource_group": neo4j_rg,
        "location": location,
        "databricks_resource_group": dbx_rg,
        "neo4j_vnet_id": neo4j_vnet_id,
        "neo4j_nsg_name": neo4j_nsg_name,
        "resource_suffix": resource_suffix,
    })

    cmd_dbx = [
        "ansible-playbook",
        str(PLAYBOOKS_DIR / "databricks.yml"),
        "--extra-vars", f"@{scenario_file}",
        "--extra-vars", extra_vars_dbx,
    ]

    console.print(f"[bold]Running Databricks playbook:[/bold] {scenario}\n")
    result = subprocess.run(cmd_dbx, env=env)

    if result.returncode != 0:
        console.print(f"\n[red]✗ Databricks playbook failed (exit {result.returncode})[/red]")
        console.print(f"[dim]Neo4j RG: {neo4j_rg}[/dim]")
        console.print(f"[dim]Databricks RG: {dbx_rg}[/dim]")
        console.print(f"[dim]Run cleanup --scenario {scenario} to remove all resources.[/dim]")
        raise typer.Exit(1)

    console.print("\n[green]✓ Databricks playbook completed[/green]\n")

    # --- Query final outputs and save complete state ---
    console.print("[dim]Querying Azure for connection details...[/dim]")
    public_ip = _get_vmss_public_ip(neo4j_rg)
    lb_private_ip = _get_lb_private_ip(neo4j_rg)
    workspace_url = _get_databricks_workspace_url(dbx_rg)
    neo4j_nsg_id = _get_neo4j_nsg_id(neo4j_rg)
    databricks_vnet_id = _get_databricks_vnet_id(dbx_rg)

    details_file = _save_deployment_details(
        scenario_name=scenario,
        resource_group=neo4j_rg,
        password=password,
        node_count=node_count,
        public_ip=public_ip,
        license_type=license_type,
        lb_private_ip=lb_private_ip,
        neo4j_vnet_id=neo4j_vnet_id,
        neo4j_nsg_id=neo4j_nsg_id,
        databricks_resource_group=dbx_rg,
        databricks_managed_resource_group=dbx_managed_rg,
        databricks_workspace_url=workspace_url,
        databricks_vnet_id=databricks_vnet_id,
    )

    with open(details_file) as f:
        details = json.load(f)

    _display_connection_info(details, scenario)
    console.print(f"[dim]Saved to: {details_file}[/dim]\n")


@app.command()
def status(
    scenario: Annotated[
        str,
        typer.Option("--scenario", "-s", help="Scenario name"),
    ],
) -> None:
    """Show connection details for a previously deployed scenario."""
    details_file = find_deployment_file(scenario, DEPLOYMENTS_DIR)

    if not details_file:
        console.print(f"[red]No deployment found for scenario: {scenario}[/red]")
        console.print(f"[dim]Run: ansible-deploy deploy --scenario {scenario}[/dim]")
        raise typer.Exit(1)

    with open(details_file) as f:
        details = json.load(f)

    if details.get("engine") != "ansible":
        console.print(
            "[yellow]Warning: This deployment was not created by ansible-deploy[/yellow]"
        )

    _display_connection_info(details, scenario)
    neo4j_rg = details.get("neo4j_resource_group") or details.get("resource_group", "unknown")
    console.print(f"[dim]Neo4j resource group: {neo4j_rg}[/dim]")
    if details.get("databricks_resource_group"):
        console.print(f"[dim]Databricks resource group: {details['databricks_resource_group']}[/dim]")
    console.print(f"[dim]Deployed at: {details.get('created_at', 'unknown')}[/dim]")


@app.command()
def cleanup(
    resource_group: Annotated[
        Optional[str],
        typer.Option("--resource-group", "-g", help="Neo4j resource group to delete"),
    ] = None,
    scenario: Annotated[
        Optional[str],
        typer.Option("--scenario", "-s", help="Resolve resource groups from scenario deployment file"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """
    Delete resource groups for an Ansible-managed deployment.

    For combined Neo4j + Databricks scenarios, deletes all three resource groups
    (Databricks managed, Databricks, Neo4j) in that order, waiting for each
    deletion to complete before starting the next.
    """
    databricks_resource_group = None
    databricks_managed_resource_group = None

    if scenario:
        details_file = find_deployment_file(scenario, DEPLOYMENTS_DIR)
        if not details_file:
            console.print(f"[red]No deployment found for scenario: {scenario}[/red]")
            raise typer.Exit(1)
        with open(details_file) as f:
            details = json.load(f)
        resource_group = details.get("neo4j_resource_group") or details.get("resource_group")
        databricks_resource_group = details.get("databricks_resource_group")
        databricks_managed_resource_group = details.get("databricks_managed_resource_group")

    if not resource_group:
        console.print("[red]Provide --resource-group or --scenario[/red]")
        raise typer.Exit(1)

    # Safety check: verify the Neo4j RG is managed by this tool
    tag_result = run_command(
        ["az", "group", "show", "-n", resource_group, "--query", "tags", "-o", "json"],
        check=False,
    )
    if tag_result.returncode == 0:
        try:
            tags = json.loads(tag_result.stdout)
            if tags.get("managed-by") != MANAGED_BY_TAG:
                console.print(
                    f"[red]{resource_group} was not created by ansible-deploy "
                    f"(managed-by={tags.get('managed-by', 'unset')}). Refusing to delete.[/red]"
                )
                if not force:
                    raise typer.Exit(1)
                console.print("[yellow]⚠ Proceeding anyway (--force)[/yellow]")
        except json.JSONDecodeError:
            pass

    # Confirm before deleting
    # Delete Databricks workspace RG first — Azure auto-deletes the managed RG when the
    # workspace is removed. Attempting managed RG second handles partial-state cases where
    # the workspace was never provisioned and the managed RG still exists independently.
    groups_to_delete = [
        g for g in [databricks_resource_group, databricks_managed_resource_group, resource_group]
        if g
    ]
    if not force:
        console.print("[bold]Resource groups to delete:[/bold]")
        for g in groups_to_delete:
            console.print(f"  {g}")
        if not typer.confirm("Proceed?", default=False):
            console.print("[cyan]Cleanup cancelled.[/cyan]")
            raise typer.Exit(0)

    # Delete in order: Databricks managed → Databricks → Neo4j
    all_succeeded = True
    for group in groups_to_delete:
        if not _delete_resource_group(group):
            all_succeeded = False
            console.print("[yellow]Continuing with remaining deletions...[/yellow]")

    if all_succeeded:
        console.print("\n[green]✓ All resource groups deleted[/green]")
    else:
        console.print("\n[yellow]⚠ Some deletions failed. Check the Azure portal.[/yellow]")
        raise typer.Exit(1)


@app.command("setup-databricks")
def setup_databricks(
    scenario: Annotated[
        str,
        typer.Option("--scenario", "-s", help="Scenario name (must have a complete deployment JSON)"),
    ],
    token: Annotated[
        Optional[str],
        typer.Option("--token", "-t", help="Databricks personal access token (PAT)"),
    ] = None,
    profile: Annotated[
        Optional[str],
        typer.Option("--profile", "-p", help="Databricks CLI profile from ~/.databrickscfg"),
    ] = None,
    notebook_path: Annotated[
        Optional[str],
        typer.Option("--notebook-path", help="Workspace path to upload the notebook (default: /Shared/neo4j-{scenario}-connectivity-test)"),
    ] = None,
) -> None:
    """
    Create Databricks secrets and upload the connectivity test notebook for a Neo4j deployment.

    Reads .deployments/{scenario}-{engine}.json and:
    - Creates a secrets scope named neo4j-{scenario}
    - Uploads 5 secrets: bolt_uri, host, username, password, database
    - Uploads the connectivity test notebook to the Databricks workspace

    Auth options (one required):
      --token <pat>     Personal access token — generate in Databricks UI under User Settings > Developer > Access tokens
      --profile <name>  Named profile from ~/.databrickscfg
    """
    details_file = find_deployment_file(scenario, DEPLOYMENTS_DIR)
    if not details_file:
        console.print(f"[red]No deployment found for scenario: {scenario}[/red]")
        console.print(f"[dim]Run: ansible-deploy deploy --scenario {scenario}[/dim]")
        raise typer.Exit(1)

    with open(details_file) as f:
        details = json.load(f)

    if details.get("state") != "complete":
        console.print("[red]Deployment is not in a complete state. Re-deploy or check the JSON.[/red]")
        raise typer.Exit(1)

    conn = details.get("connection", {})
    bolt_uri = conn.get("databricks_bolt_uri")
    workspace_host = conn.get("databricks_workspace_host")

    if not bolt_uri:
        console.print(
            "[red]No databricks_bolt_uri in deployment JSON.[/red]\n"
            "[dim]This field is only present for peered-cluster scenarios with a load balancer.\n"
            "Re-deploy to regenerate the JSON with the new fields.[/dim]"
        )
        raise typer.Exit(1)

    if not workspace_host:
        console.print("[red]No databricks_workspace_host in deployment JSON.[/red]")
        raise typer.Exit(1)

    if not token and not profile:
        console.print(
            "[red]Provide --token or --profile for Databricks authentication.[/red]\n"
            "[dim]Generate a PAT: Databricks workspace → User Settings → Developer → Access tokens[/dim]"
        )
        raise typer.Exit(1)

    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        console.print("[red]databricks-sdk not installed. Run: uv sync[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold]Connecting to:[/bold] https://{workspace_host}")
    if token:
        client = WorkspaceClient(host=workspace_host, token=token)
    else:
        client = WorkspaceClient(profile=profile)

    scope_name = f"neo4j-{scenario}"
    if notebook_path is None:
        notebook_path = f"/Shared/neo4j-{scenario}-connectivity-test"

    try:
        from src.databricks_setup import run_databricks_setup
        run_databricks_setup(
            workspace_host=workspace_host,
            bolt_uri=bolt_uri,
            username=conn.get("username", "neo4j"),
            password=conn.get("password", ""),
            database=conn.get("neo4j_database", "neo4j"),
            scope_name=scope_name,
            notebook_path=notebook_path,
            client=client,
        )
    except Exception as e:
        console.print(f"[red]Setup failed: {e}[/red]")
        raise typer.Exit(1)

    workspace_url = conn.get("databricks_workspace_url", f"https://{workspace_host}")
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Label", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("Secrets scope", scope_name)
    table.add_row("Notebook path", notebook_path)
    table.add_row("Workspace", workspace_url)
    console.print(
        Panel(table, title="[bold green]Databricks Setup Complete[/bold green]", border_style="green")
    )



def main() -> None:
    app()


if __name__ == "__main__":
    main()
