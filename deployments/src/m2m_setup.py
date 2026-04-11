"""
M2M (Machine-to-Machine) authentication setup for Neo4j with Microsoft Entra ID.

This module provides functions to:
1. Auto-detect Azure tenant information
2. Create Entra ID app registrations via Azure CLI
3. Configure app roles for Neo4j access control
4. Generate client secrets
5. Output configuration for neo4j.conf
"""

import json
import subprocess
import uuid
from dataclasses import dataclass
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

console = Console()


@dataclass
class M2MConfig:
    """M2M authentication configuration."""

    enabled: bool = False
    provider_type: str = "entra"  # "entra" or "keycloak"

    # Entra ID fields
    tenant_id: Optional[str] = None
    api_app_id: Optional[str] = None  # Neo4j API app (resource)
    api_app_name: Optional[str] = None
    audience: Optional[str] = None  # e.g., api://neo4j-m2m or neo4j-client
    client_app_id: Optional[str] = None  # Client service app
    client_app_name: Optional[str] = None
    client_secret: Optional[str] = None
    client_secret_expiry: Optional[str] = None

    # Generic OIDC fields (used for Keycloak)
    discovery_uri: Optional[str] = None
    token_endpoint: Optional[str] = None
    client_id: Optional[str] = None
    username_claim: str = "sub"
    groups_claim: str = "roles"
    role_mapping: Optional[str] = None
    token_type_config: str = "token_type_principal=access_token;token_type_authentication=access_token"
    display_name: str = "Keycloak M2M"
    oidc_visible: bool = False


def run_az_command(args: list[str], check: bool = True) -> Optional[str]:
    """
    Run an Azure CLI command and return the output.

    Args:
        args: Command arguments (without 'az' prefix)
        check: Whether to raise on non-zero exit

    Returns:
        Command output or None on failure
    """
    cmd = ["az"] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        if check:
            console.print(f"[red]Azure CLI error: {e.stderr}[/red]")
        return None
    except FileNotFoundError:
        console.print("[red]Azure CLI not found. Please install and run 'az login'.[/red]")
        return None


def get_tenant_id() -> Optional[str]:
    """Get the current Azure tenant ID."""
    output = run_az_command(["account", "show", "--query", "tenantId", "-o", "tsv"], check=False)
    return output if output else None


def get_subscription_info() -> Optional[dict]:
    """Get current Azure subscription information."""
    output = run_az_command(["account", "show", "-o", "json"], check=False)
    if output:
        return json.loads(output)
    return None


def get_current_user_object_id() -> Optional[str]:
    """Get the object ID of the currently signed-in user."""
    output = run_az_command(
        ["ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"],
        check=False,
    )
    return output if output else None


def add_app_owner(app_id: str, user_object_id: str) -> bool:
    """
    Add a user as an owner of an app registration.

    Args:
        app_id: Application (client) ID
        user_object_id: Object ID of the user to add as owner

    Returns:
        True if successful, False otherwise
    """
    result = run_az_command(
        ["ad", "app", "owner", "add", "--id", app_id, "--owner-object-id", user_object_id],
        check=False,
    )
    return result is not None


def check_app_exists(display_name: str) -> Optional[str]:
    """
    Check if an app registration with the given name exists.

    Returns:
        App ID if exists, None otherwise
    """
    output = run_az_command(
        ["ad", "app", "list", "--display-name", display_name, "--query", "[0].appId", "-o", "tsv"],
        check=False,
    )
    return output if output else None


