"""
NSG rule checker.

Verifies that the NSG on the Neo4j subnet has an Allow rule for Bolt (7687)
and the AzureLoadBalancer probe tag rule that lets health checks reach VMs.
"""

import json

from .base import TestResult, _az


class NSGChecker:
    def __init__(self, neo4j_rg: str) -> None:
        self.neo4j_rg = neo4j_rg

    def check_bolt_rule(self) -> TestResult:
        rc, out, _ = _az(
            "network", "nsg", "list", "-g", self.neo4j_rg,
            "--query",
            "[0].securityRules[?destinationPortRange=='7687'].{access:access,source:sourceAddressPrefix}",
            "-o", "json",
        )
        if rc != 0:
            return TestResult("NSG Bolt rule (7687)", False, "az query failed")
        rules = json.loads(out) if out else []
        allow = [r for r in rules if r.get("access") == "Allow"]
        if allow:
            sources = ", ".join(r.get("source", "") for r in allow)
            return TestResult("NSG Bolt rule (7687)", True, f"Allow from: {sources}")
        return TestResult("NSG Bolt rule (7687)", False, "No Allow rule for 7687")

    def check_alb_probe_rule(self) -> TestResult:
        rc, out, _ = _az(
            "network", "nsg", "list", "-g", self.neo4j_rg,
            "--query",
            "[0].securityRules[?sourceAddressPrefix=='AzureLoadBalancer']"
            ".{name:name,priority:priority,access:access}",
            "-o", "json",
        )
        if rc != 0:
            return TestResult("NSG AzureLoadBalancerProbe rule", False, "az query failed")
        rules = json.loads(out) if out else []
        allow = [r for r in rules if r.get("access") == "Allow"]
        if allow:
            r = allow[0]
            return TestResult(
                "NSG AzureLoadBalancerProbe rule",
                True,
                f"name={r.get('name')} priority={r.get('priority')}",
            )
        return TestResult("NSG AzureLoadBalancerProbe rule", False, "No AzureLoadBalancer Allow rule")

    def run(self) -> list[TestResult]:
        return [self.check_bolt_rule(), self.check_alb_probe_rule()]
