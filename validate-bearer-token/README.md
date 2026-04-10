# Validate Bearer Token

This tool validates Neo4j M2M (Machine-to-Machine) bearer token authentication against a deployed Neo4j instance.

## Prerequisites

- A deployed Neo4j instance with M2M authentication configured
- Client secret from the Entra ID app registration
- Python 3.10+ and [uv](https://docs.astral.sh/uv/)

## Quick Start

```bash
# Navigate to this directory
cd validate-bearer-token

# Set your client secret
export NEO4J_CLIENT_SECRET="your-client-secret-from-setup"

# Run validation
uv run validate_bearer.py
```

## Usage

```bash
# Validate default scenario (standalone-v2025)
uv run validate_bearer.py

# Validate specific scenario
uv run validate_bearer.py --scenario cluster-v2025

# Pass secret as argument (less secure)
uv run validate_bearer.py --secret "your-secret"

# Skip basic auth test
uv run validate_bearer.py --skip-basic

# Only validate token acquisition (don't connect to Neo4j)
uv run validate_bearer.py --validate-token
```

## What It Does

1. **Loads deployment configuration** from `.deployments/{scenario}.json`
2. **Tests basic authentication** (username/password) to verify Neo4j is running
3. **Acquires bearer token** from Microsoft Entra ID using client credentials flow
4. **Tests bearer token authentication** against Neo4j
5. **Displays results** including the authenticated user's roles

With `--validate-token`, the script stops after step 3 and displays the token details without connecting to Neo4j. This is useful for:
- Verifying Entra ID app registration is configured correctly
- Debugging token acquisition issues before testing Neo4j connectivity
- Confirming client credentials are valid

## Configuration File

The script reads from `.deployments/{scenario}.json`, which is created by `neo4j-deploy` and contains connection and M2M authentication details. Both Entra ID and Keycloak providers are supported.

### Entra ID

```json
{
  "scenario": "standalone-v2025",
  "connection": {
    "neo4j_uri": "bolt://hostname:7687",
    "username": "neo4j",
    "password": "..."
  },
  "m2m_auth": {
    "enabled": true,
    "provider_type": "entra",
    "tenant_id": "...",
    "client_app_id": "...",
    "audience": "api://neo4j-m2m",
    "scope": "api://neo4j-m2m/.default",
    "token_endpoint": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
  }
}
```

### Keycloak

When Neo4j is deployed with Keycloak as the OIDC provider (via `keycloak-infra/`), the deployment file includes Keycloak-specific fields. The client secret is stored in the deployment JSON, so you don't need to set `NEO4J_CLIENT_SECRET`.

```json
{
  "scenario": "standalone-v2025",
  "connection": {
    "neo4j_uri": "bolt://hostname:7687",
    "username": "neo4j",
    "password": "..."
  },
  "m2m_auth": {
    "enabled": true,
    "provider_type": "keycloak",
    "discovery_uri": "https://your-keycloak.azurecontainerapps.io/realms/neo4j/.well-known/openid-configuration",
    "token_endpoint": "https://your-keycloak.azurecontainerapps.io/realms/neo4j/protocol/openid-connect/token",
    "audience": "neo4j-client",
    "client_id": "neo4j-client",
    "client_secret": "...",
    "role_mapping": "\"neo4j-admin\"=admin;\"neo4j-readwrite\"=editor;\"neo4j-readonly\"=reader",
    "display_name": "Keycloak M2M"
  }
}
```

## Keycloak Usage

If you deployed Keycloak to Azure via `keycloak-infra/` and Neo4j with Keycloak OIDC enabled, the deployment file is already configured. Run:

```bash
cd validate-bearer-token
uv run validate_bearer.py --scenario standalone-v2025
```

This acquires a token from the Azure Keycloak instance, tests basic auth, and then tests bearer token auth against Neo4j. No environment variables needed — the client secret is read from the deployment JSON.

To validate only the token (without connecting to Neo4j):

```bash
uv run validate_bearer.py --scenario standalone-v2025 --validate-token
```

You can also test against a local Keycloak started via `keycloak-test-client/` — just ensure the `.deployments/{scenario}.json` points to `http://localhost:8080` as the token endpoint.

## Troubleshooting

### "M2M authentication is not enabled"

Run `uv run neo4j-deploy setup` and enable M2M authentication in Step 7, then redeploy.

### "Token acquisition failed"

- Verify tenant ID and client ID are correct
- Check that the client secret is valid and not expired
- Ensure the client app has API permissions granted

### "Authentication failed" with valid token

- Check Neo4j OIDC configuration matches your Entra ID setup
- Verify the `audience` in neo4j.conf matches your API identifier
- Ensure the client app has the correct role assigned
- Check Neo4j logs: `journalctl -u neo4j -f`

### Generating a new client secret

If you lost your client secret:

```bash
az ad app credential reset \
  --id YOUR_CLIENT_APP_ID \
  --display-name "New Secret" \
  --years 1
```