def create_api_app(display_name: str, identifier_uri: str) -> Optional[tuple[str, str, str]]:
    """
    Create the Neo4j API app registration with app roles.

    Args:
        display_name: App display name (e.g., "Neo4j M2M API")
        identifier_uri: API identifier hint (will be converted to api://{app-id} format)

    Returns:
        Tuple of (app_id, object_id, actual_identifier_uri) or None on failure
    """
    # Define app roles for Neo4j access control
    app_roles = [
        {
            "allowedMemberTypes": ["Application"],
            "description": "Full administrative access to Neo4j",
            "displayName": "Neo4j Admin",
            "isEnabled": True,
            "value": "Neo4j.Admin",
            "id": str(uuid.uuid4()),
        },
        {
            "allowedMemberTypes": ["Application"],
            "description": "Read and write access to Neo4j data",
            "displayName": "Neo4j ReadWrite",
            "isEnabled": True,
            "value": "Neo4j.ReadWrite",
            "id": str(uuid.uuid4()),
        },
        {
            "allowedMemberTypes": ["Application"],
            "description": "Read-only access to Neo4j data",
            "displayName": "Neo4j ReadOnly",
            "isEnabled": True,
            "value": "Neo4j.ReadOnly",
            "id": str(uuid.uuid4()),
        },
    ]

    app_roles_json = json.dumps(app_roles)

    # Step 1: Create the app WITHOUT identifier URI first
    # Azure requires identifier URIs to contain app ID, tenant ID, or verified domain
    console.print(f"[cyan]Creating API app registration: {display_name}...[/cyan]")
    output = run_az_command(
        [
            "ad",
            "app",
            "create",
            "--display-name",
            display_name,
            "--app-roles",
            app_roles_json,
            "--query",
            "{appId: appId, id: id}",
            "-o",
            "json",
        ]
    )

    if not output:
        return None

    result = json.loads(output)
    app_id = result.get("appId")
    object_id = result.get("id")

    # Step 2: Set the identifier URI using the app's own ID (required by Azure policy)
    # Format: api://{app-id} is always allowed
    actual_identifier_uri = f"api://{app_id}"
    console.print(f"[cyan]Setting API identifier URI: {actual_identifier_uri}...[/cyan]")

    uri_result = run_az_command(
        [
            "ad",
            "app",
            "update",
            "--id",
            app_id,
            "--identifier-uris",
            actual_identifier_uri,
        ],
        check=False,
    )

    if uri_result is None:
        console.print("[yellow]Warning: Could not set identifier URI, continuing...[/yellow]")

    # Step 3: Set token version to 2 via manifest update
    console.print("[cyan]Configuring token version to v2.0...[/cyan]")
    run_az_command(
        [
            "ad",
            "app",
            "update",
            "--id",
            app_id,
            "--set",
            "api.requestedAccessTokenVersion=2",
        ],
        check=False,
    )

    # Step 4: Create service principal for the app
    console.print("[cyan]Creating service principal...[/cyan]")
    run_az_command(["ad", "sp", "create", "--id", app_id], check=False)

    # Step 5: Add current user as app owner (ensures manage/delete access)
    user_oid = get_current_user_object_id()
    if user_oid:
        if add_app_owner(app_id, user_oid):
            console.print("[green]✓ Added current user as app owner[/green]")
        else:
            console.print("[yellow]Warning: Could not add current user as app owner[/yellow]")

    return app_id, object_id, actual_identifier_uri


def create_client_app(display_name: str) -> Optional[str]:
    """
    Create a client app registration for services.

    Args:
        display_name: App display name (e.g., "MyService-Neo4j-Client")

    Returns:
        App ID or None on failure
    """
    console.print(f"[cyan]Creating client app registration: {display_name}...[/cyan]")
    output = run_az_command(
        [
            "ad",
            "app",
            "create",
            "--display-name",
            display_name,
            "--query",
            "appId",
            "-o",
            "tsv",
        ]
    )

    if not output:
        return None

    # Create service principal
    run_az_command(["ad", "sp", "create", "--id", output], check=False)

    # Add current user as app owner (ensures manage/delete access)
    user_oid = get_current_user_object_id()
    if user_oid:
        if add_app_owner(output, user_oid):
            console.print("[green]✓ Added current user as app owner[/green]")
        else:
            console.print("[yellow]Warning: Could not add current user as app owner[/yellow]")

    return output


def create_client_secret(app_id: str, display_name: str = "Neo4j M2M Secret", years: int = 1) -> Optional[tuple[str, str]]:
    """
    Create a client secret for an app.

    Args:
        app_id: Application ID
        display_name: Secret description
        years: Validity in years

    Returns:
        Tuple of (secret_value, expiry_date) or None on failure
    """
    console.print(f"[cyan]Creating client secret (valid for {years} year(s))...[/cyan]")
    output = run_az_command(
        [
            "ad",
            "app",
            "credential",
            "reset",
            "--id",
            app_id,
            "--display-name",
            display_name,
            "--years",
            str(years),
            "--append",
            "--query",
            "{password: password, endDateTime: endDateTime}",
            "-o",
            "json",
        ]
    )

    if not output:
        return None

    result = json.loads(output)
    return result.get("password"), result.get("endDateTime", "")


