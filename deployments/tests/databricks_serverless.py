"""
Databricks serverless connectivity checker for Neo4j.

Authenticates to the Databricks workspace using an AAD token from the
logged-in Azure CLI session (no PAT required), then submits a SparkPythonTask
job with no cluster spec so Databricks routes it to serverless compute.
A job environment spec injects the neo4j pip package without a runtime
install step. The probe script is auto-uploaded to the workspace before
each run to ensure it stays current.
"""

import base64
import io
from pathlib import Path

from rich.console import Console

from .base import TestResult
from .databricks_base import DatabricksCheckerBase

console = Console()

# Pre-uploaded to the Databricks workspace by setup-databricks and kept
# current by _ensure_probe_script before every test run.
WORKSPACE_SERVERLESS_PROBE_PATH = "/Shared/neo4j-serverless-probe.py"

# Probe script lives on disk alongside the classic probe.
_SERVERLESS_PROBE_PATH = Path(__file__).parent.parent.parent / "notebooks" / "neo4j_serverless_probe.py"

# Timeout configuration.
_POLL_TIMEOUT_SECONDS = 600    # 10 minutes — serverless skips cluster cold-start

# Ordered check keys matching the probe script output.
_CHECK_KEYS = [
    ("DNS",      "DNS resolution (from Databricks serverless)"),
    ("7687",     "TCP 7687 (from Databricks serverless)"),
    ("7474",     "TCP 7474 (from Databricks serverless)"),
    ("BOLT",     "Bolt driver (from Databricks serverless)"),
    ("TOPOLOGY", "Cluster topology (from Databricks serverless)"),
]


def _skipped(reason: str) -> list[TestResult]:
    return [TestResult(label, False, reason) for _, label in _CHECK_KEYS]


class ServerlessDatabricksChecker(DatabricksCheckerBase):
    def __init__(
        self,
        domain_name: str,
        bolt_uri: str,
        workspace_host: str,
        username: str = "neo4j",
        password: str = "",
        serverless_probe_path: str = WORKSPACE_SERVERLESS_PROBE_PATH,
    ) -> None:
        super().__init__(workspace_host, username, password)
        self.domain_name = domain_name
        self.bolt_uri = bolt_uri
        self.serverless_probe_path = serverless_probe_path

    def _ensure_probe_script(self, client) -> None:
        """Upload the serverless probe script to the workspace. Overwrites to keep it current."""
        if not _SERVERLESS_PROBE_PATH.exists():
            raise FileNotFoundError(
                f"Serverless probe script not found: {_SERVERLESS_PROBE_PATH}\n"
                "Run from the repo root or ensure notebooks/neo4j_serverless_probe.py exists."
            )
        # Delete any existing object first — overwrite=True fails when the existing
        # object type (e.g. NOTEBOOK) differs from the type being uploaded (FILE).
        try:
            client.workspace.delete(path=self.serverless_probe_path, recursive=False)
        except Exception:
            pass  # Not found is fine; any other error will surface on the import below
        from databricks.sdk.service.workspace import ImportFormat
        encoded = base64.b64encode(_SERVERLESS_PROBE_PATH.read_bytes()).decode()
        # ImportFormat.AUTO (no language) creates ObjectType.FILE when the file has no
        # "# Databricks notebook source" header. ImportFormat.SOURCE with language=PYTHON
        # always creates ObjectType.NOTEBOOK — SparkPythonTask on serverless cannot open
        # notebook objects as binary and returns ENOTSUP (errno 95). The probe script is
        # a plain Python script with no notebook header, so AUTO produces a FILE.
        client.workspace.import_(
            path=self.serverless_probe_path,
            format=ImportFormat.AUTO,
            content=encoded,
            overwrite=True,
        )
        console.print(f"[dim]Serverless probe script uploaded to {self.serverless_probe_path}[/dim]")

    def _submit_serverless_job(self, client) -> int | None:
        """Submit the serverless probe job. Returns run_id or None on failure."""
        from databricks.sdk.service import compute, jobs
        try:
            submitted = client.jobs.submit(
                run_name="neo4j-serverless-connectivity-test",
                tasks=[jobs.SubmitTask(
                    task_key="serverless_probe",
                    environment_key="serverless-env",
                    spark_python_task=jobs.SparkPythonTask(
                        python_file=f"/Workspace{self.serverless_probe_path}",
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
            return run_id
        except Exception as e:
            console.print(f"[dim]Job submission failed: {e}[/dim]")
            return None

    def check_serverless_probe(self) -> list[TestResult]:
        client = self._get_client()
        if not client:
            return _skipped("No Databricks client")

        try:
            self._ensure_probe_script(client)
        except Exception as e:
            return _skipped(f"Workspace upload failed: {e}")

        console.print("[dim]Submitting serverless job — no cluster start needed...[/dim]")

        run_id = self._submit_serverless_job(client)
        if run_id is None:
            return _skipped("Job submission failed")

        timed_out, result_state, logs, state_message = self._poll_job(
            client, run_id, "serverless_probe", timeout_seconds=_POLL_TIMEOUT_SECONDS
        )
        if timed_out:
            return _skipped(f"Job timed out after {_POLL_TIMEOUT_SECONDS // 60} minutes")

        port_results = self._parse_logs(logs)
        return self._results_from_logs(port_results, result_state, state_message, _CHECK_KEYS)

    def run(self) -> list[TestResult]:
        workspace_result = self.check_workspace_reachable()
        if not workspace_result.passed:
            return [workspace_result, *_skipped("Skipped — workspace unreachable")]
        return [workspace_result, *self.check_serverless_probe()]
