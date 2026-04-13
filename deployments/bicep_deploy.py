#!/usr/bin/env python3
"""
Neo4j Azure Deployment Tools

Main entry point for the deployment and testing framework.
"""

import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from typing_extensions import Annotated

from src.config import ConfigManager
from src.constants import NEO4J_BOLT_PORT, DATABRICKS_PLATFORM_APP_ID
from src.deployment_output import (
    ConfigurationJSON,
    ConnectionJSON,
    DeploymentJSON,
    NetworkJSON,
    SSHJSON,
    display_connection_info,
    write_deployment_json,
)
from src.models import CleanupMode, DeploymentState, Engine
from src.setup import SetupWizard
from src.utils import find_deployment_file

# Project root directories
DEPLOYMENTS_DIR = Path(__file__).parent.parent / ".deployments"
NOTEBOOKS_DIR = Path(__file__).parent.parent / "notebooks"


def _build_m2m_auth(settings) -> dict:
    """Build the m2m_auth section for the deployment JSON."""
    if not (settings and settings.m2m and settings.m2m.enabled):
        return {"enabled": False}
    m2m = settings.m2m
    if m2m.provider_type == "keycloak":
        return {
            "enabled": True,
            "provider_type": "keycloak",
            "discovery_uri": m2m.discovery_uri,
            "token_endpoint": m2m.token_endpoint,
            "audience": m2m.audience,
            "client_id": m2m.client_id,
            "client_secret": m2m.client_secret,
            "role_mapping": m2m.role_mapping,
            "display_name": m2m.display_name,
        }
    return {
        "enabled": True,
        "provider_type": "entra",
        "tenant_id": m2m.tenant_id,
        "api_app_id": m2m.api_app_id,
        "audience": m2m.audience,
        "client_app_id": m2m.client_app_id,
        "token_endpoint": f"https://login.microsoftonline.com/{m2m.tenant_id}/oauth2/v2.0/token",
        "scope": f"{m2m.audience}/.default",
        "well_known_uri": f"https://login.microsoftonline.com/{m2m.tenant_id}/v2.0/.well-known/openid-configuration",
    }


def _build_databricks_deployment_json(conn_info, outputs: dict, source_scenario: Optional[str]) -> DeploymentJSON:
    """Build DeploymentJSON for Databricks peering scenarios."""
    workspace_url = outputs.get("databricksWorkspaceUrl", {}).get("value", "")
    databricks_vnet_id = outputs.get("databricksVnetId", {}).get("value", "")

    source_conn: dict = {}
    source_net: dict = {}
    source_ssh: dict = {}
    source_cfg: dict = {}
    neo4j_rg = conn_info.resource_group

    if source_scenario:
        source_file = find_deployment_file(source_scenario, DEPLOYMENTS_DIR, Engine.bicep)
        if source_file:
            with open(source_file) as f:
                source_data = json.load(f)
            source_conn = source_data.get("connection", {})
            source_net = source_data.get("network", {})
            source_ssh = source_data.get("ssh", {})
            source_cfg = source_data.get("configuration", {})
            neo4j_rg = source_data.get("resource_group", conn_info.resource_group)

    lb_ip = source_net.get("lb_private_ip") or source_conn.get("lb_private_ip") or ""
    databricks_rg = f"{neo4j_rg}-dbx"

    return DeploymentJSON(
        scenario=conn_info.scenario_name,
        engine="bicep",
        state="complete",
        deployment_id=conn_info.deployment_id,
        resource_group=neo4j_rg,
        neo4j_resource_group=neo4j_rg,
        databricks_resource_group=databricks_rg,
        databricks_managed_resource_group=f"{databricks_rg}-managed",
        created_at=conn_info.created_at.isoformat(),
        connection=ConnectionJSON(
            neo4j_uri=source_conn.get("neo4j_uri", ""),
            browser_url=source_conn.get("browser_url", ""),
            username=source_conn.get("username", "neo4j"),
            password=source_conn.get("password", ""),
            neo4j_database="neo4j",
            lb_private_ip=lb_ip or None,
            databricks_bolt_uri=f"bolt://{lb_ip}:{NEO4J_BOLT_PORT}" if lb_ip else None,
            databricks_workspace_url=f"https://{workspace_url}" if workspace_url else None,
            databricks_workspace_host=workspace_url or None,
        ),
        ssh=SSHJSON(
            hostname=source_ssh.get("hostname") or None,
            username="neo4j",
            command=source_ssh.get("command") or None,
        ),
        configuration=ConfigurationJSON(
            license_type=source_cfg.get("license_type", "Enterprise"),
            node_count=source_cfg.get("node_count", 3),
        ),
        network=NetworkJSON(
            vnet_id=source_net.get("vnet_id", ""),
            nsg_id=source_net.get("nsg_id", ""),
            lb_private_ip=lb_ip,
            private_link_service_id=source_net.get("private_link_service_id", ""),
            databricks_vnet_id=databricks_vnet_id or None,
        ),
    )


