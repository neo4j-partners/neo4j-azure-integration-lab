"""
Databricks cross-VNet connectivity checker.

Authenticates to the Databricks workspace using an AAD token from the
logged-in Azure CLI session (no PAT required), then submits a SparkPythonTask
job that TCP- and Bolt-tests the Neo4j LB from inside the Databricks container
subnet. This is the only test that proves the full cross-VNet path.
"""

import base64
import time
from pathlib import Path
from typing import Optional

from rich.console import Console

from .base import DATABRICKS_RESOURCE_ID, TestResult, _az

console = Console()

# Uploaded once by setup-databricks and reused by every test run.
DBFS_PROBE_PATH = "dbfs:/neo4j/neo4j_classic_probe.py"

# Probe script lives on disk — avoids duplicating content in this file
# and prevents divergence with the copy uploaded by setup-databricks.
_CLASSIC_PROBE_PATH = Path(__file__).parent.parent.parent / "notebooks" / "neo4j_classic_probe.py"

# Ordered check keys matching the probe script output.
_CHECK_KEYS = [
    ("7687",     "Cross-VNet TCP 7687 (from Databricks)"),
    ("7474",     "Cross-VNet TCP 7474 (from Databricks)"),
    ("BOLT",     "Bolt driver (from Databricks classic)"),
    ("TOPOLOGY", "Cluster topology (from Databricks classic)"),
]


def _skipped(reason: str) -> list[TestResult]:
    return [TestResult(label, False, reason) for _, label in _CHECK_KEYS]


class DatabricksChecker:
    def __init__(self, lb_ip: str, workspace_host: str, username: str = "neo4j", password: str = "") -> None:
        self.lb_ip = lb_ip
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
        """Upload the classic probe script to DBFS. Overwrites to keep it current."""
        if not _CLASSIC_PROBE_PATH.exists():
            raise FileNotFoundError(
                f"Classic probe script not found: {_CLASSIC_PROBE_PATH}\n"
                "Run from the repo root or ensure notebooks/neo4j_classic_probe.py exists."
            )
        encoded = base64.b64encode(_CLASSIC_PROBE_PATH.read_bytes()).decode()
        client.dbfs.put(path=DBFS_PROBE_PATH, contents=encoded, overwrite=True)
        console.print(f"[dim]Probe script uploaded to {DBFS_PROBE_PATH}[/dim]")

    def _get_cluster_params(self, client) -> tuple[str, str]:
        """Return (spark_version, node_type_id) auto-detected from the workspace."""
        try:
            versions = client.clusters.spark_versions()
            lts = sorted(
                [v for v in (versions.versions or []) if v.key and "LTS" in (v.name or "")],
                key=lambda v: v.key,
            )
            spark_version = lts[-1].key if lts else "15.4.x-scala2.12"
        except Exception:
            spark_version = "15.4.x-scala2.12"

        try:
            ntypes = client.clusters.list_node_types()
            candidates = sorted(
                [
                    n for n in (ntypes.node_types or [])
                    if n.node_type_id and "Standard_DS" in n.node_type_id and not n.is_deprecated
                ],
                key=lambda n: (n.memory_mb or 999999, n.num_cores or 99),
            )
            node_type = candidates[0].node_type_id if candidates else "Standard_DS3_v2"
        except Exception:
            node_type = "Standard_DS3_v2"

        return spark_version, node_type

    def _parse_logs(self, logs: str) -> dict[str, str]:
        """Parse PASS:port / FAIL:port:msg lines from task stdout into a port → status map."""
        results: dict[str, str] = {}
        for line in (logs or "").splitlines():
            line = line.strip()
            if line.startswith("PASS:"):
                port = line.split(":")[1]
                results[port] = "PASS"
            elif line.startswith("FAIL:"):
                parts = line.split(":", 2)
                port = parts[1]
                msg = parts[2] if len(parts) > 2 else "unknown error"
                results[port] = f"FAIL: {msg}"
        return results

    def _run_probe_job(self, client, spark_version: str, node_type: str) -> tuple[list[TestResult], dict[str, str]]:
        """Submit, poll, and parse one probe job run. Returns (results, port_results)."""
        from databricks.sdk.service import compute, jobs
        try:
            submitted = client.jobs.submit(
                run_name="neo4j-p2p-connectivity-test",
                tasks=[jobs.SubmitTask(
                    task_key="tcp_probe",
                    spark_python_task=jobs.SparkPythonTask(
                        python_file=DBFS_PROBE_PATH,
                        parameters=[self.lb_ip, self.username, self.password],
                    ),
                    libraries=[compute.Library(pypi=compute.PythonPyPiLibrary(package="neo4j"))],
                    new_cluster=compute.ClusterSpec(
                        num_workers=0,
                        spark_version=spark_version,
                        node_type_id=node_type,
                        spark_conf={"spark.master": "local[*]"},
                        custom_tags={"purpose": "neo4j-connectivity-test"},
                    ),
                )],
            )
            run_id = submitted.run_id
            console.print(f"[dim]Job submitted run_id={run_id}, polling...[/dim]")
        except Exception as e:
            return _skipped(f"Job submission failed: {e}"), {}

        from databricks.sdk.service.jobs import RunLifeCycleState, RunResultState
        deadline = time.time() + 900
        results: list[TestResult] = []
        port_results: dict[str, str] = {}

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
                            if t.task_key == "tcp_probe" and t.run_id:
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
            results = _skipped("Job timed out after 15 minutes")

        return results, port_results

    def check_cross_vnet_tcp(self) -> list[TestResult]:
        client = self._get_client()
        if not client:
            return _skipped("No Databricks client")

        try:
            self._ensure_probe_script(client)
        except Exception as e:
            return _skipped(f"DBFS upload failed: {e}")

        spark_version, node_type = self._get_cluster_params(client)
        console.print(
            f"[dim]Submitting job (spark={spark_version} node={node_type})"
            f" — cluster start takes ~3-5 min...[/dim]"
        )

        results, port_results = self._run_probe_job(client, spark_version, node_type)

        tcp_passed = port_results.get("7687") == "PASS" and port_results.get("7474") == "PASS"
        bolt_failed = port_results.get("BOLT", "").startswith("FAIL")
        if tcp_passed and bolt_failed:
            console.print(
                "[yellow]Bolt failed but TCP passed — Neo4j db likely not yet allocated on "
                "LB-selected node. Retrying in 2 minutes...[/yellow]"
            )
            time.sleep(120)
            console.print(
                f"[dim]Retry: submitting job (spark={spark_version} node={node_type})"
                f" — cluster start takes ~3-5 min...[/dim]"
            )
            results, _ = self._run_probe_job(client, spark_version, node_type)

        return results

    def run(self) -> list[TestResult]:
        workspace_result = self.check_workspace_reachable()
        if not workspace_result.passed:
            return [workspace_result, *_skipped("Skipped — workspace unreachable")]
        return [workspace_result, *self.check_cross_vnet_tcp()]
