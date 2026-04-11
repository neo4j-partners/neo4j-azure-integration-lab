"""
Databricks cross-VNet connectivity checker.

Authenticates to the Databricks workspace using an AAD token from the
logged-in Azure CLI session (no PAT required), then submits a SparkPythonTask
job that TCP-probes the Neo4j LB from inside the Databricks container subnet.
This is the only test that proves the full cross-VNet path.
"""

import base64
import time
from typing import Optional

from rich.console import Console

from .base import DATABRICKS_RESOURCE_ID, TestResult, _az

console = Console()

# Uploaded once by setup-databricks and reused by every test run.
DBFS_PROBE_PATH = "dbfs:/neo4j/neo4j_tcp_probe.py"

# Plain Python script — no dbutils, no Spark, runs as SparkPythonTask.
# Outputs one line per port: PASS:7687 or FAIL:7687:error message
_TCP_TEST_SCRIPT = """\
import socket
import sys

lb_ip = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
for port in [7687, 7474]:
    try:
        s = socket.create_connection((lb_ip, port), timeout=10)
        s.close()
        print(f"PASS:{port}")
    except Exception as e:
        print(f"FAIL:{port}:{e}")
"""


class DatabricksChecker:
    def __init__(self, lb_ip: str, workspace_host: str) -> None:
        self.lb_ip = lb_ip
        self.workspace_host = workspace_host
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
        """Upload the TCP probe script to DBFS. Overwrites to keep it current."""
        encoded = base64.b64encode(_TCP_TEST_SCRIPT.encode()).decode()
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

    def check_cross_vnet_tcp(self) -> list[TestResult]:
        client = self._get_client()
        if not client:
            return [
                TestResult("Cross-VNet TCP 7687 (from Databricks)", False, "No Databricks client"),
                TestResult("Cross-VNet TCP 7474 (from Databricks)", False, "No Databricks client"),
            ]

        try:
            self._ensure_probe_script(client)
        except Exception as e:
            err = f"DBFS upload failed: {e}"
            return [
                TestResult("Cross-VNet TCP 7687 (from Databricks)", False, err),
                TestResult("Cross-VNet TCP 7474 (from Databricks)", False, err),
            ]

        spark_version, node_type = self._get_cluster_params(client)
        console.print(
            f"[dim]Submitting job (spark={spark_version} node={node_type})"
            f" — cluster start takes ~3-5 min...[/dim]"
        )

        from databricks.sdk.service import compute, jobs
        try:
            submitted = client.jobs.submit(
                run_name="neo4j-p2p-connectivity-test",
                tasks=[jobs.SubmitTask(
                    task_key="tcp_probe",
                    spark_python_task=jobs.SparkPythonTask(
                        python_file=DBFS_PROBE_PATH,
                        parameters=[self.lb_ip],
                    ),
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
            err = f"Job submission failed: {e}"
            return [
                TestResult("Cross-VNet TCP 7687 (from Databricks)", False, err),
                TestResult("Cross-VNet TCP 7474 (from Databricks)", False, err),
            ]

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
                    for port in ["7687", "7474"]:
                        if port in port_results:
                            passed = port_results[port] == "PASS"
                            detail = port_results[port]
                        else:
                            passed = result_state == RunResultState.SUCCESS
                            state_msg = (run.state.state_message or "")[:60] if run.state else ""
                            detail = f"result={result_state}" + (f" {state_msg}" if state_msg else "")
                        results.append(TestResult(f"Cross-VNet TCP {port} (from Databricks)", passed, detail))
                    break
            except Exception as e:
                console.print(f"[dim]  poll error: {e}[/dim]")
            time.sleep(20)
        else:
            results = [
                TestResult("Cross-VNet TCP 7687 (from Databricks)", False, "Job timed out after 10 minutes"),
                TestResult("Cross-VNet TCP 7474 (from Databricks)", False, "Job timed out after 10 minutes"),
            ]

        return results

    def run(self) -> list[TestResult]:
        workspace_result = self.check_workspace_reachable()
        if not workspace_result.passed:
            return [
                workspace_result,
                TestResult("Cross-VNet TCP 7687 (from Databricks)", False, "Skipped — workspace unreachable"),
                TestResult("Cross-VNet TCP 7474 (from Databricks)", False, "Skipped — workspace unreachable"),
            ]
        return [workspace_result, *self.check_cross_vnet_tcp()]