def _build_neo4j_deployment_json(conn_info, outputs: dict, settings) -> DeploymentJSON:
    """Build DeploymentJSON for Neo4j-only scenarios."""
    lb_ip = outputs.get("lbPrivateIpAddress", {}).get("value", "")
    return DeploymentJSON(
        scenario=conn_info.scenario_name,
        engine="bicep",
        state="complete",
        deployment_id=conn_info.deployment_id,
        resource_group=conn_info.resource_group,
        neo4j_resource_group=conn_info.resource_group,
        created_at=conn_info.created_at.isoformat(),
        connection=ConnectionJSON(
            neo4j_uri=conn_info.neo4j_uri,
            browser_url=conn_info.browser_url,
            username=conn_info.username,
            password=conn_info.password,
            neo4j_database="neo4j",
            lb_private_ip=lb_ip or None,
            bloom_url=conn_info.bloom_url,
        ),
        ssh=SSHJSON(
            hostname=conn_info.ssh_hostname,
            username=conn_info.ssh_username or "neo4j",
            command=conn_info.ssh_command,
        ),
        configuration=ConfigurationJSON(
            license_type=conn_info.license_type,
            node_count=conn_info.node_count or 1,
        ),
        network=NetworkJSON(
            vnet_id=outputs.get("vnetId", {}).get("value", ""),
            nsg_id=outputs.get("nsgId", {}).get("value", ""),
            lb_private_ip=lb_ip,
            private_link_service_id=outputs.get("privateLinkServiceId", {}).get("value", ""),
        ),
        m2m_auth=_build_m2m_auth(settings),
    )


def save_deployment_details(
    conn_info,
    deployment_state,
    settings=None,
    source_scenario: Optional[str] = None,
) -> Path:
    """
    Save deployment details to .deployments/{scenario}-bicep.json.

    For Databricks peering scenarios the saved JSON is a complete merged record
    combining Neo4j connection details from the source scenario with the
    Databricks workspace URL from this deployment.

    For Neo4j-only scenarios the schema aligns with the Ansible output so that
    callers (e.g. setup-databricks) work identically for both engines.
    """
    outputs = conn_info.outputs or {}

    if outputs.get("databricksWorkspaceUrl"):
        data = _build_databricks_deployment_json(conn_info, outputs, source_scenario)
    else:
        data = _build_neo4j_deployment_json(conn_info, outputs, settings)

    file_path = DEPLOYMENTS_DIR / f"{conn_info.scenario_name}-bicep.json"
    write_deployment_json(data, file_path)
    return file_path


# Create Typer app
app = typer.Typer(
    name="bicep-deploy",
    help="Neo4j Azure Deployment Tools - Automated deployment and testing framework for Neo4j Enterprise on Azure",
    add_completion=False,
    rich_markup_mode="rich",
)

console = Console()


def check_initialized() -> ConfigManager:
    """
    Check if the deployment tools are initialized.

    Returns:
        ConfigManager instance

    Raises:
        typer.Exit: If not initialized and user declines setup
    """
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


