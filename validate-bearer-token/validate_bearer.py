#!/usr/bin/env python3
"""
Validate Neo4j M2M Bearer Token Authentication

This script tests bearer token authentication against a deployed Neo4j instance
using the configuration from .deployments/{scenario}-{engine}.json (e.g. standalone-v2025-bicep.json)

Usage:
    # With client secret from environment
    export NEO4J_CLIENT_SECRET="your-client-secret"
    uv run validate-bearer

    # With client secret as argument
    uv run validate-bearer --secret "your-client-secret"

    # Specify scenario
    uv run validate-bearer --scenario standalone-v2025
"""

import base64
import json
import os
import sys
from pathlib import Path
from typing import Optional

import requests
from neo4j import GraphDatabase, bearer_auth
from neo4j.exceptions import AuthError, ServiceUnavailable
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def decode_jwt_payload(token: str) -> dict:
    """
    Decode a JWT token and return the payload claims.
    Does NOT verify the signature - just extracts the claims for debugging.
    """
    try:
        # JWT has 3 parts: header.payload.signature
        parts = token.split(".")
        if len(parts) != 3:
            return {"error": "Invalid JWT format"}

        # Decode the payload (middle part)
        # Add padding if needed for base64 decoding
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding

        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception as e:
        return {"error": f"Failed to decode JWT: {e}"}

# Default deployment file location (relative to script)
DEFAULT_DEPLOYMENTS_DIR = Path(__file__).parent.parent / ".deployments"


def _find_deployment_file(scenario: str, deployments_dir: Path) -> Path | None:
    """Return the engine-keyed deployment JSON for scenario, or None if not found."""
    candidates = [
        deployments_dir / f"{scenario}-bicep.json",
        deployments_dir / f"{scenario}-ansible.json",
    ]
    existing = [p for p in candidates if p.exists()]
    return max(existing, key=lambda p: p.stat().st_mtime) if existing else None


def load_deployment_config(scenario: str, deployments_dir: Path = DEFAULT_DEPLOYMENTS_DIR) -> dict:
    """
    Load deployment configuration from .deployments/{scenario}-{engine}.json

    Args:
        scenario: Scenario name (e.g., "standalone-v2025")
        deployments_dir: Path to .deployments directory

    Returns:
        Deployment configuration dict

    Raises:
        FileNotFoundError: If deployment file doesn't exist
    """
    file_path = _find_deployment_file(scenario, deployments_dir)

    if not file_path:
        raise FileNotFoundError(
            f"Deployment file not found: {scenario}-bicep.json or {scenario}-ansible.json"
            f" in {deployments_dir}\n"
            f"Available deployments: {[p.name for p in deployments_dir.glob('*.json')]}"
        )

    with open(file_path) as f:
        return json.load(f)


def get_bearer_token_entra(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    scope: str,
) -> tuple[str, int]:
    """
    Acquire bearer token from Microsoft Entra ID using MSAL client credentials flow.

    Args:
        tenant_id: Azure tenant ID
        client_id: Client application ID
        client_secret: Client secret
        scope: API scope (e.g., "api://neo4j-m2m/.default")

    Returns:
        Tuple of (access_token, expires_in_seconds)

    Raises:
        Exception: If token acquisition fails
    """
    from msal import ConfidentialClientApplication

    authority = f"https://login.microsoftonline.com/{tenant_id}"

    app = ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=client_secret,
    )

    result = app.acquire_token_for_client(scopes=[scope])

    if "access_token" not in result:
        error = result.get("error", "Unknown error")
        error_description = result.get("error_description", "No description")
        raise Exception(f"Token acquisition failed: {error}\n{error_description}")

    return result["access_token"], result.get("expires_in", 0)


def get_bearer_token_oidc(
    token_endpoint: str,
    client_id: str,
    client_secret: str,
) -> tuple[str, int]:
    """
    Acquire bearer token via plain HTTP POST (client credentials grant).
    Works with any OIDC provider including Keycloak.

    Args:
        token_endpoint: Token endpoint URL
        client_id: Client ID
        client_secret: Client secret

    Returns:
        Tuple of (access_token, expires_in_seconds)

    Raises:
        Exception: If token acquisition fails
    """
    resp = requests.post(
        token_endpoint,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )

    if resp.status_code != 200:
        raise Exception(f"Token acquisition failed (HTTP {resp.status_code}): {resp.text}")

    result = resp.json()
    if "access_token" not in result:
        error = result.get("error", "Unknown error")
        error_description = result.get("error_description", "No description")
        raise Exception(f"Token acquisition failed: {error}\n{error_description}")

    return result["access_token"], result.get("expires_in", 0)


