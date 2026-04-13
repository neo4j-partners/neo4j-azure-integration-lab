"""
Deployment orchestration and parameter file generation.

Handles:
- Loading base Bicep template parameters
- Applying scenario-specific overrides
- Injecting dynamic values (passwords)
- Parameter validation
- Generating timestamped parameter files
"""

import uuid
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

from .constants import PARAMS_DIR
from .models import Engine, Settings, TestScenario
from .password import PasswordManager
from .utils import find_deployment_file
from .utils import (
    get_timestamp,
    load_json,
    save_json,
)

console = Console()


class DeploymentEngine:
    """Orchestrates Bicep template deployments."""

    def __init__(
        self,
        settings: Settings,
        base_template_dir: Optional[Path] = None,
        deployment_type: str = "vm",
    ) -> None:
        """
        Initialize the deployment engine.

        Args:
            settings: Application settings
            base_template_dir: Path to template directory (defaults to parent of deployments/)
            deployment_type: Type of deployment ("vm")
        """
        self.settings = settings
        self.deployment_type = deployment_type
        self.password_manager = PasswordManager(settings)

        # Default to infra directory (where main.bicep is located)
        if base_template_dir is None:
            base_template_dir = Path(__file__).parent.parent.parent.resolve() / "infra"

        self.base_template_dir = base_template_dir

        # Template files
        if deployment_type == "databricks-peering":
            self.template_file = base_template_dir / "databricks-main.bicep"
        else:
            self.template_file = base_template_dir / "main.bicep"
        self.base_params_file = base_template_dir / "parameters.json"
        self.is_bicep = True

        # Verify template exists
        if not self.template_file.exists():
            raise FileNotFoundError(
                f"Bicep template not found: {self.template_file}\n"
                f"Ensure you're running from the deployments/ directory"
            )

        console.print(f"[dim]Using Bicep template: {self.template_file} ({deployment_type} deployment)[/dim]")

        if not self.base_params_file.exists():
            console.print(f"[dim]No base parameters file found at {self.base_params_file}, using defaults[/dim]")

    def generate_parameter_file(
        self,
        scenario: TestScenario,
        region: Optional[str] = None,
        debug_mode: bool = False,
    ) -> Path:
        """
        Generate Bicep template parameter file for a scenario.

        Args:
            scenario: Test scenario configuration
            region: Override region (uses default from settings if None)
            debug_mode: Enable debug mode with verbose logging

        Returns:
            Path to generated parameter file

        Raises:
            ValueError: If parameters are invalid
        """
        console.print(
            f"[cyan]Generating parameters for scenario: {scenario.name}[/cyan]"
        )

        if scenario.deployment_type.value == "databricks-peering":
            self.template_file = self.base_template_dir / "databricks-main.bicep"
            return self._generate_databricks_parameters(scenario, region or self.settings.default_region)

        # Load base parameters or create default structure
        base_params = self._load_base_parameters()

        # Get password
        password = self.password_manager.get_password(scenario.name)

        # Apply scenario overrides
        params = self._apply_scenario_overrides(
            base_params, scenario, region or self.settings.default_region
        )

        # Inject dynamic values
        params = self._inject_dynamic_values(params, password)

        # Validate parameters
        self._validate_parameters(params, scenario)

        # Generate timestamped file
        param_file_path = self._save_parameter_file(params, scenario)

        console.print(f"[green]Parameters saved to: {param_file_path}[/green]")
        return param_file_path

    def _generate_databricks_parameters(self, scenario: TestScenario, region: str) -> Path:
        """Generate parameter file for the Databricks peering deployment."""
        import json

        # Load source scenario's saved deployment JSON
        deployments_dir = Path(__file__).parent.parent.parent / ".deployments"
        source_file = find_deployment_file(scenario.source_scenario, deployments_dir, Engine.bicep)
        if not source_file:
            raise FileNotFoundError(
                f"Source bicep deployment not found: {scenario.source_scenario}-bicep.json"
                f" in {deployments_dir}\n"
                f"Deploy '{scenario.source_scenario}' first: bicep-deploy deploy --scenario {scenario.source_scenario}"
            )

        with open(source_file) as f:
            source = json.load(f)

        vnet_id = source.get("network", {}).get("vnet_id", "")
        nsg_id = source.get("network", {}).get("nsg_id", "")
        neo4j_rg = source.get("resource_group", "")

        if not vnet_id or not nsg_id or not neo4j_rg:
            raise ValueError(
                f"Source deployment '{scenario.source_scenario}' is missing network info. "
                f"Re-deploy it to populate vnet_id and nsg_id."
            )

        nsg_name = nsg_id.split("/")[-1]
        dbx_rg = f"{neo4j_rg}-dbx"

        params = {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {
                "location": {"value": region},
                "neo4jResourceGroup": {"value": neo4j_rg},
                "neo4jVnetId": {"value": vnet_id},
                "neo4jNsgName": {"value": nsg_name},
                "databricksResourceGroup": {"value": dbx_rg},
                "databricksWorkspaceName": {"value": scenario.databricks_workspace_name},
                "databricksVnetCidr": {"value": scenario.databricks_vnet_cidr},
                "sshSourceCidr": {"value": scenario.ssh_source_cidr},
            },
        }

        timestamp = get_timestamp()
        filename = f"params-{scenario.name}-{timestamp}.json"
        file_path = PARAMS_DIR / filename
        save_json(params, file_path)

        console.print(f"[green]Databricks parameters saved to: {file_path}[/green]")
        console.print(f"[dim]  Neo4j RG: {neo4j_rg}[/dim]")
        console.print(f"[dim]  VNet ID: {vnet_id}[/dim]")
        console.print(f"[dim]  NSG name: {nsg_name}[/dim]")
        console.print(f"[dim]  Databricks RG: {dbx_rg}[/dim]")
        return file_path

    def _load_base_parameters(self) -> dict[str, Any]:
        """
        Load base parameters from template directory or create defaults.

        Returns:
            Base parameters dictionary
        """
        if self.base_params_file.exists():
            params = load_json(self.base_params_file)

            # ARM parameter files have a specific structure with "$schema" and "parameters"
            if "parameters" in params:
                return params
            else:
                # Old format or invalid - wrap it
                return {
                    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                    "contentVersion": "1.0.0.0",
                    "parameters": params
                }
        else:
            # Create default structure
            return {
                "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                "contentVersion": "1.0.0.0",
                "parameters": {}
            }

    def _apply_scenario_overrides(
        self,
        base_params: dict[str, Any],
        scenario: TestScenario,
        region: str,
    ) -> dict[str, Any]:
        """
        Apply scenario-specific parameter overrides.

        Args:
            base_params: Base parameters from template
            scenario: Scenario configuration
            region: Target Azure region

        Returns:
            Parameters with scenario overrides applied
        """
        params = base_params.copy()

        # Ensure parameters structure exists
        if "parameters" not in params:
            params["parameters"] = {}

        p = params["parameters"]

        # Helper to set parameter value
        def set_param(key: str, value: Any) -> None:
            if key not in p:
                p[key] = {}
            p[key]["value"] = value

        # Common parameters for Enterprise VM deployments
        set_param("nodeCount", scenario.node_count)
        set_param("graphDatabaseVersion", scenario.graph_database_version)
        set_param("licenseType", scenario.license_type)

        # Common parameters
        set_param("location", region)
        set_param("diskSize", scenario.disk_size)

        # VM-specific parameters
        set_param("vmSize", scenario.vm_size)

        return params

    def _inject_dynamic_values(
        self,
        params: dict[str, Any],
        password: str,
    ) -> dict[str, Any]:
        """
        Inject dynamic values into parameters.

        Args:
            params: Parameters dictionary
            password: Admin password

        Returns:
            Parameters with dynamic values injected
        """
        p = params["parameters"]

        def set_param(key: str, value: Any) -> None:
            if key not in p:
                p[key] = {}
            p[key]["value"] = value

        # Bicep templates use embedded cloud-init (no external scripts)
        node_count = p.get("nodeCount", {}).get("value", 1)
        if node_count == 1:
            console.print("[dim]Standalone Bicep deployment - using embedded cloud-init[/dim]")
        else:
            console.print("[dim]Cluster Bicep deployment - using cloud-init[/dim]")

        # Pass admin password securely
        set_param("adminPassword", password)

        # Inject OIDC configuration if M2M is enabled
        oidc_config = self._generate_oidc_config()
        set_param("oidcConfig", oidc_config)
        if oidc_config != "none":
            console.print("[cyan]M2M OIDC authentication will be configured[/cyan]")

        return params

    def _generate_oidc_config(self) -> str:
        """
        Generate OIDC configuration string for cloud-init.

        Returns:
            OIDC configuration string or "none" if not enabled
        """
        if not self.settings.m2m or not self.settings.m2m.enabled:
            return "none"

        m2m = self.settings.m2m

        if m2m.provider_type == "keycloak":
            if not m2m.discovery_uri or not m2m.audience or not m2m.role_mapping:
                console.print("[yellow]Keycloak M2M enabled but missing discovery_uri, audience, or role_mapping, skipping OIDC config[/yellow]")
                return "none"

            visible = "true" if m2m.oidc_visible else "false"
            config_lines = [
                "",
                f"# M2M OIDC Authentication ({m2m.display_name})",
                "# Auto-configured by bicep-deploy",
                "dbms.security.authentication_providers=oidc-m2m,native",
                "dbms.security.authorization_providers=oidc-m2m,native",
                f"dbms.security.oidc.m2m.visible={visible}",
                f"dbms.security.oidc.m2m.display_name={m2m.display_name}",
                f"dbms.security.oidc.m2m.well_known_discovery_uri={m2m.discovery_uri}",
                f"dbms.security.oidc.m2m.audience={m2m.audience}",
                f"dbms.security.oidc.m2m.claims.username={m2m.username_claim}",
                f"dbms.security.oidc.m2m.claims.groups={m2m.groups_claim}",
                f"dbms.security.oidc.m2m.config={m2m.token_type_config}",
                f"dbms.security.oidc.m2m.authorization.group_to_role_mapping={m2m.role_mapping}",
            ]

            return "\\n".join(config_lines)

        # Entra ID path
        if not m2m.tenant_id or not m2m.audience:
            console.print("[yellow]M2M enabled but missing tenant_id or audience, skipping OIDC config[/yellow]")
            return "none"

        # Generate the OIDC configuration block (escaped for shell)
        # IMPORTANT: Use v1.0 discovery endpoint because Azure AD client credentials
        # flow returns v1.0 tokens by default (issuer: https://sts.windows.net/{tenant}/)
        config_lines = [
            "",
            "# M2M OIDC Authentication (Microsoft Entra ID)",
            "# Auto-configured by bicep-deploy",
            "dbms.security.authentication_providers=oidc-m2m,native",
            "dbms.security.authorization_providers=oidc-m2m,native",
            "dbms.security.oidc.m2m.visible=false",
            "dbms.security.oidc.m2m.display_name=Entra ID M2M",
            f"dbms.security.oidc.m2m.well_known_discovery_uri=https://login.microsoftonline.com/{m2m.tenant_id}/.well-known/openid-configuration",
            f"dbms.security.oidc.m2m.audience={m2m.audience}",
            "dbms.security.oidc.m2m.claims.username=sub",
            "dbms.security.oidc.m2m.claims.groups=roles",
            "dbms.security.oidc.m2m.config=token_type_principal=access_token;token_type_authentication=access_token",
            'dbms.security.oidc.m2m.authorization.group_to_role_mapping="Neo4j.Admin"=admin;"Neo4j.ReadWrite"=editor;"Neo4j.ReadOnly"=reader',
        ]

        return "\\n".join(config_lines)

    def _validate_parameters(
        self,
        params: dict[str, Any],
        scenario: TestScenario,
    ) -> None:
        """
        Validate parameter combinations.

        Args:
            params: Generated parameters
            scenario: Scenario configuration

        Raises:
            ValueError: If parameters are invalid
        """
        p = params["parameters"]

        # Node count validation (already handled by Pydantic, but double-check)
        if scenario.node_count == 1:
            console.print("[dim]Deploying standalone instance (1 node)[/dim]")
        elif scenario.node_count >= 3:
            console.print(
                f"[dim]Deploying cluster with {scenario.node_count} nodes[/dim]"
            )
        else:
            raise ValueError(
                f"Invalid node count: {scenario.node_count}. "
                f"Must be 1 (standalone) or 3-10 (cluster)"
            )

        # Check required parameters are present
        required = ["location", "adminPassword"]
        for key in required:
            if key not in p or not p[key].get("value"):
                raise ValueError(f"Required parameter '{key}' is missing or empty")

    def _save_parameter_file(
        self,
        params: dict[str, Any],
        scenario: TestScenario,
    ) -> Path:
        """
        Save parameters to timestamped file.

        Args:
            params: Parameters to save
            scenario: Scenario being deployed

        Returns:
            Path to saved parameter file
        """
        timestamp = get_timestamp()
        filename = f"params-{scenario.name}-{timestamp}.json"
        file_path = PARAMS_DIR / filename

        save_json(params, file_path)

        return file_path


