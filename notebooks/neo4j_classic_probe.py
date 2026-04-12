import socket
import sys

lb_ip = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
username = sys.argv[2] if len(sys.argv) > 2 else "neo4j"
password = sys.argv[3] if len(sys.argv) > 3 else ""

# --- TCP probes ---
for port in [7687, 7474]:
    try:
        s = socket.create_connection((lb_ip, port), timeout=10)
        s.close()
        print(f"PASS:{port}")
    except Exception as e:
        msg = str(e).replace("\n", " ")[:120]
        print(f"FAIL:{port}:{msg}")

# --- Bolt driver ---
bolt_uri = f"bolt://{lb_ip}:7687"
driver = None
try:
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(bolt_uri, auth=(username, password))
    # database="neo4j" is required. Without it the driver triggers a home-database
    # resolution step in Neo4j 5.x. During early cluster startup that step can fail
    # with DatabaseNotFound even though the database exists, causing a false FAIL.
    with driver.session(database="neo4j") as session:
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
