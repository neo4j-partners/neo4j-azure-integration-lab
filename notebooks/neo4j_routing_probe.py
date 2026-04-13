import sys
from neo4j import GraphDatabase

domain_name, bolt_uri, username, password = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
routing_uri = bolt_uri.replace("bolt://", "neo4j://")

# Test 1: bolt:// direct mode (expected: success)
try:
    with GraphDatabase.driver(bolt_uri, auth=(username, password)) as driver:
        driver.verify_connectivity()
    print("PASS:BOLT_DIRECT")
except Exception as e:
    print(f"FAIL:BOLT_DIRECT:{e}")

# Test 2: neo4j:// routing mode (expected: failure if VMSS IPs are in the routing table)
try:
    with GraphDatabase.driver(routing_uri, auth=(username, password)) as driver:
        driver.verify_connectivity()
    print("PASS:ROUTING")
except Exception as e:
    print(f"FAIL:ROUTING:{e}")
