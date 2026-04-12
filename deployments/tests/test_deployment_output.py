"""
Unit tests for src/deployment_output.py.

Tests model defaults, field assignment, JSON serialization, and the
display_connection_info renderer across all deployment states.
"""

import json
from unittest.mock import patch

import pytest

from src.deployment_output import (
    ConfigurationJSON,
    ConnectionJSON,
    DeploymentJSON,
    NetworkJSON,
    SSHJSON,
    display_connection_info,
    write_deployment_json,
)


# ---------------------------------------------------------------------------
# ConnectionJSON
# ---------------------------------------------------------------------------

class TestConnectionJSON:
    def test_defaults(self):
        c = ConnectionJSON()
        assert c.neo4j_uri == ""
        assert c.browser_url == ""
        assert c.username == "neo4j"
        assert c.password == ""
        assert c.neo4j_database == "neo4j"
        assert c.lb_private_ip is None
        assert c.databricks_bolt_uri is None
        assert c.databricks_workspace_url is None
        assert c.databricks_workspace_host is None
        assert c.bloom_url is None

    def test_field_assignment(self):
        c = ConnectionJSON(
            neo4j_uri="bolt://10.0.0.1:7687",
            browser_url="http://10.0.0.1:7474",
            username="admin",
            password="s3cr3t",
            lb_private_ip="10.0.0.1",
            bloom_url="http://10.0.0.1:7474/bloom",
        )
        assert c.neo4j_uri == "bolt://10.0.0.1:7687"
        assert c.browser_url == "http://10.0.0.1:7474"
        assert c.username == "admin"
        assert c.lb_private_ip == "10.0.0.1"
        assert c.bloom_url == "http://10.0.0.1:7474/bloom"

    def test_databricks_fields(self):
        c = ConnectionJSON(
            databricks_bolt_uri="bolt://10.0.0.1:7687",
            databricks_workspace_url="https://adb-123.azuredatabricks.net",
            databricks_workspace_host="adb-123.azuredatabricks.net",
        )
        assert c.databricks_bolt_uri == "bolt://10.0.0.1:7687"
        assert c.databricks_workspace_url == "https://adb-123.azuredatabricks.net"
        assert c.databricks_workspace_host == "adb-123.azuredatabricks.net"


# ---------------------------------------------------------------------------
# SSHJSON
# ---------------------------------------------------------------------------

class TestSSHJSON:
    def test_defaults(self):
        s = SSHJSON()
        assert s.hostname is None
        assert s.username == "neo4j"
        assert s.command is None

    def test_with_all_fields(self):
        s = SSHJSON(hostname="10.0.0.1", username="azureuser", command="ssh azureuser@10.0.0.1")
        assert s.hostname == "10.0.0.1"
        assert s.username == "azureuser"
        assert s.command == "ssh azureuser@10.0.0.1"


# ---------------------------------------------------------------------------
# ConfigurationJSON
# ---------------------------------------------------------------------------

class TestConfigurationJSON:
    def test_defaults(self):
        c = ConfigurationJSON()
        assert c.license_type == "Enterprise"
        assert c.node_count == 1

    def test_cluster(self):
        c = ConfigurationJSON(license_type="Enterprise", node_count=3)
        assert c.node_count == 3

    def test_evaluation_license(self):
        c = ConfigurationJSON(license_type="Evaluation")
        assert c.license_type == "Evaluation"


# ---------------------------------------------------------------------------
# NetworkJSON
# ---------------------------------------------------------------------------

class TestNetworkJSON:
    def test_defaults(self):
        n = NetworkJSON()
        assert n.vnet_id == ""
        assert n.nsg_id == ""
        assert n.lb_private_ip == ""
        assert n.private_link_service_id == ""
        assert n.databricks_vnet_id is None

    def test_with_databricks_vnet(self):
        n = NetworkJSON(
            vnet_id="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet",
            databricks_vnet_id="/subscriptions/sub/resourceGroups/dbx-rg/providers/Microsoft.Network/virtualNetworks/dbx-vnet",
        )
        assert "virtualNetworks/vnet" in n.vnet_id
        assert n.databricks_vnet_id is not None


