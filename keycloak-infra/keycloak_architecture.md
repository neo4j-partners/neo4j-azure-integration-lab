# Keycloak OIDC Integration for Neo4j on Azure

This document covers the architecture and design decisions for adding Keycloak as an OIDC provider to the Neo4j Azure EE deployment template. It is the canonical reference for the Keycloak integration, covering infrastructure, token flow, CLI integration, and deployment.

## OAuth Flow

The integration uses the OAuth 2.0 Client Credentials Grant for machine-to-machine authentication. No human login or browser is involved. A client application authenticates directly with Keycloak, receives a signed JWT, and presents it to Neo4j as a bearer token.

```
                          DEPLOYMENT TIME
                          ==============

  keycloak-infra/                    deployments/
  deploy.sh deploy                   bicep-deploy setup
       |                                  |
       v                                  v
  +------------------+             +-----------------+
  | Azure Container  |             | Setup Wizard    |
  | Apps: Keycloak   |             | reads           |
  | (neo4j realm,    |             | .deployment.json|
  | neo4j-client,    |             | auto-configures |
  | roles, mappers)  |             | OIDC settings   |
  +------------------+             +-----------------+
       |                                  |
       | writes .deployment.json          | bicep-deploy deploy
       | (discovery_uri, token_endpoint,  |
       |  client_id, client_secret,       v
       |  audience, role_mapping)   +-----------------+
       |                            | Azure VM: Neo4j |
       +--------------------------->| neo4j.conf with |
                                    | OIDC config     |
                                    +-----------------+


                          RUNTIME (Token Flow)
                          ====================

  +--------+           +-----------+           +-----------+
  | Client |           | Keycloak  |           |   Neo4j   |
  | App    |           | (Azure    |           | (Azure VM)|
  |        |           | Container |           |           |
  +---+----+           |  Apps)    |           +-----+-----+
      |                +-----+-----+                 |
      |                      |                       |
      | 1. POST /token       |                       |
      |   grant_type=        |                       |
      |   client_credentials |                       |
      |   client_id=         |                       |
      |   neo4j-client       |                       |
      |   client_secret=xxx  |                       |
      |--------------------->|                       |
      |                      |                       |
      | 2. JWT access token  |                       |
      |   {                  |                       |
      |    iss: keycloak/    |                       |
      |         realms/neo4j |                       |
      |    aud: neo4j-client |                       |
      |    roles: [neo4j-    |                       |
      |      admin, ...]     |                       |
      |    sub: service-     |                       |
      |      account-...     |                       |
      |   }                  |                       |
      |<---------------------|                       |
      |                      |                       |
      | 3. Bolt connection   |                       |
      |   Authorization:     |                       |
      |   Bearer <JWT>       |                       |
      |--------------------------------------------->|
      |                      |                       |
      |                      | 4. GET /.well-known/  |
      |                      |    openid-config      |
      |                      |<----------------------|
      |                      |                       |
      |                      | 5. Discovery doc      |
      |                      |    (jwks_uri, issuer) |
      |                      |---------------------->|
      |                      |                       |
      |                      | 6. GET /certs (JWKS)  |
      |                      |<----------------------|
      |                      |                       |
      |                      | 7. Public keys        |
      |                      |---------------------->|
      |                      |                       |
      |                      |         8. Verify JWT |
      |                      |            signature  |
      |                      |            Check aud  |
      |                      |            Map roles: |
      |                      |            neo4j-admin|
      |                      |              -> admin |
      |                      |                       |
      | 9. Authenticated     |                       |
      |   Bolt session       |                       |
      |   (with mapped roles)|                       |
      |<---------------------------------------------|
      |                      |                       |
```

Steps 4-7 happen once (Neo4j caches the JWKS keys). Subsequent tokens are verified locally against the cached keys until they expire or Neo4j refreshes the key set.

## Goal and Overview

