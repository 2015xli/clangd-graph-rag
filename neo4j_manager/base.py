import os
import threading
import logging
from typing import List, Dict, Tuple, Optional, Any
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Neo4j connection settings
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "12345678")

class Neo4jBase:
    """Core Neo4j connection and transaction management."""
    def __init__(self, uri: str = NEO4J_URI, user: str = NEO4J_USER, password: str = NEO4J_PASSWORD) -> None:
        self.uri, self.user, self.password = uri, user, password
        self._driver = None
        self._lock = threading.Lock()

    # ---------- lifecycle ----------
    def _ensure_driver(self):
        if self._driver is None:
            with self._lock:
                if self._driver is None:  # double-checked
                    self._driver = GraphDatabase.driver(
                        self.uri,
                        auth=(self.user, self.password)
                    )

    def close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    def __enter__(self):
        self._ensure_driver()
        return self

    def __exit__(self, exc_type, exc, tb):
        # keep existing behavior
        self.close()

    @property
    def driver(self):
        self._ensure_driver()
        return self._driver

    def check_connection(self) -> bool:
        try:
            self.driver.verify_connectivity()
            logger.info("✅ Connection established!")
            return True
        except Exception as e:
            logger.error(f"❌ Connection failed: {e}")
            return False

    def process_batch(self, batch: List[Tuple[str, Dict]]) -> List[Any]:
        """Executes a batch of queries within a single transaction."""
        all_counters = []
        with self.driver.session() as session:
            with session.begin_transaction() as tx:
                for cypher, params in batch:
                    result = tx.run(cypher, **params)
                    all_counters.append(result.consume().counters)
                tx.commit()
        return all_counters

    def execute_autocommit_query(self, cypher: str, params: Dict = None) -> Any:
        """Executes a single query and returns the update counters."""
        with self.driver.session() as session:
            result = session.run(cypher, **(params or {}))
            return result.consume().counters

    def execute_read_query(self, cypher: str, params: dict = None) -> list[dict]:
        """Executes a read query and returns a list of result records."""
        with self.driver.session() as session:
            result = session.run(cypher, **(params or {}))
            return [record.data() for record in result]

    def execute_query_and_return_records(self, cypher: str, params: dict = None) -> List[Dict]:
        """Executes a query and returns a list of result records."""
        with self.driver.session() as session:
            result = session.run(cypher, **(params or {}))
            return [record.data() for record in result]
