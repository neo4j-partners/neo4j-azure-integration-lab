"""
Test orchestrator.

Composes the individual checkers into full test runs and handles doc updates.
Callers that want to run everything should use run_vnet_tests() and
run_databricks_tests(); the shim in src/test_runner.py wraps these for
ansible_deploy.py backward compatibility.
"""

import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from .base import DeploymentProfile, TestReport, TestResult, _print_result
from .bolt import BoltChecker
from .databricks import DatabricksChecker
from .databricks_serverless import ServerlessDatabricksChecker
from .neo4j_service import NeoServiceChecker
from .nsg import NSGChecker
from .peering import PeeringChecker
from .vmss import VMSSChecker

console = Console()


def run_vnet_tests(deployment: dict) -> TestReport:
    """
    Run all VNet-internal checks: peering, NSG, VMSS state, LB probes,
    Neo4j service health, and Bolt connectivity through the LB.

    Peering checks are skipped when no databricks_resource_group is present.
    """
    profile = DeploymentProfile.model_validate(deployment)
    neo4j_rg = profile.effective_neo4j_rg
    dbx_rg = profile.databricks_resource_group
    lb_ip = profile.connection.lb_private_ip
    username = profile.connection.username
    password = profile.connection.password

    report = TestReport(label="VNet connectivity checks")
    console.print("\n[bold cyan]VNet connectivity checks[/bold cyan]")

    if dbx_rg:
        for r in PeeringChecker(neo4j_rg, dbx_rg).run():
            _print_result(r)
            report.results.append(r)

    for r in NSGChecker(neo4j_rg).run():
        _print_result(r)
        report.results.append(r)

    for r in VMSSChecker(neo4j_rg).run():
        _print_result(r)
        report.results.append(r)

    console.print("[dim]Checking Neo4j service on all nodes (run-command)...[/dim]")
    for r in NeoServiceChecker(neo4j_rg).run():
        _print_result(r)
        report.results.append(r)

    if lb_ip:
        console.print("[dim]Testing LB connectivity...[/dim]")
        for r in BoltChecker(neo4j_rg, lb_ip, username, password).run():
            _print_result(r)
            report.results.append(r)

    return report


def run_databricks_tests(deployment: dict, compute: str = "auto") -> TestReport:
    """
    Run Databricks connectivity checks: submit a SparkPythonTask probe that
    TCP- and Bolt-tests the Neo4j LB from inside Databricks compute.

    compute may be "auto", "classic", "serverless", or "both".
    When "auto", classic runs if workspace_host is present and serverless
    runs if ncc_configured is set in the deployment's serverless block.
    """
    profile = DeploymentProfile.model_validate(deployment)
    lb_ip = profile.connection.lb_private_ip
    workspace_host = profile.connection.databricks_workspace_host
    username = profile.connection.username
    password = profile.connection.password
    domain_name = profile.serverless.domain_name
    serverless_bolt_uri = profile.serverless.bolt_uri
    ncc_configured = profile.serverless.ncc_configured

    run_classic = compute in ("classic", "both") or (compute == "auto" and bool(workspace_host))
    run_serverless = compute in ("serverless", "both") or (compute == "auto" and ncc_configured)

    report = TestReport(label="Databricks connectivity checks")

    if compute == "classic" and not workspace_host:
        report.results.append(TestResult(
            "Classic compute check",
            False,
            "No Databricks workspace in this deployment profile — databricks_workspace_host is not set",
        ))
        return report

    if compute == "serverless" and not ncc_configured:
        report.results.append(TestResult(
            "Serverless compute check",
            False,
            "setup-ncc has not been run for this scenario — serverless.ncc_configured is not set",
        ))
        return report

    if run_classic and run_serverless:
        console.print(
            "\n[bold cyan]Classic + Serverless compute checks — running in parallel[/bold cyan]"
        )
        console.print("[dim](progress messages from both jobs may interleave)[/dim]")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            classic_future = executor.submit(
                DatabricksChecker(lb_ip, workspace_host, username, password).run
            )
            serverless_future = executor.submit(
                ServerlessDatabricksChecker(
                    domain_name, serverless_bolt_uri, workspace_host, username, password
                ).run
            )
            classic_results = classic_future.result()
            serverless_results = serverless_future.result()
        console.print("\n[bold cyan]Classic compute results[/bold cyan]")
        for r in classic_results:
            _print_result(r)
            report.results.append(r)
        console.print("\n[bold cyan]Serverless compute results[/bold cyan]")
        for r in serverless_results:
            _print_result(r)
            report.results.append(r)
    elif run_classic:
        console.print("\n[bold cyan]Classic compute checks[/bold cyan]")
        for r in DatabricksChecker(lb_ip, workspace_host, username, password).run():
            _print_result(r)
            report.results.append(r)
    elif run_serverless:
        console.print("\n[bold cyan]Serverless compute checks[/bold cyan]")
        for r in ServerlessDatabricksChecker(
            domain_name, serverless_bolt_uri, workspace_host, username, password
        ).run():
            _print_result(r)
            report.results.append(r)

    return report


# --- Doc update helpers ---

def _build_results_section(label: str, report: TestReport) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    overall = "PASS" if report.passed else "FAIL"

    rows = ["| | Check | Detail |", "|---|---|---|"]
    for r in report.results:
        icon = "✅" if r.passed else "❌"
        detail = r.detail.replace("|", "\\|")[:120] if r.detail else ""
        rows.append(f"| {icon} | {r.name} | {detail} |")

    return "\n".join([
        f"## {label} — {ts}",
        "",
        f"**{report.pass_count}/{len(report.results)} checks passed** — {overall}",
        "",
        *rows,
    ])


def update_doc(doc_path: Path, label: str, report: TestReport) -> None:
    """Insert or replace a named results section in the given markdown file."""
    content = doc_path.read_text()
    new_section = _build_results_section(label, report)
    marker = f"\n## {label}"

    if marker in content:
        start = content.index(marker)
        rest = content[start + 1:]
        next_h2 = rest.find("\n## ")
        if next_h2 == -1:
            content = content[:start] + "\n" + new_section + "\n"
        else:
            content = content[:start] + "\n" + new_section + "\n\n" + rest[next_h2 + 1:]
    else:
        content = content.rstrip() + "\n\n" + new_section + "\n"

    doc_path.write_text(content)
