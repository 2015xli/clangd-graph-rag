#!/usr/bin/env python3
"""
This module provides the GraphDebugManager class, which encapsulates 
instrumentation and auditing logic for the code graph.
"""

import logging
from typing import List, Dict, Set, Optional
from tqdm import tqdm
from neo4j_manager import Neo4jManager, align_string

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class GraphDebugManager:
    """Manages debugging tools like APOC triggers and scope dumping."""

    def __init__(self, neo4j_mgr: Neo4jManager):
        self.neo4j_mgr = neo4j_mgr

    def install_update_trigger(self, commit_hash: str):
        """Installs an APOC trigger to tag newly created nodes and relationships."""
        logger.info(f"Installing APOC trigger to track new nodes/relationships for commit: {commit_hash}")
        trigger_query = """
        CALL apoc.trigger.add('track-new-only', 
          "UNWIND $createdNodes AS n 
           SET n.updated = $commitHash
           WITH 1 as dummy
           UNWIND $createdRelationships AS r
           SET r.updated = $commitHash", 
          {phase: 'before'}, 
          {params: {commitHash: $commitHash}}
        )
        """
        self.neo4j_mgr.execute_autocommit_query(trigger_query, {"commitHash": commit_hash})

    def remove_update_trigger(self):
        """Removes the tracking trigger."""
        logger.info("Removing APOC update trigger.")
        self.neo4j_mgr.execute_autocommit_query("CALL apoc.trigger.remove('track-new-only')")

    def remove_updated_property(self):
        """Removes the 'updated' property from all nodes and relationships."""
        logger.info("Cleaning up 'updated' properties from graph.")
        node_query = "MATCH (n) WHERE n.updated IS NOT NULL REMOVE n.updated"
        rel_query = "MATCH ()-[r]-() WHERE r.updated IS NOT NULL REMOVE r.updated"
        self.neo4j_mgr.execute_autocommit_query(node_query)
        self.neo4j_mgr.execute_autocommit_query(rel_query)

    def _get_node_debug_info(self, node_record: Dict) -> str:
        """Helper to format node info as id:label:name."""
        node_id = node_record.get('id') or node_record.get('path') or "unknown"
        label = node_record.get('label', 'UNKNOWN')
        name = node_record.get('name', 'unnamed')
        return f"{node_id}:{label}:{name}"

    def _get_rel_debug_info(self, rel_record: Dict) -> str:
        """Helper to format relationship info as from_id:to_id:type."""
        from_id = rel_record.get('from_id') or rel_record.get('from_path') or "unknown"
        to_id = rel_record.get('to_id') or rel_record.get('to_path') or "unknown"
        rel_type = rel_record.get('type', 'UNKNOWN')
        return f"{from_id}:{to_id}:{rel_type}"

    def dump_purged_scope(self, files_to_purge_symbols: List[str], files_to_delete: List[str], seed_ids: Set[str], output_file: str = "updater_purged_scope.log", collision_file: str = "updater_colliding_symbols.log"):
        """
        Dumps the nodes and relationships about to be purged.
        Separates standard file-based symbols from colliding symbols.
        """
        logger.info(f"Dumping purged scope to {output_file} and collisions to {collision_file}...")
        
        # 1. Standard Purged Nodes (Path-based or Relationship-based association)
        node_query = """
        MATCH (n)
        WHERE (n.path IN $purge_sym_files AND NOT n:FILE AND NOT n:FOLDER)
           OR (n:FILE AND n.path IN $delete_files)
           OR EXISTS { MATCH (f:FILE)-[:DEFINES|DECLARES]->(n) WHERE f.path IN $purge_sym_files }
        RETURN n.id AS id, n.path AS path, n.name AS name, 
               [l IN labels(n) WHERE l <> 'ENTITY'][0] AS label
        """
        params = {"purge_sym_files": files_to_purge_symbols, "delete_files": files_to_delete}
        node_records = self.neo4j_mgr.execute_read_query(node_query, params)
        node_lines = [self._get_node_debug_info(r) for r in node_records]
        
        # 2. Colliding Nodes (IDs in seed_ids but path is elsewhere)
        collision_query = """
        MATCH (n)
        WHERE n.id IN $seed_ids 
          AND n.path IS NOT NULL 
          AND NOT n.path IN $purge_sym_files
          AND NOT n:FILE AND NOT n:FOLDER
        RETURN n.id AS id, n.path AS path, n.name AS name, 
               [l IN labels(n) WHERE l <> 'ENTITY'][0] AS label
        """
        collision_records = self.neo4j_mgr.execute_read_query(collision_query, {"seed_ids": list(seed_ids), "purge_sym_files": files_to_purge_symbols})
        collision_lines = [self._get_node_debug_info(r) for r in collision_records]

        # 3. Relationships for Standard Purged Nodes
        rel_query = """
        MATCH (n)
        WHERE (n.path IN $purge_sym_files AND NOT n:FILE AND NOT n:FOLDER)
           OR (n:FILE AND n.path IN $delete_files)
           OR EXISTS { MATCH (f:FILE)-[:DEFINES|DECLARES]->(n) WHERE f.path IN $purge_sym_files }
        MATCH (n)-[r]-()
        WHERE NOT (n:FILE AND type(r) = 'CONTAINS' AND NOT n.path IN $delete_files)
        RETURN DISTINCT type(r) AS type,
               startNode(r).id AS from_id, startNode(r).path AS from_path,
               endNode(r).id AS to_id, endNode(r).path AS to_path
        """
        rel_records = self.neo4j_mgr.execute_read_query(rel_query, params)
        rel_lines = [self._get_rel_debug_info(r) for r in rel_records]

        # 4. Relationships for Colliding Nodes
        coll_rel_query = """
        MATCH (n)
        WHERE n.id IN $seed_ids AND n.path IS NOT NULL AND NOT n.path IN $purge_sym_files
        MATCH (n)-[r]-()
        RETURN DISTINCT type(r) AS type,
               startNode(r).id AS from_id, startNode(r).path AS from_path,
               endNode(r).id AS to_id, endNode(r).path AS to_path
        """
        coll_rel_records = self.neo4j_mgr.execute_read_query(coll_rel_query, {"seed_ids": list(seed_ids), "purge_sym_files": files_to_purge_symbols})
        coll_rel_lines = [self._get_rel_debug_info(r) for r in coll_rel_records]

        self._write_dump(output_file, node_lines, rel_lines)
        self._write_dump(collision_file, collision_lines, coll_rel_lines)

        # 5. Combined Dump for 1:1 diffing with updated_scope.txt
        combined_node_lines = node_lines + collision_lines
        combined_rel_lines = rel_lines + coll_rel_lines
        self._write_dump("combined_purged_scope.log", combined_node_lines, combined_rel_lines)

    def dump_updated_scope(self, commit_hash: str, output_file: str = "updater_updated_scope.log"):
        """Dumps nodes and relationships tagged with the given commit hash."""
        logger.info(f"Dumping updated scope for {commit_hash} to {output_file}...")
        
        # 1. Query for tagged nodes
        node_query = """
        MATCH (n {updated: $commitHash})
        RETURN n.id AS id, n.path AS path, n.name AS name, 
               [l IN labels(n) WHERE l <> 'ENTITY'][0] AS label
        """
        node_records = self.neo4j_mgr.execute_read_query(node_query, {"commitHash": commit_hash})
        node_lines = [self._get_node_debug_info(r) for r in node_records]
        
        # 2. Query for tagged relationships OR relationships connected to tagged nodes
        rel_query = """
        MATCH (n) WHERE n.updated = $commitHash
        MATCH (n)-[r]-()
        RETURN DISTINCT type(r) AS type,
               startNode(r).id AS from_id, startNode(r).path AS from_path,
               endNode(r).id AS to_id, endNode(r).path AS to_path
        UNION
        MATCH ()-[r {updated: $commitHash}]-()
        RETURN DISTINCT type(r) AS type,
               startNode(r).id AS from_id, startNode(r).path AS from_path,
               endNode(r).id AS to_id, endNode(r).path AS to_path
        """
        rel_records = self.neo4j_mgr.execute_read_query(rel_query, {"commitHash": commit_hash})
        rel_lines = [self._get_rel_debug_info(r) for r in rel_records]

        self._write_dump(output_file, node_lines, rel_lines)

    def _write_dump(self, output_file: str, node_lines: List[str], rel_lines: List[str]):
        """Writes sorted node and relationship info to a file."""
        with open(output_file, 'w') as f:
            f.write("=== NODES ===\n")
            for line in sorted(list(set(node_lines))):
                f.write(line + "\n")
            f.write("\n=== RELATIONSHIPS ===\n")
            for line in sorted(list(set(rel_lines))):
                f.write(line + "\n")
