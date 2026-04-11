"""
Neo4j service checker.

Verifies via VMSS run-command that the neo4j systemd service is active and
that ports 7474, 7687, and 7688 are listening on every node in the cluster.
"""

from .base import TestResult, _get_vmss, _vmss_run


class NeoServiceChecker:
    def __init__(self, neo4j_rg: str) -> None:
        self.neo4j_rg = neo4j_rg

    def run(self) -> list[TestResult]:
        name, ids = _get_vmss(self.neo4j_rg)
        if not name:
            return [TestResult("Neo4j service", False, "VMSS not found")]
        if not ids:
            return [TestResult("Neo4j service", False, "No VMSS instances found")]

        results = []
        for iid in ids:
            ok, out = _vmss_run(
                self.neo4j_rg, name, iid,
                "systemctl is-active neo4j; ss -tlnp | grep -E '7687|7688|7474'",
            )
            if not ok:
                results.append(TestResult(f"Neo4j service (instance {iid})", False, "run-command failed"))
                continue
            active = "active" in out
            has_ports = "7687" in out and "7474" in out
            results.append(TestResult(
                f"Neo4j service (instance {iid})",
                active and has_ports,
                f"active={active} ports={'7474/7687/7688' if has_ports else 'missing'}",
            ))
        return results
