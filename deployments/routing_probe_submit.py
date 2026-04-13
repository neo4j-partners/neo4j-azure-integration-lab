#!/usr/bin/env python3
"""
Standalone routing probe — tests bolt:// vs neo4j:// over the Private Link path.

Submits notebooks/neo4j_routing_probe.py as a serverless Databricks job using
the same pattern as ServerlessDatabricksChecker. Reads credentials from the
deployment JSON so no manual input is required.

Run from the deployments/ directory:
    uv run python routing_probe_submit.py

Outputs PASS/FAIL lines for both connection schemes. Capture the FAIL:ROUTING
line — it identifies which address the driver failed to reach.
"""

import base64
import json
import subprocess
import sys
import time
from pathlib import Path

SCENARIO = "peer-databricks-v2025"
DEPLOYMENTS_DIR = Path(__file__).parent.parent / ".deployments"
PROBE_SCRIPT = Path(__file__).parent.parent / "notebooks" / "neo4j_routing_probe.py"
WORKSPACE_PROBE_PATH = "/Shared/neo4j-routing-probe.py"
DATABRICKS_RESOURCE_ID = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
POLL_INTERVAL_SECONDS = 20
POLL_TIMEOUT_SECONDS = 600


def _get_aad_token() -> str:
    result = subprocess.run(
        ["az", "account", "get-access-token",
         "--resource", DATABRICKS_RESOURCE_ID,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True,
    )
    token = result.stdout.strip()
    if not token:
        print("ERROR: Failed to acquire AAD token. Run 'az login' first.")
        sys.exit(1)
    return token


def main() -> None:
    deploy_file = DEPLOYMENTS_DIR / f"{SCENARIO}-bicep.json"
    if not deploy_file.exists():
        print(f"ERROR: Deployment file not found: {deploy_file}")
        sys.exit(1)

    data = json.loads(deploy_file.read_text())
    conn = data["connection"]
    workspace_host = conn["databricks_workspace_host"]
    domain_name = data["serverless"]["domain_name"]
    bolt_uri = data["serverless"]["bolt_uri"]
    username = conn.get("username", "neo4j")
    password = conn["password"]

    token = _get_aad_token()

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service import compute, jobs
    from databricks.sdk.service.workspace import ImportFormat

    client = WorkspaceClient(host=workspace_host, token=token)

    # Upload routing probe to workspace (same pattern as ServerlessDatabricksChecker)
    if not PROBE_SCRIPT.exists():
        print(f"ERROR: Probe script not found: {PROBE_SCRIPT}")
        print("Create notebooks/neo4j_routing_probe.py first (see test plan Phase 8).")
        sys.exit(1)

    # Delete any existing object first — overwrite=True fails when the existing
    # object type (e.g. NOTEBOOK) differs from the type being uploaded (FILE).
    try:
        client.workspace.delete(path=WORKSPACE_PROBE_PATH, recursive=False)
    except Exception:
        pass
    encoded = base64.b64encode(PROBE_SCRIPT.read_bytes()).decode()
    client.workspace.import_(
        path=WORKSPACE_PROBE_PATH,
        format=ImportFormat.AUTO,
        content=encoded,
        overwrite=True,
    )
    print(f"Uploaded probe to {WORKSPACE_PROBE_PATH}")

    # Submit serverless job — no new_cluster omits cluster spec → routes to serverless compute
    submitted = client.jobs.submit(
        run_name="neo4j-routing-probe",
        tasks=[jobs.SubmitTask(
            task_key="routing_probe",
            environment_key="env",
            spark_python_task=jobs.SparkPythonTask(
                python_file=f"/Workspace{WORKSPACE_PROBE_PATH}",
                parameters=[domain_name, bolt_uri, username, password],
            ),
        )],
        environments=[jobs.JobEnvironment(
            environment_key="env",
            spec=compute.Environment(dependencies=["neo4j"]),
        )],
    )
    run_id = submitted.run_id
    print(f"Submitted run_id={run_id} — polling every {POLL_INTERVAL_SECONDS}s (timeout {POLL_TIMEOUT_SECONDS // 60}m)...")

    # Poll until terminal state
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        state = client.jobs.get_run(run_id=run_id)
        lc = state.state.life_cycle_state.value
        print(f"  {lc}")
        if lc in ("TERMINATED", "SKIPPED", "INTERNAL_ERROR"):
            # get_run_output requires the task run_id, not the parent run_id
            task_run_id = run_id
            if state.tasks:
                for t in state.tasks:
                    if t.task_key == "routing_probe" and t.run_id:
                        task_run_id = t.run_id
                        break
            try:
                output = client.jobs.get_run_output(run_id=task_run_id)
                print("\n--- probe output ---")
                print(output.logs or "(no output captured)")
                if output.error:
                    print(f"error: {output.error}")
                if output.error_trace:
                    print(f"error_trace: {output.error_trace}")
            except Exception as e:
                print(f"\n--- could not retrieve output: {e} ---")
            return
        time.sleep(POLL_INTERVAL_SECONDS)

    print(f"ERROR: Job timed out after {POLL_TIMEOUT_SECONDS // 60} minutes. run_id={run_id}")
    sys.exit(1)


if __name__ == "__main__":
    main()
