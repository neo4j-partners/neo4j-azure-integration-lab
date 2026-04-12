"""
Shared base class for Databricks connectivity checkers.

Provides AAD token acquisition, workspace client creation, workspace
reachability check, log parsing, job polling, and result conversion.
DatabricksChecker and ServerlessDatabricksChecker both inherit from this.
"""

import time
from typing import Any

from rich.console import Console

from .base import DATABRICKS_RESOURCE_ID, TestResult, _az

console = Console()


class DatabricksCheckerBase:
    def __init__(self, workspace_host: str, username: str = "neo4j", password: str = "") -> None:
        self.workspace_host = workspace_host
        self.username = username
        self.password = password
        self._client = None

    def _get_aad_token(self) -> str | None:
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

    def _poll_job(
        self,
        client: Any,
        run_id: int,
        task_key: str,
        timeout_seconds: int,
    ) -> tuple[bool, Any, str, str]:
        """
        Poll a Databricks job run until it reaches a terminal state.

        Returns (timed_out, result_state, logs, state_message).
        timed_out=True when the deadline is exceeded before a terminal state.
        """
        from databricks.sdk.service.jobs import RunLifeCycleState
        deadline = time.time() + timeout_seconds
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
                    state_message = (run.state.state_message or "")[:60] if run.state else ""
                    task_run_id = run_id
                    if run.tasks:
                        for t in run.tasks:
                            if t.task_key == task_key and t.run_id:
                                task_run_id = t.run_id
                                break
                    logs = ""
                    try:
                        output = client.jobs.get_run_output(run_id=task_run_id)
                        logs = output.logs or ""
                    except Exception:
                        pass
                    return False, result_state, logs, state_message
            except Exception as e:
                console.print(f"[dim]  poll error: {e}[/dim]")
            time.sleep(20)
        return True, None, "", ""

    def _results_from_logs(
        self,
        port_results: dict[str, str],
        result_state: Any,
        state_message: str,
        check_keys: list[tuple[str, str]],
    ) -> list[TestResult]:
        """Convert parsed probe logs into a TestResult list."""
        from databricks.sdk.service.jobs import RunResultState
        results = []
        for key, label in check_keys:
            if key in port_results:
                passed = port_results[key] == "PASS"
                detail = port_results[key]
            else:
                passed = result_state == RunResultState.SUCCESS
                detail = f"result={result_state}" + (f" {state_message}" if state_message else "")
            results.append(TestResult(label, passed, detail))
        return results