@app.command()
def setup(
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Force re-running setup even if already configured")
    ] = False,
) -> None:
    """
    Run the interactive setup wizard to configure the testing environment.

    This will guide you through configuring:
    - Azure subscription and region settings
    - Resource naming conventions
    - Cleanup behavior and cost limits
    - Test scenario configuration
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
def validate(
    scenario: Annotated[
        Optional[str],
        typer.Option("--scenario", "-s", help="Validate specific scenario")
    ] = None,
    skip_what_if: Annotated[
        bool,
        typer.Option("--skip-what-if", help="Skip what-if analysis (faster)")
    ] = False,
) -> None:
    """
    Validate Bicep templates without deploying.

    Performs:
    - Template syntax validation
    - What-if analysis for resource changes
    """
    from src.deployment import DeploymentEngine
    from src.resource_groups import ResourceGroupManager
    from src.validation import TemplateValidator

    config_manager = check_initialized()

    # Load configuration
    settings = config_manager.load_settings()
    scenarios = config_manager.load_scenarios()

    if not settings or not scenarios:
        console.print("[red]Error: Configuration not loaded. Run setup first.[/red]")
        raise typer.Exit(1)

    # Filter scenarios
    if scenario:
        selected = [s for s in scenarios.scenarios if s.name == scenario]
        if not selected:
            console.print(f"[red]Error: Scenario '{scenario}' not found[/red]")
            raise typer.Exit(1)
        scenarios_to_validate = selected
    else:
        scenarios_to_validate = scenarios.scenarios

    # Initialize shared components
    validator = TemplateValidator()
    rg_manager = ResourceGroupManager()

    # Ensure validation resource group exists
    validation_rg = "arm-validation-temp"
    if not rg_manager.resource_group_exists(validation_rg):
        console.print(f"\n[cyan]Creating validation resource group: {validation_rg}[/cyan]")
        success = rg_manager.create_resource_group(
            validation_rg,
            settings.default_region,
            tags={"purpose": "arm-template-validation", "managed-by": "bicep-deploy"},
        )
        if not success:
            console.print(
                "[red]Error: Could not create validation resource group[/red]"
            )
            raise typer.Exit(1)

    console.print(f"\n[bold]Validating {len(scenarios_to_validate)} Scenario(s)[/bold]\n")

    all_valid = True

    # Track deployment engines per deployment type to avoid recreating
    engines = {}

    for s in scenarios_to_validate:
        console.print(f"\n[bold cyan]Scenario: {s.name}[/bold cyan]")
        console.print("=" * 60)

        # Get or create deployment engine for this deployment type
        from src.models import DeploymentType

        if s.deployment_type not in engines:
            try:
                engines[s.deployment_type] = DeploymentEngine(settings)
            except FileNotFoundError as e:
                console.print(f"[red]Template not found: {e}[/red]")
                all_valid = False
                continue

        engine = engines[s.deployment_type]

        # Generate parameter file
        try:
            param_file = engine.generate_parameter_file(s)
        except Exception as e:
            console.print(f"[red]Failed to generate parameters: {e}[/red]")
            all_valid = False
            continue

        # Validate template
        validation_result = validator.validate_template(
            validation_rg,
            engine.template_file,
            param_file,
        )

        if not validation_result.is_valid:
            console.print(f"[red]Validation failed: {validation_result.error_message}[/red]")
            all_valid = False
            continue

        # What-if analysis (if not skipped)
        if not skip_what_if:
            what_if_result = validator.what_if_analysis(
                validation_rg,
                engine.template_file,
                param_file,
            )

            if what_if_result.status == "Succeeded":
                validator.display_what_if_results(what_if_result)

        console.print()

    # Summary
    console.print("=" * 60)
    if all_valid:
        console.print(f"\n[green]All {len(scenarios_to_validate)} scenario(s) validated successfully[/green]")
    else:
        console.print(f"\n[yellow]Some scenarios failed validation[/yellow]")
        raise typer.Exit(1)


def _generate_param_files(
    scenarios_to_deploy: list,
    engine,
    planner,
    region: Optional[str],
    debug: bool,
) -> list:
    """Generate parameter files for all scenarios. Prints plan; raises typer.Exit on error."""
    console.print(f"\n[bold]Generating Parameter Files[/bold]\n")
    param_files = []
    for s in scenarios_to_deploy:
        try:
            param_file = engine.generate_parameter_file(scenario=s, region=region, debug_mode=debug)
            param_files.append((s, param_file))
            timestamp_str = "-".join(param_file.stem.split("-")[-2:])
            rg_name = planner.generate_resource_group_name(s.name, timestamp_str)
            deploy_name = planner.generate_deployment_name(s.name, timestamp_str)
            console.print(f"  [green]✓[/green] {s.name}")
            console.print(f"    [dim]Resource group: {rg_name}[/dim]")
            console.print(f"    [dim]Deployment: {deploy_name}[/dim]")
            console.print(f"    [dim]Parameters: {param_file}[/dim]\n")
        except Exception as e:
            console.print(f"  [red]✗[/red] {s.name}: {e}")
            raise typer.Exit(1)
    console.print(f"\n[green]✓ Generated {len(param_files)} parameter file(s)[/green]")
    return param_files


def _submit_all_deployments(
    param_files: list,
    engine,
    planner,
    rg_manager,
    orchestrator,
    cleanup,
    git_branch: str,
    settings,
    region: Optional[str],
) -> list:
    """Create resource groups and submit all deployments. Returns submitted DeploymentState list."""
    console.print(f"\n[bold]Creating Resource Groups and Submitting Deployments[/bold]\n")
    deployment_states = []

    for s, param_file in param_files:
        timestamp_str = "-".join(param_file.stem.split("-")[-2:])
        rg_name = planner.generate_resource_group_name(s.name, timestamp_str)
        deploy_name = planner.generate_deployment_name(s.name, timestamp_str)
        deployment_id = str(uuid.uuid4())

        target_region = region or settings.default_region
        tags = rg_manager.generate_tags(
            scenario_name=s.name,
            deployment_id=deployment_id,
            branch=git_branch,
            owner_email=settings.owner_email,
            cleanup_mode=cleanup,
            expires_hours=24,
        )

        console.print(f"[cyan]Creating resource group for {s.name}...[/cyan]")
        if not rg_manager.create_resource_group(rg_name, target_region, tags):
            console.print(f"[red]Failed to create resource group for {s.name}[/red]")
            continue

        is_sub_scoped = s.deployment_type.value == "databricks-peering"
        databricks_rg = None
        if is_sub_scoped:
            with open(param_file) as _f:
                _params = json.load(_f)
            databricks_rg = _params.get("parameters", {}).get("databricksResourceGroup", {}).get("value")

        state = DeploymentState(
            deployment_id=deployment_id,
            engine=Engine.bicep,
            resource_group_name=rg_name,
            deployment_name=deploy_name,
            scenario_name=s.name,
            git_branch=git_branch,
            parameter_file_path=str(param_file),
            cleanup_mode=cleanup,
            status="pending",
            subscription_scoped=is_sub_scoped,
            databricks_resource_group=databricks_rg,
        )
        rg_manager.save_deployment_state(state)

        if orchestrator.submit_deployment(state, param_file, wait=False):
            deployment_states.append(state)
        else:
            console.print(f"[red]Failed to submit deployment for {s.name}[/red]")

    return deployment_states


def _process_deployment_outputs(
    deployment_states: list,
    final_statuses: dict,
    param_files: list,
    engine,
    orchestrator,
    settings,
    cleanup_manager,
) -> tuple[int, int]:
    """Extract outputs, save connection details, display results. Returns (succeeded, failed)."""
    console.print(f"\n[bold]Processing Deployment Outputs[/bold]\n")
    succeeded_count = 0
    failed_count = 0

    for state in deployment_states:
        final_status = final_statuses.get(state.deployment_id)
        console.print(
            f"[dim]_process_deployment_outputs: "
            f"deployment_id={state.deployment_id} "
            f"scenario={state.scenario_name} "
            f"rg={state.resource_group_name} "
            f"dbx_rg={state.databricks_resource_group} "
            f"final_status={final_status}[/dim]"
        )

        if final_status != "Succeeded":
            failed_count += 1
            cleanup_manager.auto_cleanup_deployment(state, no_wait=True)
            continue

        succeeded_count += 1
        outputs = orchestrator.extract_outputs(
            state.resource_group_name,
            state.deployment_name,
            is_subscription_scoped=state.subscription_scoped,
        )

        if not outputs:
            console.print(f"[yellow]Warning: No outputs for {state.scenario_name}[/yellow]")
            continue

        scenario_obj = next((s for s, _ in param_files if s.name == state.scenario_name), None)
        if not scenario_obj:
            console.print(f"[red]Error: Scenario config not found for {state.scenario_name}[/red]")
            continue

        password = engine.password_manager.get_password(state.scenario_name)
        conn_info = orchestrator.parse_connection_info(outputs, state, scenario_obj, password)

        if not conn_info:
            console.print(f"[yellow]Warning: Could not parse connection info for {state.scenario_name}[/yellow]")
            continue

        orchestrator.save_connection_info(conn_info, state.scenario_name)

        src_scenario = scenario_obj.source_scenario
        details_file = save_deployment_details(conn_info, state, settings, source_scenario=src_scenario)

        with open(details_file) as _f:
            saved_details = json.load(_f)
        display_connection_info(saved_details, state.scenario_name)
        console.print(f"[dim]Saved to: {details_file}[/dim]\n")

        cleanup_manager.auto_cleanup_deployment(state, no_wait=True)

    return succeeded_count, failed_count


@app.command()
def deploy(
    scenario: Annotated[
        Optional[str],
        typer.Option("--scenario", "-s", help="Deploy specific scenario by name")
    ] = None,
    all_scenarios: Annotated[
        bool,
        typer.Option("--all", "-a", help="Deploy all configured scenarios")
    ] = False,
    region: Annotated[
        Optional[str],
        typer.Option("--region", "-r", help="Override default Azure region")
    ] = None,
    cleanup_mode: Annotated[
        Optional[str],
        typer.Option("--cleanup-mode", "-c", help="Override cleanup behavior (immediate/on-success/manual/scheduled)")
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-d", help="Preview deployment without executing")
    ] = False,
    debug: Annotated[
        bool,
        typer.Option("--debug", help="Enable debug mode with verbose Neo4j logging")
    ] = False,
) -> None:
    """
    Deploy one or more test scenarios to Azure.

    You must specify either --scenario or --all.

    Examples:
        uv run bicep-deploy deploy --all
        uv run bicep-deploy deploy --scenario standalone-v5
        uv run bicep-deploy deploy --scenario cluster-v5 --region eastus2
        uv run bicep-deploy deploy --all --dry-run
    """
    from rich.table import Table

    from src.cleanup import CleanupManager
    from src.deployment import DeploymentEngine
    from src.monitor import DeploymentMonitor
    from src.orchestrator import DeploymentOrchestrator, DeploymentPlanner
    from src.resource_groups import ResourceGroupManager
    from src.utils import get_git_branch

    config_manager = check_initialized()
    settings = config_manager.load_settings()
    scenarios = config_manager.load_scenarios()

    if not settings or not scenarios:
        console.print("[red]Error: Configuration not loaded. Run setup first.[/red]")
        raise typer.Exit(1)

    if not scenario and not all_scenarios:
        console.print("[red]Error: Must specify either --scenario or --all[/red]")
        console.print("\n[cyan]Available scenarios:[/cyan]")
        for s in scenarios.scenarios:
            console.print(f"  - {s.name}")
        console.print("\n[cyan]Examples:[/cyan]")
        console.print("  uv run bicep-deploy deploy --scenario standalone-v5")
        console.print("  uv run bicep-deploy deploy --all")
        raise typer.Exit(1)

    if scenario and all_scenarios:
        console.print("[red]Error: Cannot specify both --scenario and --all[/red]")
        raise typer.Exit(1)

    if scenario:
        selected = [s for s in scenarios.scenarios if s.name == scenario]
        if not selected:
            console.print(f"[red]Error: Scenario '{scenario}' not found[/red]")
            console.print("\n[cyan]Available scenarios:[/cyan]")
            for s in scenarios.scenarios:
                console.print(f"  - {s.name}")
            raise typer.Exit(1)
        scenarios_to_deploy = selected
    else:
        scenarios_to_deploy = scenarios.scenarios

    try:
        engine = DeploymentEngine(settings)
        planner = DeploymentPlanner(settings.resource_group_prefix)
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    # Display deployment plan
    console.print(f"\n[bold]Deployment Plan[/bold]\n")
    table = Table(title="Scenarios to Deploy")
    table.add_column("Scenario", style="cyan")
    table.add_column("Type", style="white")
    table.add_column("Nodes", style="white")
    table.add_column("Version", style="white")
    table.add_column("Size", style="white")
    table.add_column("Region", style="green")
    for s in scenarios_to_deploy:
        table.add_row(
            s.name,
            s.deployment_type.value,
            str(s.node_count),
            s.graph_database_version,
            s.vm_size or "Standard_E4s_v5",
            region or settings.default_region,
        )
    console.print(table)
    console.print(f"\n[cyan]Total scenarios:[/cyan] {len(scenarios_to_deploy)}")
    console.print(f"[cyan]Dry run:[/cyan] {dry_run}")
    if debug:
        console.print(f"[yellow]Debug mode:[/yellow] ENABLED - Verbose Neo4j logging will be configured")

    param_files = _generate_param_files(scenarios_to_deploy, engine, planner, region, debug)

    if dry_run:
        console.print("\n[yellow]Dry run complete. No resources deployed.[/yellow]")
        console.print("[dim]Remove --dry-run to execute deployment[/dim]")
        return

    # Resolve cleanup mode
    if cleanup_mode:
        try:
            cleanup = CleanupMode(cleanup_mode)
        except ValueError:
            console.print(f"[red]Error: Invalid cleanup mode '{cleanup_mode}'[/red]")
            console.print("[cyan]Valid modes: immediate, on-success, manual, scheduled[/cyan]")
            raise typer.Exit(1)
    else:
        cleanup = settings.default_cleanup_mode

    rg_manager = ResourceGroupManager()
    orchestrator = DeploymentOrchestrator(
        template_file=engine.template_file,
        resource_group_manager=rg_manager,
    )
    monitor = DeploymentMonitor(
        resource_group_manager=rg_manager,
        poll_interval=30,
        timeout_seconds=settings.deployment_timeout,
    )
    cleanup_manager = CleanupManager(rg_manager)
    git_branch = get_git_branch() or "unknown"

    deployment_states = _submit_all_deployments(
        param_files, engine, planner, rg_manager, orchestrator, cleanup, git_branch, settings, region
    )

    if not deployment_states:
        console.print("\n[red]No deployments were submitted successfully[/red]")
        raise typer.Exit(1)

    console.print(f"\n[green]✓ Submitted {len(deployment_states)} deployment(s)[/green]")

    console.print(f"\n[bold]Monitoring Deployments[/bold]\n")
    final_statuses = monitor.monitor_deployments(deployment_states, show_live_dashboard=True)

    succeeded_count, failed_count = _process_deployment_outputs(
        deployment_states, final_statuses, param_files, engine, orchestrator, settings, cleanup_manager
    )

    # Summary
    console.print("\n" + "=" * 60)
    console.print(f"\n[bold]Deployment Summary[/bold]")
    console.print(f"[green]✓ Succeeded:[/green] {succeeded_count}")
    console.print(f"[red]✗ Failed:[/red] {failed_count}")
    console.print(f"[cyan]Total:[/cyan] {len(deployment_states)}")

    if succeeded_count > 0:
        console.print("\n[cyan]Next steps:[/cyan]")
        for state in deployment_states:
            if final_statuses.get(state.deployment_id) == "Succeeded":
                console.print(f"  - Validate {state.scenario_name}: [bold]uv run validate_deploy {state.scenario_name}[/bold]")
        console.print("  - Check status: [bold]uv run bicep-deploy status[/bold]")
        if cleanup == CleanupMode.MANUAL:
            console.print("\n[cyan]Clean up resources:[/cyan]")
            if deployment_states:
                example_id = deployment_states[0].deployment_id[:8]
                console.print(f"  - Individual: [bold]uv run bicep-deploy cleanup --deployment {example_id} --force[/bold]")
            console.print(f"  - All: [bold]uv run bicep-deploy cleanup --all --force[/bold]")
        else:
            console.print(f"  - Cleanup mode: {cleanup.value} (auto-cleanup {'enabled' if cleanup != CleanupMode.MANUAL else 'disabled'})")

    if failed_count > 0:
        raise typer.Exit(1)


@app.command()
def verify(
    deployment_id: Annotated[
        Optional[str],
        typer.Argument(help="Deployment ID to verify (defaults to most recent successful deployment)")
    ] = None,
) -> None:
    """
    Verify an existing deployment by running database-level checks.

    Connects to the deployed Neo4j instance via Bolt and:
    - Creates a test dataset
    - Verifies database connectivity
    - Checks license type
    - Cleans up test data

    For VNet and Databricks connectivity checks use: neo4j-connect check --scenario <name>

    If no deployment ID is provided, verifies the most recent successful deployment.

    Examples:
        uv run bicep-deploy verify                                        # Verify most recent
        uv run bicep-deploy verify d681f330-499d-4523-ba5b-42e28d2b7d12  # Verify specific deployment
    """
    from src.resource_groups import ResourceGroupManager
    from src.validate_deploy import validate_deployment

    config_manager = check_initialized()
    settings = config_manager.load_settings()
    scenarios = config_manager.load_scenarios()

    if not settings or not scenarios:
        console.print("[red]Error: Configuration not loaded. Run setup first.[/red]")
        raise typer.Exit(1)

    # Initialize components
    rg_manager = ResourceGroupManager()

    # If no deployment_id provided, use most recent successful deployment
    if deployment_id is None:
        all_deployments = rg_manager.load_all_deployment_states()

        # Filter for successful deployments and sort by created_at
        successful_deployments = [
            d for d in all_deployments
            if d.status == "succeeded"
        ]

        if not successful_deployments:
            console.print("[red]Error: No successful deployments found.[/red]")
            console.print("[yellow]Deploy first with: uv run bicep-deploy deploy --scenario <scenario-name>[/yellow]")
            raise typer.Exit(1)

        # Sort by created_at (most recent first)
        successful_deployments.sort(key=lambda d: d.created_at, reverse=True)
        deployment_id = successful_deployments[0].deployment_id

        console.print(f"[dim]Using most recent successful deployment: {deployment_id}[/dim]\n")

    console.print(f"[cyan]Verifying deployment: {deployment_id}[/cyan]\n")

    # Get deployment state
    deployment_state = rg_manager.get_deployment_state(deployment_id)

    if not deployment_state:
        console.print(f"[red]Error: Deployment {deployment_id} not found[/red]")
        console.print("[yellow]Run 'uv run bicep-deploy status' to see available deployments[/yellow]")
        raise typer.Exit(1)

    console.print(f"[dim]Scenario: {deployment_state.scenario_name}[/dim]")
    console.print(f"[dim]Resource Group: {deployment_state.resource_group_name}[/dim]\n")

    # Find scenario configuration
    scenario_cfg = next((s for s in scenarios.scenarios if s.name == deployment_state.scenario_name), None)

    if not scenario_cfg:
        console.print(f"[red]Error: Scenario '{deployment_state.scenario_name}' not found in configuration[/red]")
        raise typer.Exit(1)

    # Load connection info from .arm-testing/results
    from src.validate_deploy import load_connection_info_from_scenario

    conn_data = load_connection_info_from_scenario(deployment_state.scenario_name)
    if not conn_data:
        console.print(f"[red]Error: No connection information found for {deployment_state.scenario_name}[/red]")
        console.print("[yellow]Connection info is created after successful deployment[/yellow]")
        raise typer.Exit(1)

    # Extract connection details
    uri = conn_data.get("neo4j_uri")
    username = conn_data.get("username", "neo4j")
    password = conn_data.get("password")

    if not uri or not password:
        console.print("[red]Error: Connection info is incomplete[/red]")
        raise typer.Exit(1)

    # Run validation
    console.print(f"[cyan]Running validation...[/cyan]\n")

    try:
        success = validate_deployment(uri, username, password, scenario_cfg.license_type)

        console.print("\n" + "=" * 60)
        console.print(f"\n[bold]Test Results[/bold]\n")

        if success:
            console.print(f"[green]✓ All tests PASSED[/green]")
        else:
            console.print(f"[red]✗ Tests FAILED[/red]")
            raise typer.Exit(1)

    except Exception as e:
        console.print(f"\n[red]✗ Test execution failed: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def status(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show detailed status information")
    ] = False,
) -> None:
    """
    Show status of active deployments.

    Displays:
    - Deployment ID and name
    - Scenario name
    - Region and resource group
    - Status (pending/deploying/succeeded/failed/deleted)
    - Creation time
    """
    from rich.table import Table
    from src.resource_groups import ResourceGroupManager

    check_initialized()

    # Load all deployment states
    rg_manager = ResourceGroupManager()
    deployments = rg_manager.load_all_deployment_states()

    if not deployments:
        console.print("[yellow]No deployments found[/yellow]")
        console.print("\n[cyan]Deploy a scenario:[/cyan]")
        console.print("  uv run bicep-deploy deploy --scenario standalone-v5")
        raise typer.Exit(0)

    # Filter out deleted deployments unless verbose
    if not verbose:
        deployments = [d for d in deployments if d.status != "deleted"]

    if not deployments:
        console.print("[yellow]No active deployments[/yellow]")
        console.print("[dim]Use --verbose to see deleted deployments[/dim]")
        raise typer.Exit(0)

    # Create status table
    table = Table(title=f"Deployment Status ({len(deployments)} deployment(s))")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Scenario", style="white")
    table.add_column("Resource Group", style="dim")
    table.add_column("Status", style="white")
    table.add_column("Cleanup Mode", style="white")
    table.add_column("Created", style="dim")

    if verbose:
        table.add_column("Branch", style="dim")
        table.add_column("Deployment Name", style="dim")

    # Sort by creation time (newest first)
    deployments_sorted = sorted(deployments, key=lambda d: d.created_at, reverse=True)

    for deployment in deployments_sorted:
        # Format status with color
        status_str = deployment.status
        if deployment.status == "succeeded":
            status_str = f"[green]{deployment.status}[/green]"
        elif deployment.status == "failed":
            status_str = f"[red]{deployment.status}[/red]"
        elif deployment.status == "deleted":
            status_str = f"[dim]{deployment.status}[/dim]"
        elif deployment.status == "deploying":
            status_str = f"[yellow]{deployment.status}[/yellow]"

        # Format deployment ID (show first 8 chars)
        short_id = deployment.deployment_id[:8]

        # Format creation time (relative)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        age = now - deployment.created_at

        if age.days > 0:
            created_str = f"{age.days}d ago"
        elif age.seconds > 3600:
            created_str = f"{age.seconds // 3600}h ago"
        elif age.seconds > 60:
            created_str = f"{age.seconds // 60}m ago"
        else:
            created_str = "just now"

        # Add row
        row = [
            short_id,
            deployment.scenario_name,
            deployment.resource_group_name,
            status_str,
            deployment.cleanup_mode.value,
            created_str,
        ]

        if verbose:
            row.append(deployment.git_branch)
            row.append(deployment.deployment_name)

        table.add_row(*row)

    console.print(table)

    # Show summary
    console.print()
    active_count = sum(1 for d in deployments if d.status not in ["deleted", "failed"])
    if active_count > 0:
        console.print(f"[cyan]Active deployments:[/cyan] {active_count}")
        console.print("\n[dim]To clean up:[/dim]")
        console.print("  uv run bicep-deploy cleanup --deployment <id> --force")
        console.print("  uv run bicep-deploy cleanup --all --force")


@app.command()
def cleanup(
    deployment: Annotated[
        Optional[str],
        typer.Option("--deployment", "-d", help="Clean up specific deployment by ID")
    ] = None,
    all_deployments: Annotated[
        bool,
        typer.Option("--all", "-a", help="Clean up all resources")
    ] = False,
    older_than: Annotated[
        Optional[str],
        typer.Option("--older-than", "-o", help="Clean up resources older than duration (e.g., '2h', '1d')")
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip confirmation prompts")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview cleanup without executing")
    ] = False,
) -> None:
    """
    Clean up Azure resources from test deployments.

    Cleanup modes:
    - immediate: Delete resources immediately after deployment
    - on-success: Delete only if tests passed (keep failures for debugging)
    - manual: Never auto-delete (requires --force flag)
    - scheduled: Delete when expiration time is reached

    Examples:
        uv run bicep-deploy cleanup --deployment 2c4ca18c --force
        uv run bicep-deploy cleanup --all --force
        uv run bicep-deploy cleanup --older-than 24h
        uv run bicep-deploy cleanup --all --dry-run
    """
    from src.cleanup import CleanupManager
    from src.resource_groups import ResourceGroupManager

    check_initialized()

    if not deployment and not all_deployments and not older_than:
        console.print("[red]Error: Must specify --deployment, --all, or --older-than[/red]")
        console.print("\n[cyan]Examples:[/cyan]")
        console.print("  uv run bicep-deploy cleanup --deployment 2c4ca18c --force")
        console.print("  uv run bicep-deploy cleanup --all --force")
        console.print("  uv run bicep-deploy cleanup --older-than 24h")
        raise typer.Exit(1)

    # Initialize components
    rg_manager = ResourceGroupManager()
    cleanup_manager = CleanupManager(rg_manager)

    # Load all deployments
    all_states = rg_manager.load_all_deployment_states()

    if not all_states:
        console.print("[yellow]No deployments found in state file[/yellow]")
        raise typer.Exit(0)

    # Filter deployments based on criteria
    deployments_to_cleanup = []

    if deployment:
        # Clean up specific deployment by ID (partial match supported)
        matching = [
            d for d in all_states
            if d.deployment_id.startswith(deployment) or deployment in d.deployment_id
        ]

        if not matching:
            console.print(f"[red]Error: No deployment found matching '{deployment}'[/red]")
            console.print("[yellow]Run 'uv run bicep-deploy status' to see available deployments[/yellow]")
            raise typer.Exit(1)

        if len(matching) > 1:
            console.print(f"[yellow]Warning: Multiple deployments match '{deployment}':[/yellow]")
            for d in matching:
                console.print(f"  - {d.deployment_id} ({d.scenario_name})")
            console.print("\n[yellow]Please provide a more specific deployment ID[/yellow]")
            raise typer.Exit(1)

        deployments_to_cleanup = matching

    elif older_than:
        # Clean up deployments older than specified duration
        filtered = cleanup_manager.filter_deployments_by_age(all_states, older_than)

        if not filtered:
            console.print(f"[yellow]No deployments found older than {older_than}[/yellow]")
            raise typer.Exit(0)

        deployments_to_cleanup = filtered

    elif all_deployments:
        # Clean up all deployments (excluding already deleted)
        deployments_to_cleanup = [
            d for d in all_states
            if d.status != "deleted"
        ]

        if not deployments_to_cleanup:
            console.print("[yellow]No active deployments to clean up[/yellow]")
            raise typer.Exit(0)

    # Execute cleanup
    summary = cleanup_manager.cleanup_deployments(
        deployments=deployments_to_cleanup,
        dry_run=dry_run,
        force=force,
        no_wait=True,
    )

    # Display summary
    cleanup_manager.display_cleanup_summary(summary, dry_run=dry_run)

    # Exit with error if any failed
    if summary.failed > 0:
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
        typer.Option(
            "--notebook-path",
            help="Workspace path to upload the notebook (default: /Shared/neo4j-{scenario}-connectivity-test)",
        ),
    ] = None,
) -> None:
    """
    Create Databricks secrets and upload the connectivity test notebook for a Neo4j deployment.

    Reads .deployments/{scenario}-{engine}.json and:
    - Creates a secrets scope named neo4j-bicep-{scenario}
    - Uploads 5 secrets: bolt_uri, host, username, password, database
    - Uploads the connectivity test notebook to the Databricks workspace

    Auth options (one required):
      --token <pat>     Personal access token — generate in Databricks UI under
                        User Settings > Developer > Access tokens
      --profile <name>  Named profile from ~/.databrickscfg
    """
    from rich.panel import Panel
    from rich.table import Table

    details_file = find_deployment_file(scenario, DEPLOYMENTS_DIR, Engine.bicep)
    if not details_file:
        console.print(f"[red]No bicep deployment found for scenario: {scenario}[/red]")
        console.print(f"[dim]Run: uv run bicep-deploy deploy --scenario {scenario}[/dim]")
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
            "Re-deploy to regenerate the JSON with the required fields.[/dim]"
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

    scope_name = f"neo4j-bicep-{scenario}"
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
            help="Databricks NCC name. Defaults to 'neo4j-bicep-<sha1(scenario)[:8]>'. "
                 "NCCs are account-level objects — name must be ≤30 chars. "
                 "Use a unique name when multiple deployments need isolated NCCs.",
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

    Reads .deployments/{scenario}-{engine}.json and:
    - Creates or reuses a Databricks NCC in the workspace region
    - Attaches the NCC to the workspace
    - Creates a PE rule with domain_names=[--domain-name] pointing at the PLS
    - Polls for the Pending endpoint connection and approves it via Azure CLI

    Auth: pass --account-profile <name> for a ~/.databrickscfg profile that has
    host + account_id (e.g. azure-account-admin). Falls back to AAD token from az login.

    After setup-ncc completes, use 'bolt://<domain-name>:7687' in serverless notebooks.
    """
    import subprocess as _sp

    from rich.panel import Panel
    from rich.table import Table

    details_file = find_deployment_file(scenario, DEPLOYMENTS_DIR, Engine.bicep)
    if not details_file:
        console.print(f"[red]No bicep deployment found for scenario: {scenario}[/red]")
        console.print(f"[dim]Run: uv run bicep-deploy deploy --scenario {scenario}[/dim]")
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

    # --- Resolve subscription ID to construct the deterministic PLS ARM resource ID ---
    try:
        sub_result = _sp.run(
            ["az", "account", "show", "--query", "id", "--output", "tsv"],
            capture_output=True, text=True, check=True,
        )
        subscription_id = sub_result.stdout.strip()
    except _sp.CalledProcessError as e:
        console.print(f"[red]Failed to get Azure subscription ID: {e.stderr}[/red]")
        raise typer.Exit(1)

    pls_resource_id = (
        f"/subscriptions/{subscription_id}"
        f"/resourceGroups/{neo4j_rg}"
        f"/providers/Microsoft.Network/privateLinkServices/{pls_name}"
    )

    # --- Resolve workspace region from the Databricks resource group location ---
    try:
        region_result = _sp.run(
            ["az", "group", "show", "--name", databricks_rg, "--query", "location", "--output", "tsv"],
            capture_output=True, text=True, check=True,
        )
        workspace_region = region_result.stdout.strip()
    except _sp.CalledProcessError as e:
        console.print(f"[red]Failed to get workspace region from resource group '{databricks_rg}': {e.stderr}[/red]")
        raise typer.Exit(1)

    # --- Resolve numeric workspace ID ---
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

    # --- Get AAD token for Databricks Account API ---
    # DATABRICKS_PLATFORM_APP_ID is the fixed, globally-registered Azure AD
    # application ID for the Databricks platform service — identical in every tenant.
    try:
        token_result = _sp.run(
            [
                "az", "account", "get-access-token",
                "--resource", DATABRICKS_PLATFORM_APP_ID,
                "--query", "accessToken",
                "--output", "tsv",
            ],
            capture_output=True, text=True, check=True,
        )
        token = token_result.stdout.strip()
    except _sp.CalledProcessError as e:
        console.print(f"[red]Failed to get Databricks AAD token: {e.stderr}[/red]")
        raise typer.Exit(1)

    # Default NCC name to scenario-scoped name when not explicitly provided
    resolved_ncc_name = ncc_name or f"neo4j-bicep-{hashlib.sha1(scenario.encode()).hexdigest()[:8]}"

    # --- Run NCC setup ---
    console.print(f"\n[bold]NCC Setup[/bold]")
    console.print(f"  PLS resource ID: [dim]{pls_resource_id}[/dim]")
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

    # --- Persist serverless block to deployment JSON (best-effort) ---
    try:
        with open(details_file) as _f:
            _data = json.load(_f)
        _data["serverless"] = {
            "ncc_configured": True,
            "domain_name": domain_name,
            "bolt_uri": f"bolt://{domain_name}:{NEO4J_BOLT_PORT}",
            "pls_name": pls_name,
        }
        with open(details_file, "w") as _f:
            json.dump(_data, _f, indent=2)
    except Exception as _e:
        console.print(f"[yellow]Warning: could not update deployment JSON: {_e}[/yellow]")

    bolt_uri = f"bolt://{domain_name}:{NEO4J_BOLT_PORT}"
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Label", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("NCC name", resolved_ncc_name)
    table.add_row("PLS name", pls_name)
    table.add_row("domain_name", domain_name)
    table.add_row("Serverless bolt URI", bolt_uri)
    table.add_row("", "")
    table.add_row("Next step", f"Connect from a serverless notebook with:")
    table.add_row("", f'GraphDatabase.driver("{bolt_uri}", auth=("neo4j", "<password>"))')
    console.print(
        Panel(table, title="[bold green]NCC Setup Complete[/bold green]", border_style="green")
    )


