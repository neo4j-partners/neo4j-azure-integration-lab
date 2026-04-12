import socket
import sys

domain_name = sys.argv[1]
bolt_uri = sys.argv[2]
username = sys.argv[3]
password = sys.argv[4]

# --- DNS resolution ---
try:
    socket.getaddrinfo(domain_name, 7687)
    print("PASS:DNS")
except Exception as e:
    msg = str(e).replace("\n", " ")[:120]
    print(f"FAIL:DNS:{msg}")

# --- TCP probe 7687 ---
try:
    s = socket.create_connection((domain_name, 7687), timeout=10)
    s.close()
    print("PASS:7687")
except Exception as e:
    msg = str(e).replace("\n", " ")[:120]
    print(f"FAIL:7687:{msg}")

# --- TCP probe 7474 ---
try:
    s = socket.create_connection((domain_name, 7474), timeout=10)
    s.close()
    print("PASS:7474")
except Exception as e:
    msg = str(e).replace("\n", " ")[:120]
    print(f"FAIL:7474:{msg}")

# --- Bolt driver ---
driver = None
try:
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(bolt_uri, auth=(username, password))
    with driver.session() as session:
        session.run("RETURN 1").consume()
    print("PASS:BOLT")
except Exception as e:
    msg = str(e).replace("\n", " ")[:120]
    print(f"FAIL:BOLT:{msg}")
finally:
    if driver is not None:
        driver.close()

# --- Topology ---
driver = None
try:
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(bolt_uri, auth=(username, password))
    with driver.session(database="system") as session:
        result = session.run("SHOW SERVERS")
        servers = result.data()
    enabled = [s for s in servers if s.get("state", "").upper() == "ENABLED"]
    if len(enabled) >= 1:
        print(f"PASS:TOPOLOGY:{len(enabled)}")
    else:
        print(f"FAIL:TOPOLOGY:0 enabled servers out of {len(servers)}")
except Exception as e:
    msg = str(e).replace("\n", " ")[:120]
    print(f"FAIL:TOPOLOGY:{msg}")
finally:
    if driver is not None:
        driver.close()
