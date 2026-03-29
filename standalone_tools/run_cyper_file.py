#!/usr/bin/env python3
import argparse
import os
import sys
from neo4j import GraphDatabase


class Neo4jManager:
    """Manages Neo4j database operations."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        self.uri = uri
        self.user = user
        self.password = password
        self.driver = None

    def __enter__(self):
        self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.driver:
            self.driver.close()

    def check_connection(self) -> bool:
        try:
            self.driver.verify_connectivity()
            print("✅ Connection established!")
            with self.driver.session() as session:
                result = session.run("RETURN 1 AS result").single()
                print("Test query result:", result["result"])
            return True
        except Exception as e:
            print("❌ Connection failed:", e)
            return False

    def reset_database(self) -> None:
        with self.driver.session() as session:
            print("Deleting existing data...")
            session.run("MATCH (n) DETACH DELETE n")
            print("Database cleared.")

    def run_query(self, query: str, session=None) -> None:
        """Run a single query (optionally in existing session)."""
        try:
            if session:
                session.run(query)
            else:
                with self.driver.session() as s:
                    s.run(query)
            print(f"✅ Executed: {query.strip()}")
        except Exception as e:
            print(f"❌ Failed query: {query.strip()}\n   Error: {e}")

    def run_queries_batch(self, queries: list[str]) -> None:
        """Run all queries in one session."""
        with self.driver.session() as session:
            for query in queries:
                try:
                    session.run(query)
                    print(f"✅ Executed: {query.strip()}")
                except Exception as e:
                    print(f"❌ Failed query: {query.strip()}\n   Error: {e}")


def read_queries_from_file(filepath: str) -> list[str]:
    """Read Cypher queries from file, separated by ';' or newlines."""
    queries = []
    buffer = ""
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue

            buffer += " " + line

            while ";" in buffer:
                part, buffer = buffer.split(";", 1)
                if part.strip():
                    queries.append(part.strip())

    if buffer.strip():
        queries.append(buffer.strip())

    return queries


def main():
    parser = argparse.ArgumentParser(description="Run Cypher queries from file into Neo4j")
    parser.add_argument("file", help="Input file containing Cypher queries (semicolon optional)")
    parser.add_argument("--reset", action="store_true", help="Reset the database before running queries")
    parser.add_argument("--non-batch", action="store_true", help="Run queries one by one instead of batch mode")
    args = parser.parse_args()

    # Read connection info from environment
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")

    if not uri or not user or not password:
        print("❌ Missing Neo4j connection details in environment variables:")
        print("   NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD must be set.")
        sys.exit(1)

    with Neo4jManager(uri, user, password) as neo:
        if not neo.check_connection():
            return

        if args.reset:
            neo.reset_database()

        queries = read_queries_from_file(args.file)
        print(f"📥 Loaded {len(queries)} queries from {args.file}")

        if args.non_batch:
            print("⚡ Running in non-batch mode (one query per session)...")
            for q in queries:
                neo.run_query(q)
        else:
            print("⚡ Running in batch mode (all queries in one session)...")
            neo.run_queries_batch(queries)

        print("✅ All queries processed.")


if __name__ == "__main__":
    main()

