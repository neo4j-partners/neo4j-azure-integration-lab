"""
Pydantic models for configuration and state management.
"""

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class CleanupMode(str, Enum):
    """Cleanup modes for resource management."""

    IMMEDIATE = "immediate"
    ON_SUCCESS = "on-success"
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class PasswordStrategy(str, Enum):
    """Password provisioning strategies."""

    GENERATE = "generate"
    ENVIRONMENT = "environment"
    PROMPT = "prompt"


class DeploymentType(str, Enum):
    """Deployment platform type."""

    VM = "vm"
    DATABRICKS_PEERING = "databricks-peering"


class TestScenario(BaseModel):
    """Configuration for a single test scenario."""

    name: str = Field(..., description="Scenario name (e.g., 'standalone-v5')")

    # Deployment platform
    deployment_type: DeploymentType = Field(
        DeploymentType.VM, description="Deployment platform"
    )

    # Common Neo4j settings
    node_count: Literal[1, 3, 4, 5, 6, 7, 8, 9, 10] = Field(
        ..., description="Number of cluster nodes"
    )
    graph_database_version: Literal["2025"] = Field(
        ..., description="Neo4j version (2025.x)"
    )
    disk_size: int = Field(32, ge=32, description="Disk size in GB")
    license_type: Literal["Enterprise", "Evaluation"] = Field(
        "Evaluation", description="License type"
    )

    # VM-specific settings
    vm_size: Optional[str] = Field(None, description="Azure VM size")
    read_replica_count: Literal[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10] = Field(
        0, description="Number of read replicas (4.4 only)"
    )
    read_replica_vm_size: Optional[str] = Field(None, description="VM size for read replicas")
    read_replica_disk_size: int = Field(32, ge=32, description="Disk size for read replicas")

    # Plugin settings
    install_graph_data_science: bool = Field(False, description="Install GDS plugin")
    graph_data_science_license_key: str = Field("None", description="GDS license key")
    install_bloom: bool = Field(False, description="Install Bloom")
    bloom_license_key: str = Field("None", description="Bloom license key")

    # Databricks peering fields (only used when deployment_type is databricks-peering)
    source_scenario: Optional[str] = Field(None, description="Source Neo4j scenario to peer with")
    databricks_workspace_name: str = Field("neo4j-dbx", description="Databricks workspace name")
    databricks_vnet_cidr: str = Field("192.168.0.0/16", description="Databricks VNet CIDR")

    # Network hardening (passed through to both main.bicep and databricks-main.bicep)
    ssh_source_cidr: str = Field("Internet", description="Source CIDR for SSH NSG rule")

    @field_validator("read_replica_count")
    @classmethod
    def validate_read_replicas(cls, v: int, info) -> int:
        """Validate that read replicas are only used with Neo4j 4.4."""
        data = info.data
        if v > 0:
            if data.get("graph_database_version") != "4.4":
                raise ValueError("Read replicas are only supported with Neo4j 4.4")
        return v

    @field_validator("vm_size")
    @classmethod
    def validate_vm_size(cls, v: Optional[str], info) -> Optional[str]:
        """Ensure VM size is set for deployments."""
        if not v:
            return "Standard_D2s_v5"
        return v


class M2MSettings(BaseModel):
    """M2M (Machine-to-Machine) authentication settings."""

    enabled: bool = Field(False, description="Whether M2M auth is enabled")
    provider_type: str = Field("entra", description="Provider type: 'entra' or 'keycloak'")

    # Entra ID fields (used when provider_type is 'entra')
    tenant_id: Optional[str] = Field(None, description="Azure Entra ID tenant ID")
    api_app_id: Optional[str] = Field(None, description="Neo4j API app registration ID")
    api_app_name: Optional[str] = Field(None, description="Neo4j API app display name")
    audience: Optional[str] = Field(None, description="OIDC audience value")
    client_app_id: Optional[str] = Field(None, description="Client app registration ID")
    client_app_name: Optional[str] = Field(None, description="Client app display name")

    # Generic OIDC fields (used when provider_type is 'keycloak')
    discovery_uri: Optional[str] = Field(None, description="OIDC discovery URI")
    token_endpoint: Optional[str] = Field(None, description="Token endpoint for acquiring tokens")
    client_id: Optional[str] = Field(None, description="OIDC client ID")
    client_secret: Optional[str] = Field(None, description="OIDC client secret (demo only)")
    username_claim: str = Field("sub", description="JWT claim for username")
    groups_claim: str = Field("roles", description="JWT claim for group/role mapping")
    role_mapping: Optional[str] = Field(None, description="Role mapping string for Neo4j")
    token_type_config: str = Field(
        "token_type_principal=access_token;token_type_authentication=access_token",
        description="Neo4j token type configuration",
    )
    display_name: str = Field("Keycloak M2M", description="OIDC provider display name")
    oidc_visible: bool = Field(False, description="Whether OIDC provider is visible in Neo4j Browser")


