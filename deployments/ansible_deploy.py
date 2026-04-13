#!/usr/bin/env python3
"""
Neo4j Azure Deployment Tools (Ansible)

Entry point for deploying Neo4j Enterprise via Ansible playbooks.
"""

import hashlib
import json
import os
import subprocess
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

from src.config import ConfigManager
from src.constants import NEO4J_BOLT_PORT, NEO4J_HTTP_PORT, RESOURCE_SUFFIX_LENGTH
from src.deployment_output import (
    ConfigurationJSON,
    ConnectionJSON,
    DeploymentJSON,
    NetworkJSON,
    SSHJSON,
    display_connection_info,
    write_deployment_json,
)
from src.password import PasswordManager
from src.setup import SetupWizard
from src.models import Engine
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


def _get_neo4j_pls_resource_id(resource_group: str) -> Optional[str]:
    """Query Azure for the Private Link Service resource ID in the resource group."""
    result = run_command(
        [
            "az", "network", "private-link-service", "list",
            "-g", resource_group, "--query", "[0].id", "-o", "tsv",
        ],
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


def _delete_pls_connections(resource_group: str, pls_name: str = "pls-neo4j") -> None:
    """Delete all private endpoint connections on the PLS before RG deletion.

    When the Databricks RG is deleted, Azure removes the PE from the Databricks side but
    leaves the connection record on the PLS in the Neo4j RG. That stale record blocks
    deletion of the PLS (and by extension the entire resource group).
    """
    result = run_command(
        [
            "az", "network", "private-link-service", "show",
            "--resource-group", resource_group,
            "--name", pls_name,
            "--query", "privateEndpointConnections[].name",
            "--output", "tsv",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return
    for conn_name in result.stdout.strip().splitlines():
        conn_name = conn_name.strip()
        if not conn_name:
            continue
        console.print(f"[dim]Removing PLS connection {conn_name} from {pls_name}...[/dim]")
        run_command(
            [
                "az", "network", "private-link-service", "connection", "delete",
                "--resource-group", resource_group,
                "--service-name", pls_name,
                "--name", conn_name,
                "--yes",
            ],
            check=False,
        )


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
    data = DeploymentJSON(
        scenario=scenario_name,
        engine="ansible",
        state="partial",
        resource_group=neo4j_resource_group,
        neo4j_resource_group=neo4j_resource_group,
        databricks_resource_group=databricks_resource_group,
        databricks_managed_resource_group=databricks_managed_resource_group,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    file_path = DEPLOYMENTS_DIR / f"{scenario_name}-ansible.json"
    write_deployment_json(data, file_path)
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
    pls_resource_id: Optional[str] = None,
    databricks_resource_group: Optional[str] = None,
    databricks_managed_resource_group: Optional[str] = None,
    databricks_workspace_url: Optional[str] = None,
    databricks_vnet_id: Optional[str] = None,
) -> Path:
    """Save deployment details to .deployments/{scenario}-ansible.json."""
    if public_ip:
        neo4j_uri = (
            f"bolt://{public_ip}:{NEO4J_BOLT_PORT}" if node_count == 1
            else f"neo4j://{public_ip}:{NEO4J_BOLT_PORT}"
        )
        browser_url = f"http://{public_ip}:{NEO4J_HTTP_PORT}"
        ssh_command = f"ssh neo4j@{public_ip}"
    else:
        neo4j_uri = browser_url = ssh_command = "unavailable - query Azure portal for IP"
        public_ip = None

    data = DeploymentJSON(
        scenario=scenario_name,
        engine="ansible",
        state="complete",
        resource_group=resource_group,
        neo4j_resource_group=resource_group,
        databricks_resource_group=databricks_resource_group,
        databricks_managed_resource_group=databricks_managed_resource_group,
        created_at=datetime.now(timezone.utc).isoformat(),
        connection=ConnectionJSON(
            neo4j_uri=neo4j_uri,
            browser_url=browser_url,
            username="neo4j",
            password=password,
            neo4j_database="neo4j",
            lb_private_ip=lb_private_ip,
            databricks_bolt_uri=f"bolt://{lb_private_ip}:{NEO4J_BOLT_PORT}" if lb_private_ip else None,
            databricks_workspace_url=f"https://{databricks_workspace_url}" if databricks_workspace_url else None,
            databricks_workspace_host=databricks_workspace_url,
        ),
        ssh=SSHJSON(
            hostname=public_ip,
            username="neo4j",
            command=ssh_command,
        ),
        configuration=ConfigurationJSON(
            license_type=license_type,
            node_count=node_count,
        ),
        network=NetworkJSON(
            vnet_id=neo4j_vnet_id or "",
            nsg_id=neo4j_nsg_id or "",
            lb_private_ip=lb_private_ip or "",
            private_link_service_id=pls_resource_id or "",
            databricks_vnet_id=databricks_vnet_id,
        ),
    )

    file_path = DEPLOYMENTS_DIR / f"{scenario_name}-ansible.json"
    write_deployment_json(data, file_path)
    return file_path


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
        pls_resource_id = _get_neo4j_pls_resource_id(neo4j_rg) if node_count >= 3 else None
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
            pls_resource_id=pls_resource_id,
        )
        with open(details_file) as f:
            details = json.load(f)
        display_connection_info(details, scenario)
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
    resource_suffix = hashlib.sha1(neo4j_rg.encode()).hexdigest()[:RESOURCE_SUFFIX_LENGTH]
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
    pls_resource_id = _get_neo4j_pls_resource_id(neo4j_rg)
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
        pls_resource_id=pls_resource_id,
        databricks_resource_group=dbx_rg,
        databricks_managed_resource_group=dbx_managed_rg,
        databricks_workspace_url=workspace_url,
        databricks_vnet_id=databricks_vnet_id,
    )

    with open(details_file) as f:
        details = json.load(f)

    display_connection_info(details, scenario)
    console.print(f"[dim]Saved to: {details_file}[/dim]\n")


@app.command()
def status(
    scenario: Annotated[
        str,
        typer.Option("--scenario", "-s", help="Scenario name"),
    ],
) -> None:
    """Show connection details for a previously deployed scenario."""
    details_file = find_deployment_file(scenario, DEPLOYMENTS_DIR, Engine.ansible)

    if not details_file:
        console.print(f"[red]No ansible deployment found for scenario: {scenario}[/red]")
        console.print(f"[dim]Run: ansible-deploy deploy --scenario {scenario}[/dim]")
        raise typer.Exit(1)

    with open(details_file) as f:
        details = json.load(f)

    display_connection_info(details, scenario)
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
        details_file = find_deployment_file(scenario, DEPLOYMENTS_DIR, Engine.ansible)
        if not details_file:
            console.print(f"[red]No ansible deployment found for scenario: {scenario}[/red]")
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
        # Clear any stale PLS private endpoint connections before deleting the Neo4j RG.
        # Deleting the Databricks RG removes the PE from the Databricks side but leaves the
        # connection record on pls-neo4j, which blocks deletion of the Neo4j resource group.
        if group == resource_group:
            _delete_pls_connections(group)
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

    Auth options (in order of precedence):
      --token <pat>     Personal access token — generate in Databricks UI under User Settings > Developer > Access tokens
      --profile <name>  Named profile from ~/.databrickscfg
      (none)            Falls back to AAD token from active az login session
    """
    details_file = find_deployment_file(scenario, DEPLOYMENTS_DIR, Engine.ansible)
    if not details_file:
        console.print(f"[red]No ansible deployment found for scenario: {scenario}[/red]")
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

    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        console.print("[red]databricks-sdk not installed. Run: uv sync[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold]Connecting to:[/bold] https://{workspace_host}")
    if token:
        client = WorkspaceClient(host=workspace_host, token=token)
    elif profile:
        client = WorkspaceClient(profile=profile)
    else:
        # Fall back to AAD token from active az login session — same as DatabricksCheckerBase
        import subprocess as _sp
        _result = _sp.run(
            ["az", "account", "get-access-token",
             "--resource", "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d",
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, check=False,
        )
        aad_token = _result.stdout.strip()
        if not aad_token:
            console.print(
                "[red]No --token or --profile provided and AAD token acquisition failed.[/red]\n"
                "[dim]Run 'az login' or pass --token / --profile explicitly.[/dim]"
            )
            raise typer.Exit(1)
        client = WorkspaceClient(host=workspace_host, token=aad_token)

    scope_name = f"neo4j-ansible-{scenario}"
    if notebook_path is None:
        notebook_path = f"/Shared/neo4j-ansible-{scenario}-connectivity-test"

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
            dbfs_probe_path=f"dbfs:/neo4j-ansible/{scenario}/neo4j_classic_probe.py",
            serverless_probe_path=f"/Shared/neo4j-ansible-{scenario}-serverless-probe.py",
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


# Fixed, globally-registered Azure AD application ID for the Databricks platform service.
# Identical across all tenants — used to acquire an AAD token for the Databricks Account API.
_DATABRICKS_PLATFORM_APP_ID = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"


@app.command("setup-ncc")
def setup_ncc_cmd(
    scenario: Annotated[
        str,
        typer.Option("--scenario", "-s", help="Scenario name (must have a complete deployment JSON)"),
    ],
    domain_name: Annotated[
        str,
        typer.Option(
            "--domain-name",
            help="Hostname to use in the NCC PE rule domain_names list. "
                 "The serverless Neo4j driver must connect using this hostname.",
        ),
    ] = "neo4j.private",
    pls_name: Annotated[
        str,
        typer.Option("--pls-name", help="Private Link Service resource name (default: pls-neo4j)"),
    ] = "pls-neo4j",
    ncc_name: Annotated[
        str,
        typer.Option(
            "--ncc-name",
            help="Databricks NCC name. Defaults to 'neo4j-ncc-ansible-<scenario>'. "
                 "NCCs are account-level objects shared across workspaces in the same region — "
                 "use a scenario-scoped name when multiple deployments need isolated NCCs.",
        ),
    ] = "",
    account_profile: Annotated[
        Optional[str],
        typer.Option(
            "--account-profile",
            help="~/.databrickscfg profile for the Databricks Account API (must have host + account_id). "
                 "Example: azure-account-admin. If omitted, falls back to AAD token from az login.",
        ),
    ] = None,
) -> None:
    """
    Create a Databricks NCC, attach it to the workspace, and establish a private endpoint
    to the Neo4j Private Link Service for serverless compute connectivity.

    Reads .deployments/{scenario}-ansible.json and:
    - Creates or reuses a Databricks NCC in the workspace region
    - Attaches the NCC to the workspace
    - Creates a PE rule with domain_names=[--domain-name] pointing at the PLS
    - Polls for the Pending endpoint connection and approves it via Azure CLI

    Auth: pass --account-profile <name> for a ~/.databrickscfg profile that has
    host + account_id (e.g. azure-account-admin). Falls back to AAD token from az login.

    After setup-ncc completes, use 'bolt://<domain-name>:7687' in serverless notebooks.

    Note: requires Databricks Account Admin in the account console
    (https://accounts.azuredatabricks.net → Settings → Admins).
    """
    import subprocess as _sp

    details_file = find_deployment_file(scenario, DEPLOYMENTS_DIR, Engine.ansible)
    if not details_file:
        console.print(f"[red]No ansible deployment found for scenario: {scenario}[/red]")
        console.print(f"[dim]Run: ansible-deploy deploy --scenario {scenario}[/dim]")
        raise typer.Exit(1)

    with open(details_file) as f:
        details = json.load(f)

    if details.get("state") != "complete":
        console.print("[red]Deployment is not in a complete state.[/red]")
        raise typer.Exit(1)

    neo4j_rg: str = details.get("neo4j_resource_group", "")
    databricks_rg: str = details.get("databricks_resource_group", "")

    if not neo4j_rg or not databricks_rg:
        console.print("[red]Missing neo4j_resource_group or databricks_resource_group in deployment JSON.[/red]")
        raise typer.Exit(1)

    # Use saved PLS resource ID if present; otherwise construct deterministically.
    saved_pls_id: str = (details.get("network") or {}).get("private_link_service_id", "")

    try:
        sub_result = _sp.run(
            ["az", "account", "show", "--query", "id", "--output", "tsv"],
            capture_output=True, text=True, check=True,
        )
        subscription_id = sub_result.stdout.strip()
    except _sp.CalledProcessError as e:
        console.print(f"[red]Failed to get Azure subscription ID: {e.stderr}[/red]")
        raise typer.Exit(1)

    pls_resource_id = saved_pls_id or (
        f"/subscriptions/{subscription_id}"
        f"/resourceGroups/{neo4j_rg}"
        f"/providers/Microsoft.Network/privateLinkServices/{pls_name}"
    )

    try:
        region_result = _sp.run(
            ["az", "group", "show", "--name", databricks_rg, "--query", "location", "--output", "tsv"],
            capture_output=True, text=True, check=True,
        )
        workspace_region = region_result.stdout.strip()
    except _sp.CalledProcessError as e:
        console.print(f"[red]Failed to get workspace region from '{databricks_rg}': {e.stderr}[/red]")
        raise typer.Exit(1)

    try:
        ws_id_result = _sp.run(
            [
                "az", "databricks", "workspace", "list",
                "--resource-group", databricks_rg,
                "--query", "[0].workspaceId",
                "--output", "tsv",
            ],
            capture_output=True, text=True, check=True,
        )
        workspace_id = int(ws_id_result.stdout.strip())
    except (_sp.CalledProcessError, ValueError) as e:
        console.print(f"[red]Failed to get Databricks workspace ID from '{databricks_rg}': {e}[/red]")
        raise typer.Exit(1)

    try:
        token_result = _sp.run(
            [
                "az", "account", "get-access-token",
                "--resource", _DATABRICKS_PLATFORM_APP_ID,
                "--query", "accessToken",
                "--output", "tsv",
            ],
            capture_output=True, text=True, check=True,
        )
        token = token_result.stdout.strip()
    except _sp.CalledProcessError as e:
        console.print(f"[red]Failed to get Databricks AAD token: {e.stderr}[/red]")
        raise typer.Exit(1)

    resolved_ncc_name = ncc_name or f"neo4j-ansi-{hashlib.sha1(scenario.encode()).hexdigest()[:8]}"

    console.print(f"\n[bold]NCC Setup[/bold]")
    console.print(f"  PLS resource ID:  [dim]{pls_resource_id}[/dim]")
    console.print(f"  Workspace region: [dim]{workspace_region}[/dim]")
    console.print(f"  Workspace ID:     [dim]{workspace_id}[/dim]")
    console.print(f"  NCC name:         [dim]{resolved_ncc_name}[/dim]")
    console.print(f"  domain_names:     [dim]{[domain_name]}[/dim]")

    try:
        from src.databricks_setup import setup_ncc
        setup_ncc(
            pls_resource_id=pls_resource_id,
            workspace_id=workspace_id,
            workspace_region=workspace_region,
            resource_group=neo4j_rg,
            pls_name=pls_name,
            domain_names=[domain_name],
            token=token,
            ncc_name=resolved_ncc_name,
            account_profile=account_profile,
        )
    except TimeoutError as e:
        console.print(f"[yellow]⚠ {e}[/yellow]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]NCC setup failed: {e}[/red]")
        raise typer.Exit(1)

    # Persist serverless block to deployment JSON (best-effort).
    try:
        with open(details_file) as _f:
            _data = json.load(_f)
        _data["serverless"] = {
            "ncc_configured": True,
            "domain_name": domain_name,
            "bolt_uri": f"neo4j://{domain_name}:{NEO4J_BOLT_PORT}",
            "pls_name": pls_name,
        }
        with open(details_file, "w") as _f:
            json.dump(_data, _f, indent=2)
    except Exception as _e:
        console.print(f"[yellow]Warning: could not update deployment JSON: {_e}[/yellow]")

    bolt_uri = f"neo4j://{domain_name}:{NEO4J_BOLT_PORT}"
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Label", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("NCC name", resolved_ncc_name)
    table.add_row("PLS name", pls_name)
    table.add_row("domain_name", domain_name)
    table.add_row("Serverless bolt URI", bolt_uri)
    table.add_row("", "")
    table.add_row("Next step", "Wait 10 minutes for NCC attachment to propagate, then:")
    table.add_row("", f'neo4j-connect check --scenario {scenario} --compute serverless --engine ansible')
    console.print(
        Panel(table, title="[bold green]NCC Setup Complete[/bold green]", border_style="green")
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
