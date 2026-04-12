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

from rich.console import Console

from .base import TestResult
from .databricks_base import DatabricksCheckerBase

console = Console()

# Uploaded once by setup-databricks and reused by every test run.
DBFS_PROBE_PATH = "dbfs:/neo4j/neo4j_classic_probe.py"

# Probe script lives on disk — avoids duplicating content in this file
# and prevents divergence with the copy uploaded by setup-databricks.
_CLASSIC_PROBE_PATH = Path(__file__).parent.parent.parent / "notebooks" / "neo4j_classic_probe.py"

# Timeout / retry configuration.
_POLL_TIMEOUT_SECONDS = 900       # 15 minutes for classic cluster cold-start
_BOLT_RETRY_WAIT_SECONDS = 120    # 2-minute wait before retrying when TCP passes but Bolt fails

# Ordered check keys matching the probe script output.
_CHECK_KEYS = [
    ("7687",     "Cross-VNet TCP 7687 (from Databricks)"),
    ("7474",     "Cross-VNet TCP 7474 (from Databricks)"),
    ("BOLT",     "Bolt driver (from Databricks classic)"),
    ("TOPOLOGY", "Cluster topology (from Databricks classic)"),
]


def _skipped(reason: str) -> list[TestResult]:
    return [TestResult(label, False, reason) for _, label in _CHECK_KEYS]


class DatabricksChecker(DatabricksCheckerBase):
    def __init__(self, lb_ip: str, workspace_host: str, username: str = "neo4j", password: str = "") -> None:
        super().__init__(workspace_host, username, password)
        self.lb_ip = lb_ip

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

    def _submit_classic_job(self, client, spark_version: str, node_type: str) -> int | None:
        """Submit the classic probe job. Returns run_id or None on failure."""
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
            return run_id
        except Exception as e:
            console.print(f"[dim]Job submission failed: {e}[/dim]")
            return None

    def _run_probe(self, client, spark_version: str, node_type: str) -> tuple[list[TestResult], dict[str, str]]:
        """Submit, poll, and parse one probe run. Returns (results, port_results)."""
        run_id = self._submit_classic_job(client, spark_version, node_type)
        if run_id is None:
            return _skipped("Job submission failed"), {}

        timed_out, result_state, logs, state_message = self._poll_job(
            client, run_id, "tcp_probe", timeout_seconds=_POLL_TIMEOUT_SECONDS
        )
        if timed_out:
            return _skipped(f"Job timed out after {_POLL_TIMEOUT_SECONDS // 60} minutes"), {}

        port_results = self._parse_logs(logs)
        results = self._results_from_logs(port_results, result_state, state_message, _CHECK_KEYS)
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

        results, port_results = self._run_probe(client, spark_version, node_type)

        tcp_passed = port_results.get("7687") == "PASS" and port_results.get("7474") == "PASS"
        bolt_failed = port_results.get("BOLT", "").startswith("FAIL")
        if tcp_passed and bolt_failed:
            console.print(
                f"[yellow]Bolt failed but TCP passed — Neo4j db likely not yet allocated on "
                f"LB-selected node. Retrying in {_BOLT_RETRY_WAIT_SECONDS // 60} minutes...[/yellow]"
            )
            time.sleep(_BOLT_RETRY_WAIT_SECONDS)
            console.print(
                f"[dim]Retry: submitting job (spark={spark_version} node={node_type})"
                f" — cluster start takes ~3-5 min...[/dim]"
            )
            results, _ = self._run_probe(client, spark_version, node_type)

        return results

    def run(self) -> list[TestResult]:
        workspace_result = self.check_workspace_reachable()
        if not workspace_result.passed:
            return [workspace_result, *_skipped("Skipped — workspace unreachable")]
        return [workspace_result, *self.check_cross_vnet_tcp()]