def get_app_role_id(api_app_id: str, role_value: str) -> Optional[str]:
    """
    Get the ID of an app role by its value.

    Args:
        api_app_id: The API app's application ID
        role_value: The role value (e.g., "Neo4j.ReadWrite")

    Returns:
        Role ID or None if not found
    """
    output = run_az_command(
        [
            "ad",
            "app",
            "show",
            "--id",
            api_app_id,
            "--query",
            f"appRoles[?value=='{role_value}'].id",
            "-o",
            "tsv",
        ],
        check=False,
    )
    return output if output else None


def grant_api_permission(client_app_id: str, api_app_id: str, role_value: str) -> bool:
    """
    Grant an API permission (app role) to a client app.

    Args:
        client_app_id: Client application ID
        api_app_id: API application ID
        role_value: Role value to grant (e.g., "Neo4j.ReadWrite")

    Returns:
        True if successful, False otherwise
    """
    # Get the role ID
    role_id = get_app_role_id(api_app_id, role_value)
    if not role_id:
        console.print(f"[red]Could not find role '{role_value}' in API app[/red]")
        return False

    # Get API service principal ID
    api_sp_output = run_az_command(
        ["ad", "sp", "show", "--id", api_app_id, "--query", "id", "-o", "tsv"],
        check=False,
    )
    if not api_sp_output:
        console.print("[red]Could not find API service principal[/red]")
        return False

    # Get client service principal ID
    client_sp_output = run_az_command(
        ["ad", "sp", "show", "--id", client_app_id, "--query", "id", "-o", "tsv"],
        check=False,
    )
    if not client_sp_output:
        console.print("[red]Could not find client service principal[/red]")
        return False

    # Grant the app role assignment
    console.print(f"[cyan]Granting '{role_value}' permission...[/cyan]")
    result = run_az_command(
        [
            "rest",
            "--method",
            "POST",
            "--uri",
            f"https://graph.microsoft.com/v1.0/servicePrincipals/{client_sp_output}/appRoleAssignments",
            "--headers",
            "Content-Type=application/json",
            "--body",
            json.dumps(
                {
                    "principalId": client_sp_output,
                    "resourceId": api_sp_output,
                    "appRoleId": role_id,
                }
            ),
        ],
        check=False,
    )

    return result is not None


def generate_neo4j_oidc_config(config: M2MConfig) -> str:
    """
    Generate neo4j.conf OIDC configuration lines.

    Args:
        config: M2M configuration

    Returns:
        Configuration string for neo4j.conf
    """
    if not config.enabled:
        return ""

    if config.provider_type == "keycloak":
        if not config.discovery_uri or not config.audience or not config.role_mapping:
            return ""
        visible = "true" if config.oidc_visible else "false"
        return f"""
# ============================================================
# M2M OIDC Authentication ({config.display_name})
# Auto-generated by bicep-deploy setup
# ============================================================

dbms.security.authentication_providers=oidc-m2m,native
dbms.security.authorization_providers=oidc-m2m,native
dbms.security.oidc.m2m.visible={visible}
dbms.security.oidc.m2m.display_name={config.display_name}
dbms.security.oidc.m2m.well_known_discovery_uri={config.discovery_uri}
dbms.security.oidc.m2m.audience={config.audience}
dbms.security.oidc.m2m.claims.username={config.username_claim}
dbms.security.oidc.m2m.claims.groups={config.groups_claim}
dbms.security.oidc.m2m.config={config.token_type_config}
dbms.security.oidc.m2m.authorization.group_to_role_mapping={config.role_mapping}
"""

    # Entra ID path
    if not config.tenant_id:
        return ""

    # Use v1.0 discovery endpoint because Azure AD client credentials flow
    # returns v1.0 tokens by default (issuer: https://sts.windows.net/{tenant}/)
    return f"""
# ============================================================
# M2M OIDC Authentication (Microsoft Entra ID)
# Auto-generated by bicep-deploy setup
# ============================================================

dbms.security.authentication_providers=oidc-m2m,native
dbms.security.authorization_providers=oidc-m2m,native
dbms.security.oidc.m2m.visible=false
dbms.security.oidc.m2m.display_name=Entra ID M2M
dbms.security.oidc.m2m.well_known_discovery_uri=https://login.microsoftonline.com/{config.tenant_id}/.well-known/openid-configuration
dbms.security.oidc.m2m.audience={config.audience}
dbms.security.oidc.m2m.claims.username=sub
dbms.security.oidc.m2m.claims.groups=roles
dbms.security.oidc.m2m.config=token_type_principal=access_token;token_type_authentication=access_token
dbms.security.oidc.m2m.authorization.group_to_role_mapping="Neo4j.Admin"=admin;"Neo4j.ReadWrite"=editor;"Neo4j.ReadOnly"=reader
"""