def test_neo4j_connection(
    neo4j_uri: str,
    token: str,
) -> tuple[bool, str, Optional[dict]]:
    """
    Test Neo4j connection using bearer token authentication.

    Args:
        neo4j_uri: Neo4j connection URI (bolt:// or neo4j://)
        token: Bearer access token

    Returns:
        Tuple of (success, message, result_data)
    """
    driver = None
    try:
        driver = GraphDatabase.driver(neo4j_uri, auth=bearer_auth(token))

        # Verify connectivity
        driver.verify_connectivity()

        # Run a simple query to verify auth works
        with driver.session() as session:
            # Get current user info
            result = session.run("SHOW CURRENT USER")
            user_record = result.single()

            user_info = {
                "username": user_record["user"],
                "roles": list(user_record["roles"]),
            }

            # Run a simple data query
            count_result = session.run("MATCH (n) RETURN count(n) AS nodeCount")
            node_count = count_result.single()["nodeCount"]
            user_info["node_count"] = node_count

            return True, "Connection successful", user_info

    except AuthError as e:
        return False, f"Authentication failed: {e}", None
    except ServiceUnavailable as e:
        return False, f"Neo4j service unavailable: {e}", None
    except Exception as e:
        return False, f"Connection error: {e}", None
    finally:
        if driver:
            driver.close()


def test_basic_auth_connection(
    neo4j_uri: str,
    username: str,
    password: str,
) -> tuple[bool, str]:
    """
    Test Neo4j connection using basic (username/password) authentication.

    Args:
        neo4j_uri: Neo4j connection URI
        username: Neo4j username
        password: Neo4j password

    Returns:
        Tuple of (success, message)
    """
    driver = None
    try:
        driver = GraphDatabase.driver(neo4j_uri, auth=(username, password))
        driver.verify_connectivity()
        return True, "Basic auth connection successful"
    except Exception as e:
        return False, f"Basic auth failed: {e}"
    finally:
        if driver:
            driver.close()


