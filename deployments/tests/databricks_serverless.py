"""
Databricks serverless connectivity checker for Neo4j.

Authenticates to the Databricks workspace using an AAD token from the
logged-in Azure CLI session (no PAT required), then submits a SparkPythonTask
job with no cluster spec so Databricks routes it to serverless compute.
A job environment spec injects the neo4j pip package without a runtime
install step.  The probe script is auto-uploaded to the workspace before
each run to ensure it stays current.
"""

import base64
import time
from pathlib import Path
from typing import Optional

from rich.console import Console

from .base import DATABRICKS_RESOURCE_ID, TestResult, _az

console = Console()

# Pre-uploaded to the Databricks workspace by setup-databricks and kept
# current by _ensure_probe_script before every test run.
WORKSPACE_SERVERLESS_PROBE_PATH = "/Shared/neo4j-serverless-probe.py"

# Probe script lives on disk alongside the classic probe.
_SERVERLESS_PROBE_PATH = Path(__file__).parent.parent.parent / "notebooks" / "neo4j_serverless_probe.py"

# Ordered check keys matching the probe script output.
_CHECK_KEYS = [
    ("DNS",      "DNS resolution (from Databricks serverless)"),
    ("7687",     "TCP 7687 (from Databricks serverless)"),
    ("7474",     "TCP 7474 (from Databricks serverless)"),
    ("BOLT",     "Bolt driver (from Databricks serverless)"),
    ("TOPOLOGY", "Cluster topology (from Databricks serverless)"),
]

_SKIPPED_RESULTS = [TestResult(label, False, "{reason}") for _, label in _CHECK_KEYS]


def _skipped(reason: str) -> list[TestResult]:
    return [TestResult(label, False, reason) for _, label in _CHECK_KEYS]


