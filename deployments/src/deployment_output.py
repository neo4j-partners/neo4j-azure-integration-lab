"""
Shared models and helpers for saving and displaying deployment connection details.

Both bicep_deploy and ansible_deploy write the same JSON schema to
.deployments/{scenario}-{engine}.json and render it the same way.
"""

import json
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


class ConnectionJSON(BaseModel):
    neo4j_uri: str = ""
    browser_url: str = ""
    username: str = "neo4j"
    password: str = ""
    neo4j_database: str = "neo4j"
    lb_private_ip: Optional[str] = None
    databricks_bolt_uri: Optional[str] = None
    databricks_workspace_url: Optional[str] = None
    databricks_workspace_host: Optional[str] = None
    bloom_url: Optional[str] = None


class SSHJSON(BaseModel):
    hostname: Optional[str] = None
    username: str = "neo4j"
    command: Optional[str] = None


class ConfigurationJSON(BaseModel):
    license_type: str = "Enterprise"
    node_count: int = 1


class NetworkJSON(BaseModel):
    vnet_id: str = ""
    nsg_id: str = ""
    lb_private_ip: str = ""
    private_link_service_id: str = ""
    databricks_vnet_id: Optional[str] = None


class DeploymentJSON(BaseModel):
    scenario: str
    engine: str
    state: str
    deployment_id: Optional[str] = None
    resource_group: str
    neo4j_resource_group: str
    databricks_resource_group: Optional[str] = None
    databricks_managed_resource_group: Optional[str] = None
    created_at: str
    connection: ConnectionJSON = Field(default_factory=ConnectionJSON)
    ssh: SSHJSON = Field(default_factory=SSHJSON)
    configuration: ConfigurationJSON = Field(default_factory=ConfigurationJSON)
    network: NetworkJSON = Field(default_factory=NetworkJSON)
    m2m_auth: Optional[dict[str, Any]] = None
    serverless: Optional[dict[str, Any]] = None


def write_deployment_json(data: DeploymentJSON, file_path: Path) -> None:
    """Serialize a DeploymentJSON model to disk."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w") as f:
        json.dump(data.model_dump(mode="json"), f, indent=2)


def display_connection_info(details: dict[str, Any], scenario_name: str) -> None:
    """
    Render connection details from a saved deployment JSON dict.

    Handles Neo4j-only, Databricks-peering, and partial-state deployments
    uniformly regardless of which engine produced the JSON.
    """
    conn = details.get("connection", {})
    ssh = details.get("ssh", {})
    cfg = details.get("configuration", {})
    is_partial = details.get("state") == "partial"

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Label", style="cyan")
    table.add_column("Value", style="white")

    if is_partial:
        table.add_row("State", "[yellow]partial — Databricks deployment did not complete[/yellow]")
        table.add_row("Neo4j RG", details.get("neo4j_resource_group", ""))
        table.add_row("Databricks RG", details.get("databricks_resource_group", ""))
    else:
        if conn.get("browser_url"):
            table.add_row("Browser URL", conn["browser_url"])
        if conn.get("neo4j_uri"):
            table.add_row("Neo4j URI", conn["neo4j_uri"])
        table.add_row("Username", conn.get("username", "neo4j"))
        table.add_row("Password", conn.get("password", ""))

        if conn.get("bloom_url"):
            table.add_row("Bloom URL", conn["bloom_url"])
        if conn.get("lb_private_ip"):
            table.add_row("LB Private IP", conn["lb_private_ip"])
        if conn.get("databricks_bolt_uri"):
            table.add_row("Databricks Bolt URI", conn["databricks_bolt_uri"])
        if (details.get("serverless") or {}).get("bolt_uri"):
            table.add_row("Serverless Bolt URI", details["serverless"]["bolt_uri"])
        if conn.get("databricks_workspace_url"):
            table.add_row("Databricks URL", conn["databricks_workspace_url"])
        if ssh.get("command"):
            table.add_row("SSH Command", ssh["command"])

        if cfg.get("license_type"):
            table.add_row("License", cfg["license_type"])
        if cfg.get("node_count", 1) > 1:
            table.add_row("Cluster Size", f"{cfg['node_count']} nodes")

    console.print(Panel(
        table,
        title=f"[bold green]{scenario_name} - Connection Details[/bold green]",
        border_style="green",
    ))
