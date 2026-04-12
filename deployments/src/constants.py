"""
Constants and default values for the Neo4j Azure deployment tools.
"""

from pathlib import Path
from typing import Final

# Directory paths (relative to deployments/)
ARM_TESTING_DIR: Final[Path] = Path(".arm-testing")
CONFIG_DIR: Final[Path] = ARM_TESTING_DIR / "config"
STATE_DIR: Final[Path] = ARM_TESTING_DIR / "state"
PARAMS_DIR: Final[Path] = ARM_TESTING_DIR / "params"
RESULTS_DIR: Final[Path] = ARM_TESTING_DIR / "results"
LOGS_DIR: Final[Path] = ARM_TESTING_DIR / "logs"
TEMPLATES_DIR: Final[Path] = ARM_TESTING_DIR / "templates"

# Configuration files
SETTINGS_FILE: Final[Path] = CONFIG_DIR / "settings.yaml"
SCENARIOS_FILE: Final[Path] = CONFIG_DIR / "scenarios.yaml"

# Default Azure regions (commonly used for testing)
DEFAULT_REGIONS: Final[list[str]] = [
    "eastus2",
    "westeurope",
    "northeurope",
    "uksouth",
]

# Resource naming patterns
RESOURCE_GROUP_PREFIX: Final[str] = "neo4j-test"
DEPLOYMENT_PREFIX: Final[str] = "bicep-deploy"

# Default VM sizes for cost-effective testing
DEFAULT_VM_SIZES: Final[dict[str, str]] = {
    "standalone": "Standard_D2s_v5",
    "cluster": "Standard_D2s_v5",
    "performance": "Standard_E4s_v5",
}

# Neo4j versions
NEO4J_VERSIONS: Final[list[str]] = ["2025"]

# License types
LICENSE_TYPES: Final[list[str]] = ["Enterprise", "Evaluation"]

# Cleanup modes
CLEANUP_MODES: Final[list[str]] = ["immediate", "on-success", "manual", "scheduled"]

# Default cleanup mode
DEFAULT_CLEANUP_MODE: Final[str] = "on-success"

# Default deployment timeout (in seconds)
DEFAULT_DEPLOYMENT_TIMEOUT: Final[int] = 1800  # 30 minutes

# Azure resource tags
RESOURCE_TAGS: Final[dict[str, str]] = {
    "purpose": "bicep-deployment",
    "managed-by": "bicep-deploy",
}

# Timestamp format
TIMESTAMP_FORMAT: Final[str] = "%Y%m%d-%H%M%S"

# Password options
PASSWORD_OPTIONS: Final[list[str]] = [
    "generate",
    "environment",
    "prompt",
]

# Neo4j network ports
NEO4J_BOLT_PORT: Final[int] = 7687
NEO4J_HTTP_PORT: Final[int] = 7474
NEO4J_HTTPS_PORT: Final[int] = 7473

# Length of deterministic SHA-1 resource suffix used in Ansible deployments
RESOURCE_SUFFIX_LENGTH: Final[int] = 13

# Azure AD application ID for the Databricks platform service (fixed across all tenants)
DATABRICKS_PLATFORM_APP_ID: Final[str] = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