class Settings(BaseModel):
    """Main configuration settings."""

    # Azure settings
    subscription_id: str = Field(..., description="Azure subscription ID")
    subscription_name: str = Field(..., description="Azure subscription name")
    default_region: str = Field("westeurope", description="Default Azure region")

    # Resource naming
    resource_group_prefix: str = Field(
        "neo4j-test", description="Prefix for resource group names"
    )

    # Cleanup settings
    default_cleanup_mode: CleanupMode = Field(
        CleanupMode.ON_SUCCESS, description="Default cleanup behavior"
    )

    # Cost settings
    max_cost_per_deployment: Optional[float] = Field(
        None, description="Maximum estimated cost in USD"
    )

    # Git settings
    auto_detect_branch: bool = Field(
        True, description="Automatically detect Git branch for artifact location"
    )
    repository_org: Optional[str] = Field(None, description="GitHub organization")
    repository_name: Optional[str] = Field(None, description="GitHub repository name")

    # Password settings
    password_strategy: PasswordStrategy = Field(
        PasswordStrategy.GENERATE, description="How to provide admin password"
    )

    # Deployment settings
    deployment_timeout: int = Field(
        1800, description="Deployment timeout in seconds"
    )

    # User info
    owner_email: str = Field(..., description="Owner email for resource tagging")

    # M2M authentication settings
    m2m: Optional[M2MSettings] = Field(
        default_factory=lambda: M2MSettings(),
        description="M2M bearer token authentication settings"
    )


class ScenarioCollection(BaseModel):
    """Collection of test scenarios."""

    scenarios: list[TestScenario] = Field(..., description="List of test scenarios")


class DeploymentState(BaseModel):
    """State tracking for a deployment."""

    deployment_id: str = Field(..., description="Unique deployment identifier")
    resource_group_name: str = Field(..., description="Azure resource group name")
    deployment_name: str = Field(..., description="Azure deployment name")
    scenario_name: str = Field(..., description="Scenario name")
    git_branch: str = Field(..., description="Git branch used")
    parameter_file_path: str = Field(..., description="Path to parameter file")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), description="Creation timestamp"
    )
    expires_at: Optional[datetime] = Field(None, description="Expiration timestamp")
    cleanup_mode: CleanupMode = Field(..., description="Cleanup mode")
    status: Literal["pending", "deploying", "succeeded", "failed", "deleted"] = Field(
        "pending", description="Deployment status"
    )
    subscription_scoped: bool = Field(False, description="Use subscription-scoped deployment")
    databricks_resource_group: Optional[str] = Field(
        None, description="Databricks customer RG for databricks-peering deployments (deleted alongside main RG)"
    )


class ActiveDeployments(BaseModel):
    """Collection of active deployments."""

    deployments: list[DeploymentState] = Field(
        default_factory=list, description="List of active deployments"
    )


class ConnectionInfo(BaseModel):
    """Connection information for a deployed Neo4j instance."""

    deployment_id: str = Field(..., description="Deployment ID")
    scenario_name: str = Field(..., description="Scenario name")
    resource_group: str = Field(..., description="Resource group name")

    # Connection details
    neo4j_uri: str = Field(..., description="Neo4j connection URI (bolt:// for standalone, neo4j:// for cluster)")
    browser_url: str = Field(..., description="Neo4j Browser URL (http://...)")
    bloom_url: Optional[str] = Field(None, description="Bloom URL if installed")

    # SSH access
    ssh_hostname: Optional[str] = Field(None, description="SSH hostname for VM access")
    ssh_username: Optional[str] = Field(None, description="SSH username")
    ssh_command: Optional[str] = Field(None, description="SSH command to connect")

    # Credentials
    username: str = Field(default="neo4j", description="Neo4j username")
    password: str = Field(..., description="Neo4j admin password")

    # License information
    license_type: str = Field(default="Evaluation", description="License type (Evaluation or Enterprise)")

    # Cluster information
    node_count: Optional[int] = Field(None, description="Number of cluster nodes (None for standalone)")

    # Deployment outputs (raw)
    outputs: dict[str, Any] = Field(..., description="Raw deployment outputs")

    # Metadata
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when connection info was extracted"
    )
