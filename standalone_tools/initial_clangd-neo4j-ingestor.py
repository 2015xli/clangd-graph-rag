#!/usr/bin/env python3
"""
Streaming importer: clangd YAML index -> Neo4j
Handles multi-document YAML with !Symbol tags.
"""
import os
import yaml
from neo4j import GraphDatabase

# -------------------------
# Neo4j connection settings
# -------------------------
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "12345678")

INDEX_FILE = "project-clangd.yaml"
BATCH_SIZE = 500


# -------------------------
# YAML tag handling
# -------------------------
def unknown_tag(loader, tag_suffix, node):
    return loader.construct_mapping(node)

yaml.SafeLoader.add_multi_constructor("!", unknown_tag)


# -------------------------
# Neo4j helpers
# -------------------------

def check_neo4j_dbms_connection(driver):
      try:
          driver.verify_connectivity()
          print("✅ Connection established!")
          # Optionally perform a quick test query:
          with driver.session() as session:
              result = session.run("RETURN 1 AS result").single()
              print("Test query result:", result["result"])
      except Exception as e:
          print("❌ Connection failed:", e)
      finally:
          #driver.close()
          print("Checked!")

def delete_neo4j_database(driver):
      """Clear existing data in Neo4j and ChromaDB"""
      with driver.session() as session:
          session.run("MATCH (n) DETACH DELETE n")

def init_neo4j_driver():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    check_neo4j_dbms_connection(driver)
    delete_neo4j_database(driver)

    return driver


def create_constraints(session):
    session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (f:FILE) REQUIRE f.path IS UNIQUE")
    session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (fn:FUNCTION) REQUIRE fn.id IS UNIQUE")
    session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (ds:DATA_STRUCTURE) REQUIRE ds.id IS UNIQUE")
    session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (fld:FIELD) REQUIRE fld.id IS UNIQUE")


# -------------------------
# Symbol processing
# -------------------------
def process_symbol(sym):
    ops = []

    sid = sym.get("ID")
    name = sym.get("Name")
    kind = sym.get("SymInfo", {}).get("Kind")

    if not sid or not kind:
        return []

    # Node label & properties
    if kind == "Function":
        label = "FUNCTION"
        props = {
            "id": sid,
            "name": name,
            "signature": sym.get("Signature", ""),
            "return_type": sym.get("ReturnType", ""),
            "has_definition": "Definition" in sym,
        }
    elif kind in ("Struct", "Class", "Union", "Enum"):
        label = "DATA_STRUCTURE"
        props = {
            "id": sid,
            "name": name,
            "kind": kind,
            "has_definition": "Definition" in sym,
        }
    else:
        return []

    # Create node
    ops.append((
        f"MERGE (n:{label} {{id:$id}}) SET n += $props",
        {"id": sid, "props": props}
    ))

    # File relations
    for loc in ("CanonicalDeclaration", "Definition"):
        if loc in sym:
            file_uri = sym[loc].get("FileURI")
            if file_uri:
                ops.append(("MERGE (f:FILE {path:$path})", {"path": file_uri}))
                if loc == "Definition":
                    ops.append((
                        "MATCH (f:FILE {path:$path}), (n {id:$id}) "
                        "MERGE (f)-[:DEFINES]->(n)",
                        {"path": file_uri, "id": sid}
                    ))

    return ops


# -------------------------
# Main
# -------------------------
def main():
    driver = init_neo4j_driver()
    with driver.session() as session:
        create_constraints(session)

    batch, count = [], 0

    with open(INDEX_FILE, "r") as f:
        for sym in yaml.safe_load_all(f):
            if not sym:
                continue
            ops = process_symbol(sym)
            for cypher, params in ops:
                batch.append((cypher, params))
                if len(batch) >= BATCH_SIZE:
                    with driver.session() as session:
                        for cy, pa in batch:
                            session.run(cy, **pa)
                    count += len(batch)
                    print(f"Committed {count} ops...")
                    batch.clear()

    if batch:
        with driver.session() as session:
            for cy, pa in batch:
                session.run(cy, **pa)
        count += len(batch)

    print(f"Done. Executed {count} operations.")


if __name__ == "__main__":
    main()

