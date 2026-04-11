"""
VMSS instance state and LB probe configuration checker.

Verifies that all VMSS instances are in Succeeded state and that no LB probe
uses raw TCP on Bolt ports (7687/7688), which causes the health-check race
condition documented in no-connect.md.
"""

import json

from .base import TestResult, _az


class VMSSChecker:
    def __init__(self, neo4j_rg: str) -> None:
        self.neo4j_rg = neo4j_rg

    def _get_lb_name(self) -> str:
        rc, name, _ = _az(
            "network", "lb", "list", "-g", self.neo4j_rg, "--query", "[0].name", "-o", "tsv"
        )
        return name if rc == 0 else ""

    def check_instance_states(self) -> TestResult:
        rc, name, _ = _az(
            "vmss", "list", "-g", self.neo4j_rg, "--query", "[0].name", "-o", "tsv"
        )
        if rc != 0 or not name:
            return TestResult("VMSS instances state", False, "VMSS not found")
        rc2, out, _ = _az(
            "vmss", "list-instances", "-g", self.neo4j_rg, "-n", name,
            "--query", "[].provisioningState", "-o", "json",
        )
        if rc2 != 0:
            return TestResult("VMSS instances state", False, "az query failed")
        states = json.loads(out) if out else []
        ok = all(s == "Succeeded" for s in states)
        return TestResult(
            "VMSS instances state",
            ok,
            f"{len(states)} instances: {', '.join(states)}",
        )

    def check_lb_probes(self) -> TestResult:
        lb = self._get_lb_name()
        if not lb:
            return TestResult("LB probe configuration", False, "LB not found")
        rc, out, _ = _az(
            "network", "lb", "probe", "list",
            "-g", self.neo4j_rg, "--lb-name", lb,
            "--query", "[].{name:name,protocol:protocol,port:port}",
            "-o", "json",
        )
        if rc != 0 or not out:
            return TestResult("LB probe configuration", False, "az query failed")
        probes = json.loads(out)
        bad = [p for p in probes if p.get("port") in [7687, 7688] and p.get("protocol") == "Tcp"]
        if bad:
            names = [p["name"] for p in bad]
            return TestResult("LB probe configuration", False, f"TCP probes on Bolt ports: {names}")
        return TestResult("LB probe configuration", True, f"{len(probes)} probes, none TCP on 7687/7688")

    def run(self) -> list[TestResult]:
        return [self.check_instance_states(), self.check_lb_probes()]
