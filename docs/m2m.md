# M2M Bearer Token Authentication

The setup wizard includes an optional step to configure M2M (Machine-to-Machine) authentication. This allows services, APIs, and automated processes to connect to Neo4j using OAuth 2.0 bearer tokens. Two OIDC providers are supported:

- **Keycloak** — Deploy Keycloak to Azure Container Apps, then the wizard reads OIDC values from the Keycloak deployment automatically
- **Microsoft Entra ID** — Create app registrations via Azure CLI (automatic) or enter values manually

During setup, the wizard presents three options:

1. No M2M authentication
2. Keycloak (reads from `keycloak-infra/.deployment.json`)
3. Entra ID (automatic or manual Azure AD app registration)

## Keycloak Setup

Deploy Keycloak first, then run the Neo4j deployment wizard:

```bash
# Deploy Keycloak to Azure Container Apps
cd keycloak-infra
./deploy.sh deploy

# Verify Keycloak is running and tokens work
./deploy.sh test

# Run Neo4j setup — select "Keycloak" at the M2M step
cd ../deployments
uv run bicep-deploy setup

# Deploy Neo4j (OIDC config is injected into neo4j.conf via cloud-init)
uv run bicep-deploy deploy --scenario standalone-v2025
```

The wizard reads `keycloak-infra/.deployment.json` and configures the OIDC provider, discovery URI, audience, role mapping, and client credentials automatically.

See [keycloak-infra/README.md](../keycloak-infra/README.md) for Keycloak deployment details.

## Entra ID Setup

The wizard uses Azure CLI (`az`) commands to:

1. Detect your Azure tenant from your current `az login` session
2. Create an API app registration with Neo4j roles (`Neo4j.Admin`, `Neo4j.ReadWrite`, `Neo4j.ReadOnly`)
3. Create a client app registration and generate a client secret
4. Grant API permissions and assign roles

## Validating Bearer Token Authentication

**Python (both providers):**

```bash
cd validate-bearer-token
uv run validate_bearer.py --scenario standalone-v2025
```

The script detects the provider type from `.deployments/standalone-v2025-bicep.json` (or `-ansible.json`) and acquires a token from the correct endpoint.

**Java JDBC (Keycloak):**

```bash
cd oauth-java
./run.sh
```

The script reads deployment info, acquires a Keycloak token, and connects to Neo4j using the [Neo4j JDBC driver](https://github.com/neo4j/neo4j-jdbc) with `authScheme=bearer`. It verifies the connection and displays the OIDC-mapped roles.
