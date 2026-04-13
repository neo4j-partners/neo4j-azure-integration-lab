#!/usr/bin/env python3
"""
Neo4j connectivity test suite.

Reads .deployments/{scenario}-{engine}.json and runs VNet-internal and
Databricks cross-VNet checks. --engine is required: bicep or ansible.
"""

import json
from enum import Enum
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from typing_extensions import Annotated

from src.models import ComputeType, Engine
from src.utils import find_deployment_file

DEPLOYMENTS_DIR = Path(__file__).parent.parent / ".deployments"

app = typer.Typer(
    name="neo4j-connect",
    help="Neo4j connectivity test suite — VNet-internal and Databricks cross-VNet checks",
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()


class CheckSuite(str, Enum):
    vnet = "vnet"
    databricks = "databricks"
    all = "all"


@app.command()
def check(
    scenario: Annotated[
        str,
        typer.Option("--scenario", "-s", help="Scenario name (e.g. peer-databricks-v2025)"),
    ],
    engine: Annotated[
        Engine,
        typer.Option("--engine", "-e", help="Deployment engine: bicep or ansible"),
    ],
    checks: Annotated[
        CheckSuite,
        typer.Option("--checks", "-c", help="Which checks to run: vnet, databricks, or all (default: all)"),
    ] = CheckSuite.all,
    compute: Annotated[
        ComputeType,
        typer.Option(
            "--compute",
            "-C",
            help=(
                "Databricks compute type: classic, serverless, both, or auto. "
                "auto (default) runs classic when databricks_workspace_host is present "
                "and adds serverless when serverless.ncc_configured is set."
            ),
        ),
    ] = ComputeType.auto,
    update_doc: Annotated[
        Path | None,
        typer.Option("--update-doc", help="Markdown file to insert results into (replaces existing section on re-run)"),
    ] = None,
) -> None:
    """
    Run connectivity checks for a deployed Neo4j scenario.

    Reads .deployments/{scenario}-{engine}.json — engine is required.

    [bold]VNet checks[/bold] (--checks vnet): peering, NSG rules, VMSS instance state,
    Neo4j service health per node, and LB Bolt connectivity. Runs entirely via the
    Azure CLI and vmss run-command — no Databricks credentials needed. ~3-5 minutes.

    [bold]Databricks checks[/bold] (--checks databricks): submits a SparkPythonTask job
    that TCP-probes the Neo4j LB from inside the Databricks container subnet. This is
    the only test that proves the actual Databricks → Neo4j path. Requires a Databricks
    workspace in the deployment profile. Automatically skipped when running --checks all
    with no workspace. ~5-8 minutes (cluster cold start).

    Examples:
        uv run neo4j-connect check --scenario peer-databricks-v2025 --engine bicep
        uv run neo4j-connect check --scenario peer-databricks-v2025 --engine bicep --checks vnet
        uv run neo4j-connect check --scenario peer-databricks-v2025 --engine ansible --checks databricks
        uv run neo4j-connect check --scenario peer-databricks-v2025 --engine bicep --update-doc no-connect-v2.md
    """
    details_file = find_deployment_file(scenario, DEPLOYMENTS_DIR, engine)
    if not details_file:
        console.print(f"[red]No {engine.value} deployment found for scenario: {scenario}[/red]")
        console.print(f"[dim]Run: {engine.value}-deploy deploy --scenario {scenario}[/dim]")
        raise typer.Exit(1)

    with open(details_file) as f:
        deployment = json.load(f)

    console.print(
        f"\n[dim]Profile: {details_file.name} (engine: {deployment.get('engine', 'unknown')})[/dim]"
    )

    from tests.runner import run_databricks_tests, run_vnet_tests
    from tests.runner import update_doc as _update_doc

    conn = deployment.get("connection", {})
    has_workspace = bool(conn.get("databricks_workspace_host"))
    any_failure = False

    if checks in (CheckSuite.all, CheckSuite.vnet):
        vnet_report = run_vnet_tests(deployment)
        _print_report(vnet_report)
        if update_doc and update_doc.exists():
            _update_doc(update_doc, vnet_report.label, vnet_report)
            console.print(f"[dim]Updated {update_doc.name}[/dim]")
        if not vnet_report.passed:
            any_failure = True

    if checks in (CheckSuite.all, CheckSuite.databricks):
        if not has_workspace:
            if checks == CheckSuite.databricks:
                console.print(
                    "[red]No Databricks workspace in this deployment profile. "
                    "Cannot run Databricks checks.[/red]"
                )
                raise typer.Exit(1)
            console.print("[dim]No Databricks workspace in profile — skipping Databricks checks.[/dim]")
        else:
            dbx_report = run_databricks_tests(deployment, compute=compute)
            _print_report(dbx_report)
            if update_doc and update_doc.exists():
                _update_doc(update_doc, dbx_report.label, dbx_report)
                console.print(f"[dim]Updated {update_doc.name}[/dim]")
            if not dbx_report.passed:
                any_failure = True

    if any_failure:
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """
    List all deployment profiles (.deployments/*.json) across both engines.

    Groups entries by scenario name and shows engine, state, LB IP,
    Databricks workspace, and creation timestamp.

    Use neo4j-connect check --scenario <name> to run connectivity checks
    for any profile listed here.
    """
    if not DEPLOYMENTS_DIR.exists():
        console.print("[yellow]No .deployments directory found.[/yellow]")
        raise typer.Exit(0)

    profiles = sorted(DEPLOYMENTS_DIR.glob("*.json"))
    if not profiles:
        console.print("[yellow]No deployment profiles found.[/yellow]")
        console.print("[dim]Deploy first: bicep-deploy deploy or ansible-deploy deploy[/dim]")
        raise typer.Exit(0)

    # Group by scenario name, tagged by engine
    by_scenario: dict[str, list[dict]] = {}
    for p in profiles:
        stem = p.stem
        matched = next((e for e in Engine if stem.endswith(f"-{e.value}")), None)
        if matched:
            scenario_name = stem[:-(len(matched.value) + 1)]
            engine_label = matched.value
        else:
            scenario_name = stem
            engine_label = "unknown"

        try:
            with open(p) as f:
                data = json.load(f)
        except Exception:
            continue

        conn = data.get("connection", {})
        by_scenario.setdefault(scenario_name, []).append({
            "engine": engine_label,
            "state": data.get("state", "unknown"),
            "created_at": data.get("created_at", ""),
            "lb_ip": conn.get("lb_private_ip", ""),
            "workspace": conn.get("databricks_workspace_host", ""),
        })

    table = Table(title="Deployment Profiles (.deployments/)")
    table.add_column("Scenario", style="cyan")
    table.add_column("Engine", style="white")
    table.add_column("State", style="white")
    table.add_column("LB IP", style="dim")
    table.add_column("Databricks Workspace", style="dim")
    table.add_column("Created", style="dim")

    for scenario_name in sorted(by_scenario):
        entries = sorted(by_scenario[scenario_name], key=lambda e: e["created_at"])
        for entry in entries:
            state = entry["state"]
            state_str = f"[green]{state}[/green]" if state == "complete" else f"[yellow]{state}[/yellow]"
            created = entry["created_at"][:16].replace("T", " ") if entry["created_at"] else ""
            table.add_row(
                scenario_name,
                entry["engine"],
                state_str,
                entry["lb_ip"],
                entry["workspace"],
                created,
            )

    console.print(table)
    console.print("\n[dim]Run checks: neo4j-connect check --scenario <name>[/dim]")


def _print_report(report) -> None:
    color = "green" if report.passed else "red"
    console.print(
        f"\n[bold {color}]{report.label}: "
        f"{report.pass_count} passed, {report.fail_count} failed[/bold {color}]"
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