class M2MSetupWizard:
    """Interactive wizard for M2M authentication setup."""

    def __init__(self):
        """Initialize the M2M setup wizard."""
        self.config = M2MConfig()

    def run(self) -> M2MConfig:
        """
        Run the M2M setup wizard.

        Returns:
            M2MConfig with the configuration (enabled=False if skipped)
        """
        console.print("\n[bold]Step 7: M2M Bearer Token Authentication (Optional)[/bold]")

        # Show explanation
        console.print(
            Panel(
                "[bold]Machine-to-Machine (M2M) Authentication[/bold]\n\n"
                "M2M authentication allows services, APIs, and automated processes to\n"
                "connect to Neo4j using OAuth 2.0 bearer tokens instead of username/password.\n\n"
                "This is ideal for:\n"
                "  - Backend services and APIs\n"
                "  - ETL pipelines (Spark, Airflow)\n"
                "  - CI/CD pipelines\n"
                "  - Microservices",
                title="About M2M Authentication",
                border_style="blue",
            )
        )

        from rich.prompt import IntPrompt

        console.print("\n[bold]M2M Provider:[/bold]")
        console.print("  1. [cyan]No M2M[/cyan] - Skip bearer token authentication")
        console.print("  2. [cyan]Keycloak[/cyan] - Use Keycloak OIDC (reads from keycloak-infra deployment)")
        console.print("  3. [cyan]Entra ID[/cyan] - Use Microsoft Entra ID")

        choice = IntPrompt.ask("Select option", default=1, choices=["1", "2", "3"])

        if choice == 1:
            console.print("[yellow]Skipping M2M authentication setup.[/yellow]")
            self.config.enabled = False
            return self.config
        elif choice == 2:
            return self._keycloak_setup()
        else:
            return self._entra_setup()

    def _keycloak_setup(self) -> M2MConfig:
        """Set up Keycloak OIDC by reading from keycloak-infra/.deployment.json."""
        import pathlib

        console.print("\n[bold]Keycloak OIDC Setup[/bold]")

        # Look for .deployment.json relative to project root
        # The deployments/ dir is typically one level down from the project root
        script_dir = pathlib.Path(__file__).resolve().parent.parent  # deployments/
        project_root = script_dir.parent  # project root
        default_path = project_root / "keycloak-infra" / ".deployment.json"

        if default_path.exists():
            console.print(f"[green]Found Keycloak deployment info: {default_path}[/green]")
            deployment_path = default_path
        else:
            console.print("[yellow]Could not find keycloak-infra/.deployment.json[/yellow]")
            path_str = Prompt.ask("Path to Keycloak .deployment.json")
            deployment_path = pathlib.Path(path_str).expanduser().resolve()
            if not deployment_path.exists():
                console.print(f"[red]File not found: {deployment_path}[/red]")
                self.config.enabled = False
                return self.config

        # Read deployment info
        with open(deployment_path) as f:
            deployment_info = json.load(f)

        oidc = deployment_info.get("oidc", {})
        if not oidc:
            console.print("[red]No 'oidc' section found in deployment info.[/red]")
            self.config.enabled = False
            return self.config

        # Populate config from deployment info
        self.config.enabled = True
        self.config.provider_type = "keycloak"
        self.config.discovery_uri = oidc.get("discovery_uri")
        self.config.token_endpoint = oidc.get("token_endpoint")
        self.config.audience = oidc.get("audience")
        self.config.client_id = oidc.get("client_id")
        self.config.client_secret = oidc.get("client_secret")
        self.config.role_mapping = oidc.get("role_mapping")
        self.config.token_type_config = oidc.get("token_type_config", self.config.token_type_config)
        self.config.display_name = oidc.get("display_name", self.config.display_name)
        self.config.oidc_visible = oidc.get("visible", False)

        # Show summary
        table = Table(title="Keycloak OIDC Configuration")
        table.add_column("Setting", style="cyan")
        table.add_column("Value", style="white")
        table.add_row("Discovery URI", self.config.discovery_uri or "Not set")
        table.add_row("Audience", self.config.audience or "Not set")
        table.add_row("Client ID", self.config.client_id or "Not set")
        secret_display = f"{self.config.client_secret[:8]}..." if self.config.client_secret else "Not set"
        table.add_row("Client Secret", secret_display)
        table.add_row("Role Mapping", self.config.role_mapping or "Not set")
        table.add_row("Display Name", self.config.display_name)
        console.print(table)

        # Validate discovery URI is reachable
        if self.config.discovery_uri:
            console.print("\n[cyan]Checking discovery endpoint...[/cyan]")
            try:
                import urllib.request
                req = urllib.request.Request(self.config.discovery_uri, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        console.print("[green]Discovery endpoint is reachable[/green]")
                    else:
                        console.print(f"[yellow]Discovery endpoint returned HTTP {resp.status}[/yellow]")
            except Exception as e:
                console.print(f"[yellow]Could not reach discovery endpoint: {e}[/yellow]")
                console.print("[yellow]This may be fine if Keycloak is still starting.[/yellow]")

        if not Confirm.ask("\nUse this configuration?", default=True):
            self.config.enabled = False
            return self.config

        # Show OIDC config preview
        console.print("\n[bold]Neo4j Configuration Preview:[/bold]")
        console.print(
            Panel(
                generate_neo4j_oidc_config(self.config),
                title="neo4j.conf OIDC Settings",
                border_style="green",
            )
        )

        return self.config

    def _entra_setup(self) -> M2MConfig:
        """Set up Entra ID M2M authentication."""
        self.config.enabled = True
        self.config.provider_type = "entra"

        # Detect tenant ID
        console.print("\n[cyan]Detecting Azure tenant...[/cyan]")
        tenant_id = get_tenant_id()

        if not tenant_id:
            console.print("[red]Could not detect Azure tenant. Please ensure you're logged in with 'az login'.[/red]")
            self.config.enabled = False
            return self.config

        # Show detected info
        sub_info = get_subscription_info()
        if sub_info:
            table = Table(title="Azure Account Information")
            table.add_column("Property", style="cyan")
            table.add_column("Value", style="white")
            table.add_row("Tenant ID", tenant_id)
            table.add_row("Subscription", sub_info.get("name", "Unknown"))
            table.add_row("User", sub_info.get("user", {}).get("name", "Unknown"))
            console.print(table)

        self.config.tenant_id = tenant_id

        # Ask for setup method
        console.print("\n[bold]Entra ID Setup Options:[/bold]")
        console.print("  1. [cyan]Automatic[/cyan] - Create Entra ID apps using Azure CLI (Recommended)")
        console.print("  2. [cyan]Manual[/cyan] - Enter existing app registration IDs")

        from rich.prompt import IntPrompt

        choice = IntPrompt.ask("Select option", default=1, choices=["1", "2"])

        if choice == 1:
            return self._automatic_setup()
        else:
            return self._manual_setup()

    def _automatic_setup(self) -> M2MConfig:
        """Run automatic Entra ID app setup."""
        console.print("\n[bold]Automatic Entra ID Setup[/bold]")

        # Get app names
        default_api_name = "Neo4j M2M API"
        api_name = Prompt.ask("API app name", default=default_api_name)
        self.config.api_app_name = api_name

        # Note: Azure requires identifier URIs to contain app ID, tenant ID, or verified domain
        # We'll use api://{app-id} format which is always allowed
        console.print("[dim]Note: API identifier will be set to api://{app-id} format (Azure requirement)[/dim]")

        # Check if API app already exists
        existing_app_id = check_app_exists(api_name)
        if existing_app_id:
            console.print(f"[yellow]App '{api_name}' already exists (ID: {existing_app_id})[/yellow]")
            if Confirm.ask("Use existing app?", default=True):
                self.config.api_app_id = existing_app_id
                # Get the existing app's identifier URI
                self.config.audience = f"api://{existing_app_id}"
            else:
                console.print("[yellow]Please choose a different name or delete the existing app.[/yellow]")
                return self._automatic_setup()
        else:
            # Create API app (returns app_id, object_id, actual_identifier_uri)
            result = create_api_app(api_name, "")  # Audience will be set by the function
            if not result:
                console.print("[red]Failed to create API app. Please check Azure CLI permissions.[/red]")
                self.config.enabled = False
                return self.config
            self.config.api_app_id = result[0]
            # result[2] is the actual identifier URI (api://{app-id})
            self.config.audience = result[2]
            console.print(f"[green]Created API app: {self.config.api_app_id}[/green]")
            console.print(f"[green]API identifier (audience): {self.config.audience}[/green]")

        # Ask about client app
        if Confirm.ask("\nCreate a client app for testing?", default=True):
            default_client_name = "Neo4j-Test-Client"
            client_name = Prompt.ask("Client app name", default=default_client_name)
            self.config.client_app_name = client_name

            # Check if exists
            existing_client_id = check_app_exists(client_name)
            if existing_client_id:
                console.print(f"[yellow]App '{client_name}' already exists (ID: {existing_client_id})[/yellow]")
                if Confirm.ask("Use existing app?", default=True):
                    self.config.client_app_id = existing_client_id
                else:
                    client_name = Prompt.ask("Enter different name")
                    self.config.client_app_name = client_name

            if not self.config.client_app_id:
                # Create client app
                client_id = create_client_app(client_name)
                if client_id:
                    self.config.client_app_id = client_id
                    console.print(f"[green]Created client app: {client_id}[/green]")

                    # Create client secret
                    secret_result = create_client_secret(client_id)
                    if secret_result:
                        self.config.client_secret = secret_result[0]
                        self.config.client_secret_expiry = secret_result[1]
                        console.print("[green]Created client secret[/green]")
                        console.print(
                            Panel(
                                f"[bold red]SAVE THIS SECRET NOW - IT WILL NOT BE SHOWN AGAIN![/bold red]\n\n"
                                f"Client Secret: [cyan]{self.config.client_secret}[/cyan]\n"
                                f"Expires: {self.config.client_secret_expiry}",
                                title="Client Secret",
                                border_style="red",
                            )
                        )

                    # Grant permission
                    console.print("\n[bold]Granting API Permission[/bold]")
                    role_choice = Prompt.ask(
                        "Select role to grant",
                        choices=["Neo4j.Admin", "Neo4j.ReadWrite", "Neo4j.ReadOnly"],
                        default="Neo4j.ReadWrite",
                    )

                    if grant_api_permission(self.config.client_app_id, self.config.api_app_id, role_choice):
                        console.print(f"[green]Granted '{role_choice}' permission to client app[/green]")
                    else:
                        console.print(
                            "[yellow]Could not auto-grant permission. "
                            "Please grant admin consent manually in Azure Portal.[/yellow]"
                        )

        self._show_summary()
        return self.config

    def _manual_setup(self) -> M2MConfig:
        """Run manual setup with existing app IDs."""
        console.print("\n[bold]Manual Configuration[/bold]")
        console.print("Enter your existing Entra ID app registration details:\n")
        console.print("[dim]Note: API identifier must be in api://{app-id} or api://{verified-domain}/path format[/dim]")

        self.config.api_app_id = Prompt.ask("API app client ID")

        # Default audience to api://{app-id} format
        default_audience = f"api://{self.config.api_app_id}" if self.config.api_app_id else "api://<app-id>"
        self.config.audience = Prompt.ask(
            "API identifier (audience)",
            default=default_audience,
        )

        self.config.client_app_id = Prompt.ask("Client app client ID (optional)", default="")

        self._show_summary()
        return self.config

    def _show_summary(self) -> None:
        """Show configuration summary."""
        console.print("\n[bold]M2M Configuration Summary[/bold]")

        table = Table()
        table.add_column("Setting", style="cyan")
        table.add_column("Value", style="white")

        table.add_row("Enabled", str(self.config.enabled))
        table.add_row("Tenant ID", self.config.tenant_id or "Not set")
        table.add_row("Audience", self.config.audience or "Not set")
        table.add_row("API App ID", self.config.api_app_id or "Not set")
        table.add_row("Client App ID", self.config.client_app_id or "Not set")

        console.print(table)

        if self.config.enabled:
            console.print("\n[bold]Neo4j Configuration Preview:[/bold]")
            console.print(
                Panel(
                    generate_neo4j_oidc_config(self.config),
                    title="neo4j.conf OIDC Settings",
                    border_style="green",
                )
            )

            if self.config.client_app_id and self.config.client_secret:
                console.print("\n[bold]Test Token Command:[/bold]")
                test_cmd = f"""curl -X POST "https://login.microsoftonline.com/{self.config.tenant_id}/oauth2/v2.0/token" \\
  -d "grant_type=client_credentials" \\
  -d "client_id={self.config.client_app_id}" \\
  -d "client_secret=<YOUR_SECRET>" \\
  -d "scope={self.config.audience}/.default"
"""
                console.print(Panel(test_cmd, title="Get Access Token", border_style="cyan"))