def main():
    """Main entry point for bearer token validation."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate Neo4j M2M bearer token authentication"
    )
    parser.add_argument(
        "--scenario",
        default="standalone-v2025",
        help="Scenario name (default: standalone-v2025)",
    )
    parser.add_argument(
        "--secret",
        help="Client secret (or set NEO4J_CLIENT_SECRET env var)",
    )
    parser.add_argument(
        "--deployments-dir",
        type=Path,
        default=DEFAULT_DEPLOYMENTS_DIR,
        help="Path to .deployments directory",
    )
    parser.add_argument(
        "--skip-basic",
        action="store_true",
        help="Skip basic auth test",
    )
    parser.add_argument(
        "--validate-token",
        action="store_true",
        help="Only validate token acquisition (don't connect to Neo4j)",
    )

    args = parser.parse_args()

    # Load deployment configuration
    console.print(f"\n[bold]Loading deployment configuration[/bold]")
    console.print(f"[dim]Scenario: {args.scenario}[/dim]")

    try:
        config = load_deployment_config(args.scenario, args.deployments_dir)
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    # Display deployment info
    conn = config.get("connection", {})
    m2m = config.get("m2m_auth", {})

    table = Table(title="Deployment Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("Scenario", config.get("scenario", "Unknown"))
    table.add_row("Neo4j URI", conn.get("neo4j_uri", "Not set"))
    table.add_row("Browser URL", conn.get("browser_url", "Not set"))
    table.add_row("M2M Enabled", str(m2m.get("enabled", False)))

    if m2m.get("enabled"):
        provider = m2m.get("provider_type", "entra")
        table.add_row("Provider", provider)
        if provider == "keycloak":
            table.add_row("Token Endpoint", m2m.get("token_endpoint", "Not set"))
            table.add_row("Client ID", m2m.get("client_id", "Not set"))
            table.add_row("Audience", m2m.get("audience", "Not set"))
        else:
            table.add_row("Tenant ID", m2m.get("tenant_id", "Not set"))
            table.add_row("Client App ID", m2m.get("client_app_id", "Not set"))
            table.add_row("Audience", m2m.get("audience", "Not set"))

    console.print(table)

    # Test basic auth first (optional)
    if not args.skip_basic:
        console.print(f"\n[bold]Testing Basic Authentication[/bold]")

        basic_success, basic_msg = test_basic_auth_connection(
            conn.get("neo4j_uri"),
            conn.get("username", "neo4j"),
            conn.get("password"),
        )

        if basic_success:
            console.print(f"[green]{basic_msg}[/green]")
        else:
            console.print(f"[yellow]{basic_msg}[/yellow]")

    # Check if M2M is enabled
    if not m2m.get("enabled"):
        console.print(
            Panel(
                "[yellow]M2M authentication is not enabled for this deployment.[/yellow]\n\n"
                "To enable M2M authentication:\n"
                "1. Run [cyan]uv run bicep-deploy setup[/cyan] and enable M2M in Step 7\n"
                "2. Redeploy the scenario\n\n"
                "Or manually configure OIDC in neo4j.conf on the server.",
                title="M2M Not Configured",
                border_style="yellow",
            )
        )
        sys.exit(0)

    # Get client secret
    provider_type = m2m.get("provider_type", "entra")

    # For Keycloak, try reading secret from deployment info first
    client_secret = args.secret or os.environ.get("NEO4J_CLIENT_SECRET")
    if not client_secret and provider_type == "keycloak":
        client_secret = m2m.get("client_secret")

    if not client_secret:
        console.print(
            Panel(
                "[red]Client secret is required for M2M authentication.[/red]\n\n"
                "Provide it via:\n"
                "  1. Environment variable: [cyan]export NEO4J_CLIENT_SECRET='your-secret'[/cyan]\n"
                "  2. Command line argument: [cyan]--secret 'your-secret'[/cyan]",
                title="Client Secret Required",
                border_style="red",
            )
        )
        sys.exit(1)

    # Acquire bearer token
    console.print(f"\n[bold]Acquiring Bearer Token[/bold]")
    console.print(f"[dim]Provider: {provider_type}[/dim]")
    console.print(f"[dim]Token endpoint: {m2m.get('token_endpoint')}[/dim]")

    try:
        if provider_type == "keycloak":
            token, expires_in = get_bearer_token_oidc(
                token_endpoint=m2m.get("token_endpoint"),
                client_id=m2m.get("client_id"),
                client_secret=client_secret,
            )
        else:
            token, expires_in = get_bearer_token_entra(
                tenant_id=m2m.get("tenant_id"),
                client_id=m2m.get("client_app_id"),
                client_secret=client_secret,
                scope=m2m.get("scope"),
            )
        console.print(f"[green]Token acquired successfully[/green]")
        console.print(f"[dim]Expires in: {expires_in} seconds[/dim]")
        console.print(f"[dim]Token (first 50 chars): {token[:50]}...[/dim]")

        # Decode and display JWT claims for debugging
        claims = decode_jwt_payload(token)
        console.print(f"\n[bold]JWT Token Claims (for debugging):[/bold]")
        claims_table = Table()
        claims_table.add_column("Claim", style="cyan")
        claims_table.add_column("Value", style="white")

        # Show important claims for OIDC debugging
        important_claims = ["aud", "iss", "sub", "roles", "appid", "azp", "tid", "ver"]
        for claim in important_claims:
            if claim in claims:
                value = claims[claim]
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value)
                claims_table.add_row(claim, str(value))

        console.print(claims_table)

        # Check for potential issues
        expected_audience = m2m.get("audience")
        actual_audience = claims.get("aud")
        if expected_audience and actual_audience and expected_audience != actual_audience:
            console.print(f"\n[red]AUDIENCE MISMATCH![/red]")
            console.print(f"  Expected (neo4j.conf): {expected_audience}")
            console.print(f"  Actual (token aud):    {actual_audience}")

        if "roles" not in claims:
            console.print(f"\n[yellow]WARNING: No 'roles' claim in token![/yellow]")
            console.print("  The client app may not have app roles assigned.")
            console.print("  Check Azure Portal > App registrations > Client app > API permissions")

    except Exception as e:
        console.print(f"[red]Failed to acquire token: {e}[/red]")
        sys.exit(1)

    # If --validate-token, show token details and exit
    if args.validate_token:
        console.print(
            Panel(
                f"[bold green]Token Acquisition Successful![/bold green]\n\n"
                f"Token Type: Bearer\n"
                f"Provider: {provider_type}\n"
                f"Expires In: {expires_in} seconds\n\n"
                f"[dim]Token preview: {token[:80]}...[/dim]",
                title="Token Validated",
                border_style="green",
            )
        )

        # Show equivalent curl command
        if provider_type == "keycloak":
            curl_cmd = f'''curl -X POST "{m2m.get("token_endpoint")}" \\
  -d "grant_type=client_credentials" \\
  -d "client_id={m2m.get("client_id")}" \\
  -d "client_secret=<YOUR_SECRET>"'''
        else:
            curl_cmd = f'''curl -X POST "https://login.microsoftonline.com/{m2m.get("tenant_id")}/oauth2/v2.0/token" \\
  -d "grant_type=client_credentials" \\
  -d "client_id={m2m.get("client_app_id")}" \\
  -d "client_secret=<YOUR_SECRET>" \\
  -d "scope={m2m.get("scope")}"'''

        console.print(f"\n[bold]Equivalent curl command:[/bold]")
        console.print(Panel(curl_cmd, title="Get Access Token", border_style="cyan"))

        sys.exit(0)

    # Test Neo4j connection with bearer token
    console.print(f"\n[bold]Testing Bearer Token Authentication[/bold]")
    console.print(f"[dim]Connecting to: {conn.get('neo4j_uri')}[/dim]")

    success, message, user_info = test_neo4j_connection(
        neo4j_uri=conn.get("neo4j_uri"),
        token=token,
    )

    if success:
        console.print(
            Panel(
                f"[bold green]Bearer Token Authentication Successful![/bold green]\n\n"
                f"Username: {user_info.get('username', 'Unknown')}\n"
                f"Roles: {', '.join(user_info.get('roles', []))}\n"
                f"Node Count: {user_info.get('node_count', 0)}",
                title="Success",
                border_style="green",
            )
        )

        # Show example code
        console.print(f"\n[bold]Example Python Code[/bold]")
        if provider_type == "keycloak":
            example_code = f'''
import requests
from neo4j import GraphDatabase, bearer_auth

# Configuration
TOKEN_ENDPOINT = "{m2m.get('token_endpoint')}"
CLIENT_ID = "{m2m.get('client_id')}"
CLIENT_SECRET = "your-client-secret"
NEO4J_URI = "{conn.get('neo4j_uri')}"

# Get token
resp = requests.post(TOKEN_ENDPOINT, data={{
    "grant_type": "client_credentials",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
}})
token = resp.json()["access_token"]

# Connect to Neo4j
driver = GraphDatabase.driver(NEO4J_URI, auth=bearer_auth(token))
with driver.session() as session:
    result = session.run("MATCH (n) RETURN count(n)")
    print(result.single()[0])
driver.close()
'''
        else:
            example_code = f'''
from msal import ConfidentialClientApplication
from neo4j import GraphDatabase, bearer_auth

# Configuration
TENANT_ID = "{m2m.get('tenant_id')}"
CLIENT_ID = "{m2m.get('client_app_id')}"
CLIENT_SECRET = "your-client-secret"  # From environment
SCOPE = "{m2m.get('scope')}"
NEO4J_URI = "{conn.get('neo4j_uri')}"

# Get token
app = ConfidentialClientApplication(CLIENT_ID, authority=f"https://login.microsoftonline.com/{{TENANT_ID}}", client_credential=CLIENT_SECRET)
result = app.acquire_token_for_client(scopes=[SCOPE])
token = result["access_token"]

# Connect to Neo4j
driver = GraphDatabase.driver(NEO4J_URI, auth=bearer_auth(token))
with driver.session() as session:
    result = session.run("MATCH (n) RETURN count(n)")
    print(result.single()[0])
driver.close()
'''
        console.print(Panel(example_code.strip(), title="Python Example", border_style="cyan"))

    else:
        console.print(
            Panel(
                f"[bold red]Bearer Token Authentication Failed[/bold red]\n\n{message}\n\n"
                "Troubleshooting:\n"
                "1. Verify Neo4j OIDC configuration is correct\n"
                "2. Check that the audience matches the API identifier\n"
                "3. Ensure the client app has the correct role granted\n"
                "4. Check Neo4j logs: [cyan]journalctl -u neo4j -f[/cyan]",
                title="Failed",
                border_style="red",
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