# ---------------------------------------------------------------------------
# DeploymentJSON
# ---------------------------------------------------------------------------

def _minimal_deployment(**overrides) -> DeploymentJSON:
    defaults = dict(
        scenario="standalone-v2025",
        engine="bicep",
        state="complete",
        resource_group="neo4j-test-abc",
        neo4j_resource_group="neo4j-test-abc",
        created_at="2026-04-12T10:00:00",
    )
    defaults.update(overrides)
    return DeploymentJSON(**defaults)


class TestDeploymentJSON:
    def test_required_fields(self):
        d = _minimal_deployment()
        assert d.scenario == "standalone-v2025"
        assert d.engine == "bicep"
        assert d.state == "complete"
        assert d.resource_group == "neo4j-test-abc"
        assert d.neo4j_resource_group == "neo4j-test-abc"
        assert d.created_at == "2026-04-12T10:00:00"

    def test_nested_defaults(self):
        d = _minimal_deployment()
        assert isinstance(d.connection, ConnectionJSON)
        assert isinstance(d.ssh, SSHJSON)
        assert isinstance(d.configuration, ConfigurationJSON)
        assert isinstance(d.network, NetworkJSON)
        assert d.m2m_auth is None
        assert d.serverless is None
        assert d.deployment_id is None
        assert d.databricks_resource_group is None
        assert d.databricks_managed_resource_group is None

    def test_optional_resource_groups(self):
        d = _minimal_deployment(
            databricks_resource_group="dbx-rg",
            databricks_managed_resource_group="dbx-managed-rg",
        )
        assert d.databricks_resource_group == "dbx-rg"
        assert d.databricks_managed_resource_group == "dbx-managed-rg"

    def test_nested_models_independent(self):
        """Two DeploymentJSON instances must not share nested model instances."""
        d1 = _minimal_deployment()
        d2 = _minimal_deployment()
        d1.connection.password = "changed"
        assert d2.connection.password == ""

    def test_m2m_auth_dict(self):
        d = _minimal_deployment(m2m_auth={"client_id": "abc", "tenant_id": "xyz"})
        assert d.m2m_auth["client_id"] == "abc"

    def test_serverless_dict(self):
        d = _minimal_deployment(serverless={"bolt_uri": "bolt://pls.azure.com:7687", "ncc_configured": True})
        assert d.serverless["ncc_configured"] is True

    def test_model_dump_roundtrip(self):
        d = _minimal_deployment(
            connection=ConnectionJSON(neo4j_uri="bolt://10.0.0.1:7687", password="s3cr3t"),
            configuration=ConfigurationJSON(node_count=3),
        )
        dumped = d.model_dump(mode="json")
        assert dumped["connection"]["neo4j_uri"] == "bolt://10.0.0.1:7687"
        assert dumped["configuration"]["node_count"] == 3
        assert dumped["scenario"] == "standalone-v2025"

    def test_ansible_engine(self):
        d = _minimal_deployment(engine="ansible")
        assert d.engine == "ansible"


# ---------------------------------------------------------------------------
# write_deployment_json
# ---------------------------------------------------------------------------