class ServerlessDatabricksChecker:
    def __init__(
        self,
        domain_name: str,
        bolt_uri: str,
        workspace_host: str,
        username: str = "neo4j",
        password: str = "",
    ) -> None:
        self.domain_name = domain_name
        self.bolt_uri = bolt_uri
        self.workspace_host = workspace_host
        self.username = username
        self.password = password
        self._client = None

    def _get_aad_token(self) -> Optional[str]:
        rc, out, _ = _az(
            "account", "get-access-token",
            "--resource", DATABRICKS_RESOURCE_ID,
            "--query", "accessToken",
            "-o", "tsv",
        )
        return out if rc == 0 and out else None

    def _get_client(self):
        if self._client:
            return self._client
        token = self._get_aad_token()
        if not token:
            return None
        try:
            from databricks.sdk import WorkspaceClient
            self._client = WorkspaceClient(host=self.workspace_host, token=token)
            return self._client
        except Exception:
            return None

    def check_workspace_reachable(self) -> TestResult:
        token = self._get_aad_token()
        if not token:
            return TestResult("Databricks AAD token", False, "az account get-access-token failed")
        client = self._get_client()
        if not client:
            return TestResult("Databricks workspace API", False, "WorkspaceClient init failed")
        try:
            me = client.current_user.me()
            return TestResult(
                "Databricks workspace API",
                True,
                f"authenticated as {me.user_name or 'unknown'}",
            )
        except Exception as e:
            return TestResult("Databricks workspace API", False, str(e)[:120])

    def _ensure_probe_script(self, client) -> None:
        """Upload the serverless probe script to the workspace. Overwrites to keep it current."""
        if not _SERVERLESS_PROBE_PATH.exists():
            raise FileNotFoundError(
                f"Serverless probe script not found: {_SERVERLESS_PROBE_PATH}\n"
                "Run from the repo root or ensure notebooks/neo4j_serverless_probe.py exists."
            )
        from databricks.sdk.service.workspace import ImportFormat, Language
        encoded = base64.b64encode(_SERVERLESS_PROBE_PATH.read_bytes()).decode()
        client.workspace.import_(
            path=WORKSPACE_SERVERLESS_PROBE_PATH,
            format=ImportFormat.SOURCE,
            language=Language.PYTHON,
            content=encoded,
            overwrite=True,
        )
        console.print(f"[dim]Serverless probe script uploaded to {WORKSPACE_SERVERLESS_PROBE_PATH}[/dim]")

    def _parse_logs(self, logs: str) -> dict[str, str]:
        """Parse PASS:key / FAIL:key:msg lines from task stdout into a key → status map."""
        results: dict[str, str] = {}
        for line in (logs or "").splitlines():
            line = line.strip()
            if line.startswith("PASS:"):
                key = line.split(":")[1]
                results[key] = "PASS"
            elif line.startswith("FAIL:"):
                parts = line.split(":", 2)
                key = parts[1]
                msg = parts[2] if len(parts) > 2 else "unknown error"
                results[key] = f"FAIL: {msg}"
        return results

    def check_serverless_probe(self) -> list[TestResult]:
        client = self._get_client()
        if not client:
            return _skipped("No Databricks client")

        try:
            self._ensure_probe_script(client)
        except Exception as e:
            return _skipped(f"Workspace upload failed: {e}")

        console.print("[dim]Submitting serverless job — no cluster start needed...[/dim]")

        from databricks.sdk.service import compute, jobs
        try:
            submitted = client.jobs.submit(
                run_name="neo4j-serverless-connectivity-test",
                tasks=[jobs.SubmitTask(
                    task_key="serverless_probe",
                    environment_key="serverless-env",
                    spark_python_task=jobs.SparkPythonTask(
                        python_file=f"/Workspace{WORKSPACE_SERVERLESS_PROBE_PATH}",
                        parameters=[self.domain_name, self.bolt_uri, self.username, self.password],
                    ),
                    # No new_cluster here — omitting it routes to serverless compute
                )],
                environments=[
                    jobs.JobEnvironment(
                        environment_key="serverless-env",
                        spec=compute.Environment(dependencies=["neo4j"]),
                    )
                ],
            )
            run_id = submitted.run_id
            console.print(f"[dim]Job submitted run_id={run_id}, polling...[/dim]")
        except Exception as e:
            return _skipped(f"Job submission failed: {e}")

        from databricks.sdk.service.jobs import RunLifeCycleState, RunResultState
        deadline = time.time() + 600
        results: list[TestResult] = []

        while time.time() < deadline:
            try:
                run = client.jobs.get_run(run_id=run_id)
                lc = run.state.life_cycle_state if run.state else None
                console.print(f"[dim]  state: {lc}[/dim]")
                if lc in (
                    RunLifeCycleState.TERMINATED,
                    RunLifeCycleState.SKIPPED,
                    RunLifeCycleState.INTERNAL_ERROR,
                ):
                    result_state = run.state.result_state if run.state else None
                    task_run_id = run_id
                    if run.tasks:
                        for t in run.tasks:
                            if t.task_key == "serverless_probe" and t.run_id:
                                task_run_id = t.run_id
                                break
                    logs = ""
                    try:
                        output = client.jobs.get_run_output(run_id=task_run_id)
                        logs = output.logs or ""
                    except Exception:
                        pass

                    port_results = self._parse_logs(logs)
                    for key, label in _CHECK_KEYS:
                        if key in port_results:
                            passed = port_results[key] == "PASS"
                            detail = port_results[key]
                        else:
                            passed = result_state == RunResultState.SUCCESS
                            state_msg = (run.state.state_message or "")[:60] if run.state else ""
                            detail = f"result={result_state}" + (f" {state_msg}" if state_msg else "")
                        results.append(TestResult(label, passed, detail))
                    break
            except Exception as e:
                console.print(f"[dim]  poll error: {e}[/dim]")
            time.sleep(20)
        else:
            results = _skipped("Job timed out after 10 minutes")

        return results

    def run(self) -> list[TestResult]:
        workspace_result = self.check_workspace_reachable()
        if not workspace_result.passed:
            return [workspace_result, *_skipped("Skipped — workspace unreachable")]
        return [workspace_result, *self.check_serverless_probe()]