The deployment template supports machine-to-machine bearer token authentication via Microsoft Entra ID. Adding Keycloak as a second OIDC provider serves organizations that run Keycloak as their identity provider and cannot use Entra for this purpose.

Neo4j's OIDC configuration is already provider-agnostic. The `neo4j.conf` settings need a discovery URI, an audience value, claims mappings, and role mappings. The Bicep templates and cloud-init scripts pass these values through as an opaque string. Nothing in the infrastructure layer cares whether the identity provider is Entra, Keycloak, or anything else that speaks OIDC. The provider-specific logic lives entirely in the deployment CLI: the setup wizard that creates Entra app registrations, the token validation script that uses MSAL, and the config generation that constructs Entra discovery URLs. That is a contained surface area.

Supporting both providers rather than replacing Entra is the right path. This is an Azure deployment template; future clients deploying on Azure will reasonably expect Entra support. The refactoring effort is the same either way, and the additional work to keep Entra as an option is minimal once the config layer is provider-agnostic. Both providers use standard OAuth 2.0 client credentials flow and standard OIDC discovery. The differences are in provisioning (Entra app registrations vs. Keycloak realm and client configuration) and endpoint URLs. The runtime behavior from Neo4j's perspective is identical.

The Keycloak integration provides:

- A pre-configured Keycloak realm with client roles mapped to Neo4j's authorization model
- A one-command Azure deployment (`deploy.sh deploy`) that stands up Keycloak on Container Apps with HTTPS
- Bicep outputs and a `.deployment.json` file containing every value needed for Neo4j OIDC configuration
- A test client that validates token acquisition and JWT claims structure
- A Keycloak option in the Neo4j deployment CLI setup wizard that auto-reads `.deployment.json` and configures OIDC end-to-end
- A dual-path token validation script that supports both Keycloak (plain HTTP) and Entra ID (MSAL)

## Architecture Decisions

### Dev mode with baked-in realm export

Keycloak runs in dev mode with the embedded H2 database. Realm data does not survive container restarts, but the realm configuration is baked into the Docker image and re-imported on every startup via `--import-realm`. For a demo this is the right tradeoff: it eliminates the need for Azure Database for PostgreSQL, reduces the resource count, and makes the deployment self-contained. The realm export file (`realm-export.json`) is the single source of truth for the client, roles, and protocol mappers.

Production deployments would replace dev mode with Keycloak's production mode backed by PostgreSQL Flexible Server. That path requires a custom image with the DigiCert root CA certificate for Azure PostgreSQL SSL verification, `KC_HOSTNAME` configured for strict mode, and a minimum of one replica to avoid Keycloak's 30-90 second Java cold starts.

### Container Apps with HTTPS ingress

Azure Container Apps provides TLS termination without a separate reverse proxy. This matters because the issuer in Keycloak tokens must match the URL Neo4j uses to fetch the OIDC discovery document. If Keycloak believes its hostname is `http://localhost:8080` but Neo4j reaches it at `https://keycloak.azurecontainerapps.io`, every token is rejected due to issuer mismatch. Container Apps solves this by giving Keycloak a stable HTTPS FQDN that is deterministic before the app is created (pattern: `https://{appName}.{defaultDomain}`), which the Bicep template passes to Keycloak via `KC_HOSTNAME`.

The alternatives were considered. AKS is overkill for a single container. App Service works but Container Apps is cheaper for container workloads and purpose-built for this use case. A standalone VM carries the highest maintenance burden for no benefit.

### Managed identity for image pull

The Container App uses a user-assigned managed identity with the `AcrPull` role to pull images from Azure Container Registry. No admin credentials are stored or passed. The identity module (`modules/identity.bicep`) creates the identity and grants the role assignment; the keycloak module references it in the registry configuration.

### Generated client secret per deployment

The `deploy.sh` script generates a unique client secret via `uuidgen` for each deployment and substitutes it into `realm-export.json` before building the image. The realm export in the repository contains a placeholder value (`neo4j-client-secret`). The generated secret is stored in `.deployment.json` alongside the deployment outputs. This means each deployment has a distinct credential, and the secret never appears in Bicep outputs or ARM template state.