@app.command()
def report(
    deployment_id: Annotated[
        Optional[str],
        typer.Argument(help="Deployment ID to generate report for (optional)")
    ] = None,
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Output file path (default: .arm-testing/results/)")
    ] = None,
    format: Annotated[
        str,
        typer.Option("--format", "-f", help="Report format (json/yaml/markdown)")
    ] = "markdown",
) -> None:
    """
    Generate test report for deployments.

    If no deployment_id specified, generates a summary report of all deployments.

    Examples:
        uv run bicep-deploy report
        uv run bicep-deploy report abc123
        uv run bicep-deploy report --format json --output report.json
    """
    check_initialized()
    console.print("[yellow]Report command not yet implemented[/yellow]")

    if deployment_id:
        console.print(f"[cyan]Would generate report for:[/cyan] {deployment_id}")
    else:
        console.print(f"[cyan]Would generate summary report for all deployments[/cyan]")

    console.print(f"[cyan]Format:[/cyan] {format}")
    if output:
        console.print(f"[cyan]Output:[/cyan] {output}")


def main() -> int:
    """
    Main entry point for the CLI.

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    try:
        app()
        return 0
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        return 130
    except Exception as e:
        console.print(f"\n[red]Error: {e}[/red]")
        import traceback
        console.print(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