class TestWriteDeploymentJSON:
    def test_creates_file(self, tmp_path):
        d = _minimal_deployment()
        out = tmp_path / "test-bicep.json"
        write_deployment_json(d, out)
        assert out.exists()

    def test_json_is_valid(self, tmp_path):
        d = _minimal_deployment(connection=ConnectionJSON(neo4j_uri="bolt://10.0.0.1:7687"))
        out = tmp_path / "test-bicep.json"
        write_deployment_json(d, out)
        with open(out) as f:
            data = json.load(f)
        assert data["scenario"] == "standalone-v2025"
        assert data["connection"]["neo4j_uri"] == "bolt://10.0.0.1:7687"

    def test_creates_parent_dirs(self, tmp_path):
        d = _minimal_deployment()
        out = tmp_path / "a" / "b" / "c" / "test-bicep.json"
        write_deployment_json(d, out)
        assert out.exists()

    def test_pretty_printed(self, tmp_path):
        d = _minimal_deployment()
        out = tmp_path / "test-bicep.json"
        write_deployment_json(d, out)
        content = out.read_text()
        assert "\n" in content
        assert "  " in content  # 2-space indent

    def test_overwrites_existing(self, tmp_path):
        out = tmp_path / "test-bicep.json"
        write_deployment_json(_minimal_deployment(state="partial"), out)
        write_deployment_json(_minimal_deployment(state="complete"), out)
        with open(out) as f:
            data = json.load(f)
        assert data["state"] == "complete"

    def test_null_optionals_included(self, tmp_path):
        """Optional None fields should serialize to null (not omitted) for schema consistency."""
        d = _minimal_deployment()
        out = tmp_path / "test.json"
        write_deployment_json(d, out)
        with open(out) as f:
            data = json.load(f)
        assert "deployment_id" in data
        assert data["deployment_id"] is None


# ---------------------------------------------------------------------------
# display_connection_info
# ---------------------------------------------------------------------------

class TestDisplayConnectionInfo:
    """
    display_connection_info renders to Rich console.
    Tests verify it handles all deployment states without raising.
    """

    def _call(self, details: dict) -> None:
        with patch("src.deployment_output.console"):
            display_connection_info(details, "test-scenario")

    def test_complete_standalone(self):
        self._call({
            "state": "complete",
            "connection": {
                "browser_url": "http://10.0.0.1:7474",
                "neo4j_uri": "bolt://10.0.0.1:7687",
                "username": "neo4j",
                "password": "s3cr3t",
            },
            "ssh": {},
            "configuration": {"license_type": "Enterprise", "node_count": 1},
        })

    def test_complete_cluster(self):
        self._call({
            "state": "complete",
            "connection": {
                "neo4j_uri": "bolt://10.0.0.1:7687",
                "username": "neo4j",
                "password": "s3cr3t",
                "lb_private_ip": "10.0.0.1",
            },
            "ssh": {"command": "ssh neo4j@10.0.0.1"},
            "configuration": {"license_type": "Enterprise", "node_count": 3},
        })

    def test_partial_state(self):
        self._call({
            "state": "partial",
            "neo4j_resource_group": "neo4j-rg",
            "databricks_resource_group": "dbx-rg",
            "connection": {},
            "ssh": {},
            "configuration": {},
        })

    def test_databricks_peering(self):
        self._call({
            "state": "complete",
            "connection": {
                "neo4j_uri": "bolt://10.0.0.1:7687",
                "username": "neo4j",
                "password": "s3cr3t",
                "databricks_bolt_uri": "bolt://10.0.0.1:7687",
                "databricks_workspace_url": "https://adb-123.azuredatabricks.net",
            },
            "ssh": {},
            "configuration": {"license_type": "Enterprise", "node_count": 1},
        })

    def test_serverless_bolt_uri(self):
        self._call({
            "state": "complete",
            "connection": {
                "neo4j_uri": "bolt://10.0.0.1:7687",
                "username": "neo4j",
                "password": "s3cr3t",
            },
            "ssh": {},
            "configuration": {"license_type": "Enterprise", "node_count": 1},
            "serverless": {"bolt_uri": "bolt://neo4j.privatelink.azure.com:7687", "ncc_configured": True},
        })

    def test_bloom_url(self):
        self._call({
            "state": "complete",
            "connection": {
                "neo4j_uri": "bolt://10.0.0.1:7687",
                "username": "neo4j",
                "password": "s3cr3t",
                "bloom_url": "http://10.0.0.1:7474/bloom",
            },
            "ssh": {},
            "configuration": {"license_type": "Enterprise", "node_count": 1},
        })

    def test_empty_details(self):
        """Minimal dict should not raise — handles missing keys gracefully."""
        self._call({"state": "complete", "connection": {}, "ssh": {}, "configuration": {}})

    def test_missing_top_level_keys(self):
        """Missing keys (e.g. no 'ssh') should not raise."""
        self._call({"state": "complete", "connection": {}})