### Keycloak option with auto-configuration from deployment output

Rather than building a full provider-agnostic OIDC abstraction, the deployment CLI gets a "Keycloak" option alongside the existing Entra ID option. When the user selects Keycloak, the wizard automatically reads `keycloak-infra/.deployment.json` (produced by `deploy.sh`) and populates all OIDC settings without manual input. If the file is not found at the default location, the wizard asks for a path. This keeps the integration tight to the Keycloak deployment workflow while requiring no manual value copying.

## Token Structure and Claims Mapping

### The claims problem

Neo4j's `dbms.security.oidc.<provider>.claims.groups` setting accepts only a top-level claim name. It does not support dot-notation or nested path traversal. This is tracked in [neo4j/neo4j#13096](https://github.com/neo4j/neo4j/issues/13096), filed March 2023, still open with no indication of implementation.

Keycloak places client roles in nested structures by default: `realm_access.roles` for realm roles, `resource_access.{client_id}.roles` for client roles. Neo4j cannot read either location. This makes a protocol mapper that flattens roles into a top-level claim a hard requirement, not a convenience.

### Protocol mappers

The realm export configures two protocol mappers on the `neo4j-client` client:

**User Client Role mapper** (`neo4j-roles`). Maps client roles assigned to the service account into a top-level `roles` claim in the access token. This mirrors what Entra does natively: Entra places app roles in a top-level `roles` array, so the Neo4j config becomes identical between providers (`claims.groups=roles`). The mapper uses `jsonType.label=String`, which handles both single-role and multi-role assignments correctly (Neo4j accepts either a string or an array since 5.5.0).

**Audience mapper** (`neo4j-audience`). Adds `neo4j-client` to the `aud` claim. Keycloak defaults the audience to `account` (an internal client used for account management). Configuring Neo4j with `audience=account` would be misleading and fragile. With the mapper, the token contains `"aud": ["neo4j-client", "account"]` and Neo4j's audience check matches against `neo4j-client`.

### Role naming

Keycloak client roles use lowercase hyphenated names following Keycloak convention. Each maps to a Neo4j built-in role:

| Keycloak client role | Neo4j role | Entra equivalent |
|---|---|---|
| `neo4j-admin` | `admin` | `Neo4j.Admin` |
| `neo4j-readwrite` | `editor` | `Neo4j.ReadWrite` |
| `neo4j-readonly` | `reader` | `Neo4j.ReadOnly` |

Neo4j does not care about role name format; the `group_to_role_mapping` is pure string matching. When explicit mapping is configured, automatic name matching is disabled. A Keycloak role named `admin` would still require an explicit `"admin"=admin` entry. The `neo4j-` prefix makes this explicit mapping obvious and avoids that false expectation.

### Client roles vs. groups

