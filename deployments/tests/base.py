"""
Shared types and Azure CLI helpers used by all connectivity test modules.
"""

import subprocess
from dataclasses import dataclass, field

from rich.console import Console

console = Console()

DATABRICKS_RESOURCE_ID = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"


@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class TestReport:
    label: str
    results: list[TestResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)


def _az(*args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        ["az", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _get_vmss(rg: str) -> tuple[str | None, list[str]]:
    """Return (vmss_name, [instance_ids]) for the first VMSS in the resource group."""
    rc, name, _ = _az("vmss", "list", "-g", rg, "--query", "[0].name", "-o", "tsv")
    if rc != 0 or not name:
        return None, []
    rc2, ids_out, _ = _az(
        "vmss", "list-instances", "-g", rg, "-n", name,
        "--query", "[].instanceId", "-o", "tsv",
    )
    instance_ids = ids_out.split() if rc2 == 0 and ids_out else []
    return name, instance_ids


def _vmss_run(rg: str, vmss_name: str, instance_id: str, script: str) -> tuple[bool, str]:
    """Run a shell script on a VMSS instance via az run-command. Returns (success, stdout)."""
    rc, out, err = _az(
        "vmss", "run-command", "invoke",
        "-g", rg, "-n", vmss_name,
        "--instance-id", instance_id,
        "--command-id", "RunShellScript",
        "--scripts", script,
        "--query", "value[0].message",
        "-o", "tsv",
    )
    if rc != 0:
        return False, err
    stdout = ""
    if "[stdout]" in out:
        after = out.split("[stdout]", 1)[1]
        stdout = after.split("[stderr]")[0].strip() if "[stderr]" in after else after.strip()
    return True, stdout


def _print_result(r: TestResult) -> None:
    status = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
    detail = f" — {r.detail}" if r.detail else ""
    console.print(f"  [{status}] {r.name}{detail}")
