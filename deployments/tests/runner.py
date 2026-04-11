"""
Test orchestrator.

Composes the individual checkers into full test runs and handles doc updates.
Callers that want to run everything should use run_vnet_tests() and
run_databricks_tests(); the shim in src/test_runner.py wraps these for
ansible_deploy.py backward compatibility.
"""

from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from .base import TestReport, _print_result
from .bolt import BoltChecker
from .databricks import DatabricksChecker
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
    neo4j_rg = deployment.get("neo4j_resource_group") or deployment.get("resource_group", "")
    dbx_rg = deployment.get("databricks_resource_group", "")
    conn = deployment.get("connection", {})
    lb_ip = conn.get("lb_private_ip", "")
    username = conn.get("username", "neo4j")
    password = conn.get("password", "")

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


def run_databricks_tests(deployment: dict) -> TestReport:
    """
    Run the cross-VNet check: authenticate to Databricks and submit a
    notebook job that TCP-probes the Neo4j LB from the Databricks container
    subnet.
    """
    conn = deployment.get("connection", {})
    lb_ip = conn.get("lb_private_ip", "")
    workspace_host = conn.get("databricks_workspace_host", "")

    report = TestReport(label="Databricks connectivity checks")
    console.print("\n[bold cyan]Databricks connectivity checks[/bold cyan]")

    for r in DatabricksChecker(lb_ip, workspace_host).run():
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