class DeploymentPlanner:
    """Plans and validates deployments before execution."""

    def __init__(self, settings_or_prefix) -> None:
        """
        Initialize the deployment planner.

        Args:
            settings_or_prefix: Settings object or resource group prefix string
        """
        if isinstance(settings_or_prefix, str):
            self.resource_group_prefix = settings_or_prefix
        else:
            self.resource_group_prefix = settings_or_prefix.resource_group_prefix

    def generate_deployment_id(self) -> str:
        """
        Generate unique deployment ID.

        Returns:
            UUID string for deployment tracking
        """
        return str(uuid.uuid4())

    def generate_resource_group_name(
        self,
        scenario_name: str,
        timestamp: Optional[str] = None,
    ) -> str:
        """
        Generate resource group name following naming convention.

        Args:
            scenario_name: Name of the scenario
            timestamp: Optional timestamp (generates new one if None)

        Returns:
            Resource group name following pattern: {prefix}-{scenario}-{timestamp}

        Example:
            neo4j-test-standalone-v5-20250116-143052
        """
        if not timestamp:
            timestamp = get_timestamp()

        # Sanitize scenario name (replace underscores with hyphens, remove special chars)
        safe_scenario = scenario_name.replace("_", "-").replace(".", "")

        rg_name = f"{self.resource_group_prefix}-{safe_scenario}-{timestamp}"

        # Azure resource group names: max 90 chars, alphanumeric and hyphens
        if len(rg_name) > 90:
            # Truncate timestamp to make it fit
            max_prefix_scenario = 90 - 16  # Leave room for -YYYYMMDD-HHMMSS
            prefix_scenario = f"{self.resource_group_prefix}-{safe_scenario}"
            if len(prefix_scenario) > max_prefix_scenario:
                prefix_scenario = prefix_scenario[:max_prefix_scenario]
            rg_name = f"{prefix_scenario}-{timestamp}"

        return rg_name

    def generate_deployment_name(
        self,
        scenario_name: str,
        timestamp: Optional[str] = None,
    ) -> str:
        """
        Generate deployment name following naming convention.

        Args:
            scenario_name: Name of the scenario
            timestamp: Optional timestamp (generates new one if None)

        Returns:
            Deployment name following pattern: bicep-deploy-{scenario}-{timestamp}

        Example:
            bicep-deploy-standalone-v5-20250116-143052
        """
        if not timestamp:
            timestamp = get_timestamp()

        safe_scenario = scenario_name.replace("_", "-").replace(".", "")
        deploy_name = f"bicep-deploy-{safe_scenario}-{timestamp}"

        # Azure deployment names: max 64 chars
        if len(deploy_name) > 64:
            max_scenario = 64 - 13 - 16  # "bicep-deploy-" + timestamp
            if len(safe_scenario) > max_scenario:
                safe_scenario = safe_scenario[:max_scenario]
            deploy_name = f"bicep-deploy-{safe_scenario}-{timestamp}"

        return deploy_name
