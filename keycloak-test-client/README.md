# Keycloak Test Client

Minimal test client for validating Keycloak's OAuth 2.0 client credentials flow. Acquires a token from a local Keycloak instance and displays the decoded JWT claims.

## Prerequisites

- Docker
- [uv](https://docs.astral.sh/uv/)

## Quick Start

From the `keycloak-test-client/` directory:

```bash
./keycloak.sh start           # Start Keycloak (waits until ready)
uv run test_keycloak.py       # Acquire token and display claims
./keycloak.sh stop            # Stop and remove container
./keycloak.sh status          # Check if Keycloak is running
```

The start command auto-imports the `neo4j` realm with a pre-configured client and roles. The admin console is available at http://localhost:8080 (admin/admin).

### Against Azure Deployment

To run against a Keycloak instance deployed to Azure via `keycloak-infra/`:

```bash
uv run test_keycloak.py --azure
```

This reads the Keycloak URL, client ID, and client secret from `keycloak-infra/.deployment.json` (created after a successful Azure deployment). You can override individual values:

```bash
uv run test_keycloak.py --azure --realm custom-realm
uv run test_keycloak.py --server-url https://your-keycloak.azurecontainerapps.io --client-id neo4j-client --client-secret your-secret
```

## Pre-configured Realm

The `realm-export.json` creates:

| Resource | Value |
|----------|-------|
| Realm | `neo4j` |
| Client ID | `neo4j-client` |
| Client secret | `neo4j-client-secret` |
| Grant type | Client credentials (service account) |
| Client roles | `neo4j-admin`, `neo4j-readwrite`, `neo4j-readonly` |

All three client roles are assigned to the service account. Two protocol mappers are configured on the client:

1. **User Client Role mapper** — flattens client roles into a top-level `roles` claim in the access token, required because Neo4j's `dbms.security.oidc.*.claims.groups` only reads top-level claims.
2. **Audience mapper** — adds `neo4j-client` to the `aud` claim so it matches the `dbms.security.oidc.m2m.audience` value Neo4j expects. Without this, Keycloak defaults `aud` to `account` (an internal client) and Neo4j rejects the token.

## Token Claims Structure

The protocol mappers produce a token with a top-level `roles` array and correct audience:

```json
{
  "iss": "http://localhost:8080/realms/neo4j",
  "aud": ["neo4j-client", "account"],
  "azp": "neo4j-client",
  "roles": ["neo4j-admin", "neo4j-readwrite", "neo4j-readonly"]
}
```

This means the Neo4j OIDC config is nearly identical between providers:

```
# Entra
dbms.security.oidc.m2m.claims.groups=roles
dbms.security.oidc.m2m.authorization.group_to_role_mapping="Neo4j.Admin"=admin;"Neo4j.ReadWrite"=editor;"Neo4j.ReadOnly"=reader

# Keycloak
dbms.security.oidc.m2m.claims.groups=roles
dbms.security.oidc.m2m.authorization.group_to_role_mapping="neo4j-admin"=admin;"neo4j-readwrite"=editor;"neo4j-readonly"=reader
```

Key differences from Entra ID tokens:

| Field | Entra ID | Keycloak |
|-------|----------|----------|
| Discovery URI | `https://login.microsoftonline.com/{tenant}/.well-known/openid-configuration` | `http://localhost:8080/realms/neo4j/.well-known/openid-configuration` |
| Audience | `api://{app-id}` | `neo4j-client` (via audience mapper) |
| Role claim | `roles` (top-level, native) | `roles` (top-level, via protocol mapper) |
| Username claim | `sub` | `preferred_username` or `sub` |

