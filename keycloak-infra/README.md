# Keycloak on Azure Container Apps

Deploys Keycloak in dev mode to Azure Container Apps with a pre-configured Neo4j realm. This is a demo setup for prototyping OAuth/OIDC bearer token authentication between Keycloak and Neo4j Enterprise Edition.

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) (logged in via `az login`)
- An active Azure subscription

## Quick Start

```bash
cd keycloak-infra

# Deploy everything (ACR, managed identity, Container Apps, Keycloak)
./deploy.sh deploy

# Check if Keycloak is ready
./deploy.sh status

# Acquire a token and validate the claims structure
./deploy.sh test

# Show all outputs and Neo4j OIDC config values
./deploy.sh outputs

# Tear down when done
./deploy.sh cleanup
```

## What Gets Deployed

The deployment runs in two steps: first the Container Registry (so the image can be built), then everything else.

| Resource | Purpose |
|----------|---------|
| Azure Container Registry (Basic) | Hosts the custom Keycloak image |
| User-Assigned Managed Identity | Pulls images from ACR (AcrPull role) |
| Log Analytics Workspace | Container Apps logging |
| Container Apps Environment | Hosting environment with HTTPS ingress |
| Container App (Keycloak 26.5.6) | Keycloak in dev mode, 1 CPU / 2 GiB, 1 replica |

The Container App uses a managed identity to pull images from ACR, with no admin credentials stored or passed.

The Keycloak image is built from `Dockerfile`, which copies `realm-export.json` into the stock Keycloak 26.5.6 image so the Neo4j realm is auto-imported on every startup.

## Pre-configured Realm

Same configuration as `keycloak-test-client/`:

| Resource | Value |
|----------|-------|
| Realm | `neo4j` |
| Client ID | `neo4j-client` |
| Client secret | `neo4j-client-secret` |
| Grant type | Client credentials (service account) |
| Client roles | `neo4j-admin`, `neo4j-readwrite`, `neo4j-readonly` |

Two protocol mappers are configured: a User Client Role mapper that flattens client roles into a top-level `roles` claim in the access token (required because Neo4j only reads top-level claims), and an audience mapper that adds `neo4j-client` to the `aud` claim (required because Keycloak defaults audience to `account` and Neo4j's audience check must match).

## Deployment Outputs

After deploy, `./deploy.sh outputs` shows everything needed to configure Neo4j:

| Output | Example |
|--------|---------|
| `oidcDiscoveryUri` | `https://keycloak-xxx.azurecontainerapps.io/realms/neo4j/.well-known/openid-configuration` |
| `oidcTokenEndpoint` | `https://keycloak-xxx.azurecontainerapps.io/realms/neo4j/protocol/openid-connect/token` |
| `oidcAudience` | `neo4j-client` |
| `oidcClientId` | `neo4j-client` |
| `oidcRoleMapping` | `"neo4j-admin"=admin;"neo4j-readwrite"=editor;"neo4j-readonly"=reader` |
| `oidcTokenTypeConfig` | `token_type_principal=access_token;token_type_authentication=access_token` |
| `oidcDisplayName` | `Keycloak M2M` |

These values feed directly into the Neo4j deployment CLI's manual OIDC setup option.

## Configuration

Override defaults with environment variables:

```bash
export KEYCLOAK_RESOURCE_GROUP=my-rg      # default: rg-keycloak-demo
export KEYCLOAK_LOCATION=westus2          # default: eastus
export KEYCLOAK_ADMIN_PASSWORD=secret     # prompted if not set
```

## Testing with the Python Client

After deploying, you can also use the test client from `keycloak-test-client/`:

```bash
KEYCLOAK_URL=$(./deploy.sh outputs 2>/dev/null | grep "Keycloak URL" | awk '{print $NF}')
cd ../keycloak-test-client
uv run test_keycloak.py --server-url "$KEYCLOAK_URL"
```

## Dev Mode Limitations

This deployment uses Keycloak's dev mode with an in-memory H2 database. This means:

- Realm data does not survive container restarts (but the realm is re-imported from the baked-in export on every startup, so the base config is always restored).
- Manual changes made through the admin console (new clients, changed secrets) are lost on restart.
- HTTPS is handled by Container Apps ingress, not Keycloak itself.

This is appropriate for a demo. For production, use Keycloak's production mode with Azure Database for PostgreSQL.