The [Neo4j Support KB article on Keycloak SSO](https://support.neo4j.com/s/article/12694420673683) documents an approach using Keycloak groups with a "Group Membership" mapper. That approach is designed for interactive user SSO where users belong to organizational groups.

Client roles are the correct choice for M2M service accounts. They are scoped to a specific client (the Neo4j application), not to the realm. Service accounts are not users in the organizational sense and do not belong to groups. Client role assignment is the intended Keycloak pattern for machine identities. Group paths also include a leading `/` (e.g., `/admin`), adding quoting complexity to the role mapping string.

### Resulting token

A token acquired via client credentials grant from the configured realm contains:

```json
{
  "iss": "https://keycloak-xxx.azurecontainerapps.io/realms/neo4j",
  "aud": ["neo4j-client", "account"],
  "azp": "neo4j-client",
  "sub": "service-account-neo4j-client",
  "roles": ["neo4j-admin", "neo4j-readwrite", "neo4j-readonly"],
  "typ": "Bearer",
  "exp": 1234567890,
  "iat": 1234567800
}
```

The `roles` claim at the top level, the `neo4j-client` audience, and the `sub` claim identifying the service account are the three fields Neo4j reads. The structure is close enough to Entra that the Neo4j OIDC config differs only in the discovery URI, audience value, and role name strings.

## Component Walkthrough

### Keycloak Realm (`realm-export.json`)

The realm export is the single source of truth for Keycloak's configuration. It defines the `neo4j` realm, the `neo4j-client` confidential client with service accounts enabled, the three client roles, both protocol mappers, and the service account user with all roles pre-assigned. The `deploy.sh` script substitutes the client secret placeholder before building the image, so the export in the repository is a template rather than a deployable artifact.

### Docker Image (`Dockerfile`)

Three lines. Extends the stock Keycloak 26.5.6 image, copies in the realm export, and sets the entrypoint to `start-dev --import-realm`. The image is built inside Azure Container Registry via `az acr build`, so no local Docker daemon is required.

```dockerfile
FROM quay.io/keycloak/keycloak:26.5.6
COPY realm-export.json /opt/keycloak/data/import/realm-export.json
ENTRYPOINT ["/opt/keycloak/bin/kc.sh", "start-dev", "--import-realm"]
```

### Azure Infrastructure (Bicep)

The deployment runs in two stages because the Container App needs an image in ACR before it can start.

**Stage 1** (`registry.bicep`): Creates an Azure Container Registry (Basic SKU) with admin user disabled. The image is built and pushed to this registry between stages.

**Stage 2** (`main.bicep`): Orchestrates three modules.

`modules/identity.bicep` creates a user-assigned managed identity and grants it the `AcrPull` role on the registry. The Container App uses this identity to pull images without stored credentials.

`modules/environment.bicep` creates a Log Analytics workspace and a Container Apps environment. The environment's default domain is used to construct Keycloak's deterministic FQDN.

`modules/keycloak.bicep` creates the Container App itself. Key configuration:

- `KC_HOSTNAME` set to the deterministic FQDN (`https://{appName}.{defaultDomain}`), solving the chicken-and-egg problem where Keycloak needs to know its own URL before the app exists.
- `KC_PROXY_HEADERS=xforwarded` so Keycloak trusts the forwarded headers from Container Apps ingress.
- External HTTPS ingress on port 8080, insecure traffic disallowed.
- 1 CPU, 2 GiB memory, 1 replica. Keycloak is a Java application and needs the memory headroom.
- Health probes on port 9000: startup (30 attempts at 5-second intervals to accommodate Java startup time), liveness, and readiness.
- A `DEPLOYMENT_ID` environment variable that changes per deployment, forcing a new Container App revision and ensuring the latest image is pulled.

### Deployment Script (`deploy.sh`)

The script orchestrates the full lifecycle: `deploy`, `status`, `test`, `outputs`, `cleanup`. The deploy command creates the resource group, deploys ACR, generates a client secret, substitutes it into the realm export, builds the image, deploys the Container Apps stack, waits for Keycloak to become healthy, and saves all outputs to `.deployment.json`. The test command acquires a token via client credentials grant using `curl` and decodes the JWT to validate claims structure without any Python dependencies.

### Test Client (`keycloak-test-client/`)

A Python script (`test_keycloak.py`, managed with `uv`, `httpx`-based) that acquires a token from Keycloak's token endpoint and decodes the JWT to display all claims. It validates the fields Neo4j cares about: top-level `roles` claim, audience match, and issuer consistency with the discovery document. It works against both local Docker and Azure deployments via `--server-url` or `--azure` flags.

## Neo4j OIDC Configuration

### Side-by-side config blocks

The complete `neo4j.conf` OIDC block for each provider. The only differences are the discovery URI, audience value, display name, and role name strings in the mapping.

```properties
# Entra ID M2M
dbms.security.authentication_providers=oidc-m2m,native
dbms.security.authorization_providers=oidc-m2m,native
dbms.security.oidc.m2m.visible=false
dbms.security.oidc.m2m.display_name=Entra ID M2M
dbms.security.oidc.m2m.well_known_discovery_uri=https://login.microsoftonline.com/{tenant_id}/.well-known/openid-configuration
dbms.security.oidc.m2m.audience={audience}
dbms.security.oidc.m2m.claims.username=sub
dbms.security.oidc.m2m.claims.groups=roles
dbms.security.oidc.m2m.config=token_type_principal=access_token;token_type_authentication=access_token
dbms.security.oidc.m2m.authorization.group_to_role_mapping="Neo4j.Admin"=admin;"Neo4j.ReadWrite"=editor;"Neo4j.ReadOnly"=reader

# Keycloak M2M
dbms.security.authentication_providers=oidc-m2m,native
dbms.security.authorization_providers=oidc-m2m,native
dbms.security.oidc.m2m.visible=false
dbms.security.oidc.m2m.display_name=Keycloak M2M
dbms.security.oidc.m2m.well_known_discovery_uri=https://{keycloak_host}/realms/{realm}/.well-known/openid-configuration
dbms.security.oidc.m2m.audience=neo4j-client
dbms.security.oidc.m2m.claims.username=sub
dbms.security.oidc.m2m.claims.groups=roles
dbms.security.oidc.m2m.config=token_type_principal=access_token;token_type_authentication=access_token
dbms.security.oidc.m2m.authorization.group_to_role_mapping="neo4j-admin"=admin;"neo4j-readwrite"=editor;"neo4j-readonly"=reader
```

### Token type configuration

The `config` setting with `token_type_principal=access_token;token_type_authentication=access_token` is required for M2M because the OAuth 2.0 client credentials flow only issues access tokens. No ID token is returned per the specification (RFC 6749 Section 4.4). Omitting this setting or using `id_token` causes authentication to fail silently for service-to-service workloads.

### The `visible` setting

Setting `visible=false` hides the OIDC provider from the Neo4j Browser login screen. This is the correct configuration for M2M providers that have no interactive login flow; without it, Browser shows a non-functional SSO login button.

This setting is not listed in the Neo4j Operations Manual (it is absent from the 18 documented OIDC provider settings). Evidence that it works: [Neo4j Browser PR #1948](https://github.com/neo4j/neo4j-browser/pull/1948) ("Respect `visible` field in SSO provider configuration") was merged November 2023 and shipped in Browser 5.15.0. The implementation filters providers where `visible === false` from the login screen, defaulting to `true` when absent. The setting is available in Neo4j 5.16+ and all 2025/2026 releases.

### Deployment outputs

The Keycloak Bicep deployment exports every value needed for Neo4j OIDC configuration:

| Output | Value |
|---|---|
| `oidcDiscoveryUri` | `https://keycloak-xxx.azurecontainerapps.io/realms/neo4j/.well-known/openid-configuration` |
| `oidcTokenEndpoint` | `https://keycloak-xxx.azurecontainerapps.io/realms/neo4j/protocol/openid-connect/token` |
| `oidcAudience` | `neo4j-client` |
| `oidcClientId` | `neo4j-client` |
| `oidcRoleMapping` | `"neo4j-admin"=admin;"neo4j-readwrite"=editor;"neo4j-readonly"=reader` |
| `oidcTokenTypeConfig` | `token_type_principal=access_token;token_type_authentication=access_token` |
| `oidcDisplayName` | `Keycloak M2M` |

The client secret is not in the Bicep output. It is generated by `deploy.sh` and stored in `.deployment.json`.

### Complete OIDC settings reference (Neo4j 2026.02)

The 18 documented settings plus the undocumented `visible` setting:

| Setting | Default | Dynamic | Notes |
|---|---|---|---|
| `display_name` | (empty) | No | Shown on Browser/Bloom login screen |
| `visible` | `true` | Yes | **Undocumented.** Hides provider from login when `false` |
| `auth_flow` | `pkce` | No | `pkce` or `implicit` |
| `well_known_discovery_uri` | (empty) | Yes | OIDC discovery URL |
| `auth_endpoint` | (empty) | Yes | Authorization endpoint |
| `auth_params` | (empty) | Yes | Semicolon-separated key-value pairs |
| `token_endpoint` | (empty) | Yes | Token endpoint |
| `token_params` | (empty) | Yes | Semicolon-separated key-value pairs |
| `jwks_uri` | (empty) | Yes | JSON Web Key Set URL |
| `user_info_uri` | (empty) | Yes | UserInfo endpoint |
| `issuer` | (empty) | Yes | Expected `iss` claim value |
| `audience` | (empty) | Yes | Expected `aud` claim value |
| `params` | (empty) | Yes | e.g., `client_id=x;response_type=code;scope=openid` |
| `config` | (empty) | Yes | e.g., `token_type_principal=access_token;...` |
| `get_groups_from_user_info` | `false` | Yes | Fetch groups from UserInfo instead of token |
| `get_username_from_user_info` | `false` | Yes | Fetch username from UserInfo instead of token |
| `claims.username` | `sub` | Yes | JWT claim for database username |
| `claims.groups` | (empty) | Yes | JWT claim for database roles (top-level only) |
| `authorization.group_to_role_mapping` | (empty) | Yes | Semicolon-separated IdP group to Neo4j role mapping |

All dynamic settings can be changed at runtime without restarting Neo4j. The global setting `dbms.security.logs.oidc.jwt_claims_at_debug_level_enabled` (default `false`, not dynamic) logs JWT claims at DEBUG level in the security log, useful for troubleshooting token issues.

## CLI Integration (Phase 5)

The deployment CLI supports Keycloak as a first-class OIDC provider alongside Entra ID. The integration touches four areas: data model, setup wizard, OIDC config generation, and token validation.

### Data model (`models.py`)

`M2MSettings` carries both Entra-specific and Keycloak/generic OIDC fields on the same model. A `provider_type` field (`"entra"` or `"keycloak"`) controls which fields the pipeline reads. Existing configs without `provider_type` default to Entra. The Keycloak fields: `discovery_uri`, `token_endpoint`, `client_id`, `client_secret`, `username_claim`, `groups_claim`, `role_mapping`, `token_type_config`, `display_name`, `oidc_visible`.

### Setup wizard (`m2m_setup.py`)

The M2M setup step is a three-way selection:

1. **No M2M authentication**
2. **Keycloak** — reads `keycloak-infra/.deployment.json` automatically. If not found at the default path, prompts for a file path. Shows a summary table of loaded values and a preview of the `neo4j.conf` OIDC block. Validates the discovery URI with an HTTP GET before saving.
3. **Entra ID** — existing automatic and manual flows, unchanged.

When the user selects Keycloak, every OIDC value is populated from `.deployment.json` with no manual entry required. The client secret is saved to `settings.yaml` (acceptable for a demo where the secret is a known value generated per deployment).

### OIDC config generation (`deployment.py`)

`_generate_oidc_config` checks `provider_type`. When `"keycloak"`, it builds the config block from the generic fields (`discovery_uri`, `audience`, `role_mapping`, `username_claim`, `groups_claim`, `token_type_config`, `display_name`, `oidc_visible`) instead of constructing Entra-specific values from a tenant ID. The generated string passes through the Bicep `replace()` chain and cloud-init `echo -e` expansion unchanged — the same escaping path proven by the Entra role mapping (which also contains double quotes and semicolons).

`generate_neo4j_oidc_config` in `m2m_setup.py` uses the same branching logic to show the user a preview during setup.

### Deployment info (`bicep_deploy.py`)

`save_deployment_details` writes an `m2m_auth` block to `.deployments/{scenario}-{engine}.json` (e.g. `standalone-v2025-bicep.json`). When `provider_type` is `"keycloak"`, the block includes `provider_type`, `discovery_uri`, `token_endpoint`, `audience`, `client_id`, `client_secret`, `role_mapping`, and `display_name`. The validation script reads this file to know how and where to acquire tokens.

### Token validation (`validate_bearer.py`)

The script detects `provider_type` from the deployment info JSON and routes to one of two token acquisition functions:

- **`get_bearer_token_oidc`** (Keycloak): Plain HTTP POST to the token endpoint with `grant_type=client_credentials`, `client_id`, and `client_secret`. Uses the existing `requests` dependency. Reads `client_secret` from the deployment info (or `NEO4J_CLIENT_SECRET` env var as fallback).
- **`get_bearer_token_entra`** (Entra ID): MSAL `ConfidentialClientApplication`, unchanged from the original implementation. MSAL import is deferred to this path only.

The rest of the script — JWT decoding, Neo4j connection with `bearer_auth()`, role verification via `SHOW CURRENT USER` — is provider-agnostic.

### `.deployment.json` format

Produced by `deploy.sh`, consumed by the setup wizard and validation script:

```json
{
  "resource_group": "rg-keycloak-...",
  "location": "eastus",
  "keycloak_url": "https://keycloak-xxx.azurecontainerapps.io",
  "admin_console_url": "https://keycloak-xxx.azurecontainerapps.io/admin",
  "acr_name": "acrkeycloak...",
  "oidc": {
    "discovery_uri": "https://keycloak-xxx.azurecontainerapps.io/realms/neo4j/.well-known/openid-configuration",
    "token_endpoint": "https://keycloak-xxx.azurecontainerapps.io/realms/neo4j/protocol/openid-connect/token",
    "audience": "neo4j-client",
    "client_id": "neo4j-client",
    "client_secret": "<generated-uuid>",
    "role_mapping": "\"neo4j-admin\"=admin;\"neo4j-readwrite\"=editor;\"neo4j-readonly\"=reader",
    "token_type_config": "token_type_principal=access_token;token_type_authentication=access_token",
    "display_name": "Keycloak M2M",
    "visible": false
  }
}
```

### End-to-end deployment sequence

```bash
# 1. Deploy Keycloak to Azure
cd keycloak-infra && ./deploy.sh deploy

# 2. Configure Neo4j with Keycloak OIDC
cd deployments && uv run bicep-deploy setup    # Select "Keycloak" at M2M step

# 3. Deploy Neo4j
uv run bicep-deploy deploy --scenario standalone-v2025

# 4. Validate bearer token auth
cd ../validate-bearer-token
uv run validate_bearer.py --scenario standalone-v2025
```

## Reference Links

**Neo4j OIDC and SSO:**
- [SSO Integration](https://neo4j.com/docs/operations-manual/current/authentication-authorization/sso-integration/)
- [Map IdP groups to Neo4j roles](https://neo4j.com/docs/operations-manual/current/authentication-authorization/sso-integration/#auth-sso-map-idp-roles)
- [SSO Configuration Tutorial](https://neo4j.com/docs/operations-manual/current/tutorial/tutorial-sso-configuration/)
- [Nested claims issue (neo4j/neo4j#13096)](https://github.com/neo4j/neo4j/issues/13096)
- [Browser `visible` setting (neo4j/neo4j-browser#1948)](https://github.com/neo4j/neo4j-browser/pull/1948)
- [Neo4j Support KB: Keycloak SSO](https://support.neo4j.com/s/article/12694420673683)
- [Reference implementation (ikwattro/neo4j-sso-keycloak)](https://github.com/ikwattro/neo4j-sso-keycloak)

**Neo4j infrastructure:**
- [Configuration settings](https://neo4j.com/docs/operations-manual/current/configuration/configuration-settings/)
- [Breaking changes in 2025.01](https://neo4j.com/docs/operations-manual/current/changes-deprecations-removals/)
