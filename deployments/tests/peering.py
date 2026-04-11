"""
VNet peering state checker.

Verifies that both the Neo4j → Databricks and Databricks → Neo4j peering
links are Connected and FullyInSync.
"""

import json

from .base import TestResult, _az


class PeeringChecker:
    def __init__(self, neo4j_rg: str, dbx_rg: str) -> None:
        self.neo4j_rg = neo4j_rg
        self.dbx_rg = dbx_rg

    def _check_peering(self, rg: str, vnet_name: str, label: str) -> TestResult:
        rc, out, _ = _az(
            "network", "vnet", "peering", "list",
            "-g", rg, "--vnet-name", vnet_name,
            "--query", "[0].{state:peeringState,sync:peeringSyncLevel}",
            "-o", "json",
        )
        if rc != 0 or not out:
            return TestResult(label, False, "az query failed")
        p = json.loads(out)
        ok = p.get("state") == "Connected" and p.get("sync") == "FullyInSync"
        return TestResult(label, ok, f"state={p.get('state')} sync={p.get('sync')}")

    def run(self) -> list[TestResult]:
        rc, neo4j_vnet, _ = _az(
            "network", "vnet", "list", "-g", self.neo4j_rg, "--query", "[0].name", "-o", "tsv"
        )
        if rc != 0 or not neo4j_vnet:
            return [
                TestResult("VNet peering (neo4j→dbx)", False, "Neo4j VNet not found"),
                TestResult("VNet peering (dbx→neo4j)", False, "Neo4j VNet not found"),
            ]

        rc2, dbx_vnet, _ = _az(
            "network", "vnet", "list", "-g", self.dbx_rg, "--query", "[0].name", "-o", "tsv"
        )

        return [
            self._check_peering(self.neo4j_rg, neo4j_vnet, "VNet peering (neo4j→dbx)"),
            self._check_peering(self.dbx_rg, dbx_vnet, "VNet peering (dbx→neo4j)")
            if rc2 == 0 and dbx_vnet
            else TestResult("VNet peering (dbx→neo4j)", False, "Databricks VNet not found"),
        ]
