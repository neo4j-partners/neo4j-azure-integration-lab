"""
Bolt connectivity checker.

Tests the LB's HTTP endpoint on 7474, TCP reachability on 7687, and a
full end-to-end Bolt query through the LB using cypher-shell. All checks
run via VMSS run-command from inside the Neo4j VNet.
"""

from .base import TestResult, _get_vmss, _vmss_run


class BoltChecker:
    def __init__(self, neo4j_rg: str, lb_ip: str, username: str, password: str) -> None:
        self.neo4j_rg = neo4j_rg
        self.lb_ip = lb_ip
        self.username = username
        self.password = password

    def _check_lb_http(self, name: str, ids: list[str]) -> TestResult:
        for iid in ids:
            ok, out = _vmss_run(
                self.neo4j_rg, name, iid,
                f"curl -sf --max-time 5 http://{self.lb_ip}:7474/ -o /dev/null -w '%{{http_code}}'",
            )
            if ok and "200" in out:
                return TestResult("LB HTTP 7474", True, f"HTTP 200 via instance {iid}")
        return TestResult("LB HTTP 7474", False, f"All instances failed to reach {self.lb_ip}:7474")

    def _check_lb_tcp(self, name: str, ids: list[str]) -> TestResult:
        for iid in ids:
            ok, out = _vmss_run(
                self.neo4j_rg, name, iid,
                f"timeout 5 bash -c 'echo > /dev/tcp/{self.lb_ip}/7687' && echo OK || echo FAIL",
            )
            if ok and "OK" in out:
                return TestResult("LB TCP 7687", True, f"TCP connected via instance {iid}")
        return TestResult("LB TCP 7687", False, f"All instances failed TCP to {self.lb_ip}:7687")

    def _check_bolt_end_to_end(self, name: str, ids: list[str]) -> TestResult:
        pw_safe = self.password.replace("'", "'\"'\"'")
        for iid in ids:
            ok, out = _vmss_run(
                self.neo4j_rg, name, iid,
                f"cypher-shell -a neo4j://{self.lb_ip}:7687 -u {self.username} -p '{pw_safe}'"
                f" 'RETURN 1 AS result' 2>/dev/null",
            )
            if ok and "1" in out:
                return TestResult("Bolt end-to-end (RETURN 1)", True, f"RETURN 1 = 1 via instance {iid}")
        return TestResult("Bolt end-to-end (RETURN 1)", False, "cypher-shell failed on all instances")

    def run(self) -> list[TestResult]:
        name, ids = _get_vmss(self.neo4j_rg)
        if not name:
            return [
                TestResult("LB HTTP 7474", False, "VMSS not found"),
                TestResult("LB TCP 7687", False, "VMSS not found"),
                TestResult("Bolt end-to-end (RETURN 1)", False, "VMSS not found"),
            ]
        if not ids:
            return [
                TestResult("LB HTTP 7474", False, "No VMSS instances found"),
                TestResult("LB TCP 7687", False, "No VMSS instances found"),
                TestResult("Bolt end-to-end (RETURN 1)", False, "No VMSS instances found"),
            ]
        return [
            self._check_lb_http(name, ids),
            self._check_lb_tcp(name, ids),
            self._check_bolt_end_to_end(name, ids),
        ]
