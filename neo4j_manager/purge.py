import logging
import json
from typing import List, Tuple, Set, Dict, Any, Optional
from collections import defaultdict
from tqdm import tqdm
from utils import align_string
from llm_client import FAKE_SUMMARY_CONTENT

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class PurgeMixin:
    """Methods for purging data, symbols, and relationships from the graph."""

    def cleanup_orphan_nodes(self):
        """Removes nodes that have no relationships to any other nodes."""
        query = """
                MATCH (n)
                WHERE NOT (n)--()
                WITH collect( properties(n)) AS deleted_nodes,
                     collect(n) AS nodes
                FOREACH (x IN nodes | DETACH DELETE x)
                RETURN deleted_nodes
        """

        with self.driver.session() as session:
            record = session.run(query).single()
            deleted_nodes = record["deleted_nodes"]
            logger.debug(f"Deleted {len(deleted_nodes)} orphan nodes.")
            logger.debug(json.dumps(deleted_nodes, indent=2, default=str))
            return len(deleted_nodes)

    def total_nodes_in_graph(self) -> int:
        """Returns the total number of nodes currently in the database."""
        query = "MATCH (n) RETURN count(n)"
        with self.driver.session() as session:
            result = session.run(query)
            return result.single()[0]

    def total_relationships_in_graph(self) -> int:
        """Returns the total number of relationships currently in the database."""
        query = "MATCH ()-[r]->() RETURN count(r)"
        with self.driver.session() as session:
            result = session.run(query)
            return result.single()[0]

    def wrapup_graph(self, keep_orphans: bool):
        """Performs final cleanup and logs graph statistics."""
        if not keep_orphans:
            deleted_nodes_count = self.cleanup_orphan_nodes()
            logger.info(f"Removed {deleted_nodes_count} orphan nodes.")
            
            # NOTE: It is fine to prune the empty NAMESPACE nodes. 
            # We just keep them here since they can be regarded as kinds of defines.
            
            # NOTE: Some files although don't define/declare any symbols, are still needed 
            # to be included in the graph because they are source files anyway.
        else:
            logger.info("Skipping cleanup of orphan nodes as requested.")

        logger.info(f"Total nodes in graph: {self.total_nodes_in_graph()}")
        logger.info(f"Total relationships in graph: {self.total_relationships_in_graph()}")

    def purge_files(self, file_paths: List[str]) -> int:
        """Deletes FILE nodes for the given paths. Does not prune empty folders."""
        if not file_paths:
            return 0

        with self.driver.session() as session:
            # Delete the specified FILE nodes
            del_files_query = "UNWIND $paths AS path MATCH (f:FILE {path: path}) DETACH DELETE f"
            result = session.run(del_files_query, paths=file_paths)
            deleted_files = result.consume().counters.nodes_deleted
            logger.info(f"Purged {deleted_files} FILE nodes.")
            return deleted_files

    def cleanup_empty_paths_recursively(self) -> Tuple[int, int]:
        """Deletes FILE nodes with no outgoing links and recursively prunes empty FOLDERs."""
        deleted_files = 0
        deleted_folders = 0

        with self.driver.session() as session:
            # Delete the specified FILE nodes
            del_files_query = "MATCH (f:FILE) WHERE NOT (f)-->() DETACH DELETE f"
            result = session.run(del_files_query)
            deleted_files = result.consume().counters.nodes_deleted
            logger.info(f"Deleted {deleted_files} empty FILE nodes.")

            # Iteratively delete empty folders
            while True:
                del_folders_query = """
                MATCH (d:FOLDER)
                WHERE NOT (d)-[:CONTAINS]->()
                DETACH DELETE d
                RETURN count(d)
                """
                result = session.run(del_folders_query)
                count = result.single()[0]
                if count == 0:
                    break
                deleted_folders += count
                logger.info(f"Pruned {deleted_folders} empty FOLDER nodes so far...")
        
        logger.info(f"Total empty FOLDER nodes pruned: {deleted_folders}")
        return deleted_files, deleted_folders

    def cleanup_empty_namespaces_recursively(self) -> int:
        """Deletes NAMESPACE nodes that do not contain any other nodes."""
        total_deleted = 0
        while True:
            query = """
            MATCH (ns:NAMESPACE)
            WHERE NOT EXISTS((ns)-[:SCOPE_CONTAINS]->())
            DETACH DELETE ns
            RETURN count(ns) AS deletedCount
            """
            with self.driver.session() as session:
                result = session.run(query)
                deleted_count = result.single()['deletedCount']
                if deleted_count == 0:
                    break 
                total_deleted += deleted_count
                logger.info(f"Cleaned up {deleted_count} empty NAMESPACE nodes in this iteration.")
        return total_deleted

    def purge_symbols_defined_in_files(self, file_paths: List[str]) -> int:
        """Deletes all symbols anchored to the given files via [:DEFINES] relationships."""
        if not file_paths:
            return 0

        # NOTE: Originally, this query used APOC to find the entire subgraph of "owned" symbols.
        # However, deleting directly defined symbols is more efficient and sufficient because
        # the mini-builder will correctly reconstruct relationships for any descendants that survive.
        query_delete_directly_defined_symbols = """
            UNWIND $paths AS path
            MATCH (file:FILE {path: path})-[:DEFINES]->(s)
            DETACH DELETE s
        """
        query = query_delete_directly_defined_symbols

        with self.driver.session() as session:
            result = session.run(query, paths=file_paths)
            deleted_symbols = result.consume().counters.nodes_deleted
            logger.info(f"Purged {deleted_symbols} symbols defined in {len(file_paths)} files.")
            return deleted_symbols

    def purge_symbols_declared_in_files(self, file_paths: List[str]) -> int:
        """Deletes all symbols anchored to the given files via [:DECLARES] relationships."""
        if not file_paths:
            return 0
        
        query = """
        UNWIND $paths AS path
        MATCH (file:FILE {path: path})-[:DECLARES]->(s)
        DETACH DELETE s
        """
        with self.driver.session() as session:
            result = session.run(query, paths=file_paths)
            deleted_symbols = result.consume().counters.nodes_deleted
            logger.info(f"Purged {deleted_symbols} symbols declared in {len(file_paths)} files.")
            return deleted_symbols

    def ingest_include_relations(self, relations: List[Dict], batch_size: int = 1000):
        """Ingests :INCLUDES relationships between files."""
        if not relations:
            return

        logger.info(f"Ingesting {len(relations)} :INCLUDES relationships in batches of {batch_size}...")
        query = """
        UNWIND $batch as relation
        MATCH (including:FILE {path: relation.including_path})
        MATCH (included:FILE {path: relation.included_path})
        MERGE (including)-[:INCLUDES]->(included)
        """

        total_created = 0
        for i in tqdm(range(0, len(relations), batch_size), desc=align_string("Ingesting INCLUDES relationships")):
            batch = relations[i:i + batch_size]
            summary = self.execute_autocommit_query(query, {"batch": batch})
            total_created += summary.relationships_created

        logger.info(f"Finished ingesting :INCLUDES relationships. Total new relationships: {total_created}.")

    def purge_include_relations_from_files(self, file_paths: List[str]) -> int:
        """Deletes all outgoing :INCLUDES relationships from the specified files."""
        if not file_paths:
            return 0
        
        query = """
        UNWIND $paths AS path
        MATCH (:FILE {path: path})-[r:INCLUDES]->()
        DELETE r
        RETURN count(r)
        """
        with self.driver.session() as session:
            result = session.run(query, paths=file_paths)
            count = result.single()[0]
            logger.info(f"Purged {count} :INCLUDES relationships from {len(file_paths)} files.")
            return count

    def purge_nodes_by_path(self, file_paths: List[str], batch_size: int = 1000) -> int:
        """Deletes all nodes (symbols, macros, aliases) belonging to the given file paths."""
        if not file_paths:
            return 0

        logger.info(f"Purging all nodes anchored to {len(file_paths)} files...")
        query = """
        UNWIND $paths AS p
        MATCH (n {path: p})
        WHERE NOT n:FILE AND NOT n:FOLDER
        DETACH DELETE n
        """
        total_deleted = 0
        for i in tqdm(range(0, len(file_paths), batch_size), desc=align_string("Purging nodes by path")):
            batch = file_paths[i:i + batch_size]
            counters = self.execute_autocommit_query(query, {"paths": batch})
            total_deleted += counters.nodes_deleted
        
        logger.info(f"  Total nodes purged by path: {total_deleted}")
        return total_deleted

    def purge_guest_declarations(self, file_paths: List[str], batch_size: int = 1000) -> int:
        """Removes outgoing [:DECLARES] relationships from the given files."""
        if not file_paths:
            return 0

        logger.info(f"Purging guest declarations from {len(file_paths)} files...")
        query = """
        UNWIND $paths AS p
        MATCH (f:FILE {path: p})-[r:DECLARES]->(s)
        DELETE r
        """
        total_purged = 0
        for i in tqdm(range(0, len(file_paths), batch_size), desc=align_string("Purging guest decls")):
            batch = file_paths[i:i + batch_size]
            counters = self.execute_autocommit_query(query, {"paths": batch})
            total_purged += counters.relationships_deleted
        
        logger.info(f"  Total guest declaration relationships purged: {total_purged}")
        return total_purged

    def purge_nodes_by_id(self, symbol_ids: Set[str], all_symbols: Dict[str, Any], dirty_files: Set[str], debug_mode: bool, batch_size: int = 1000):
        """Resolves identity collisions for seed symbols during incremental updates."""
        if not symbol_ids:
            return

        from clangd_index_yaml_parser import Symbol

        mode_str = "Isolation (DETACH DELETE)" if debug_mode else "Aggregation (Relationship Purge)"
        logger.info(f"Resolving {len(symbol_ids)} seed identities using {mode_str}...")
        
        # Group IDs by their Neo4j label for efficient batch processing
        ids_by_label = defaultdict(list)
        for sid in symbol_ids:
            sym = all_symbols.get(sid)
            if sym:
                label = Symbol.get_node_label(sym)
                if label:
                    ids_by_label[label].append(sid)
                else:
                    logger.debug(f"Seed symbol {sid} (kind: {sym.kind}) has no valid label mapping; skipping purge.")
            else:
                logger.debug(f"Seed symbol ID {sid} not found in full symbols; skipping purge.")

        if not ids_by_label:
            logger.info("No valid seed labels found to purge.")
            return

        total_affected = 0
        for label, ids in ids_by_label.items():
            if debug_mode:
                query = f"""
                UNWIND $ids AS sid
                MATCH (n:{label} {{id: sid}})
                WHERE NOT n.path IN $dirty_files
                DETACH DELETE n
                """
            else:
                query = f"""
                UNWIND $ids AS sid
                MATCH (n:{label} {{id: sid}})
                WHERE NOT n.path IN $dirty_files
                OPTIONAL MATCH (f:FILE)-[r:DEFINES|DECLARES]->(n)
                OPTIONAL MATCH (n)-[r2:EXPANDED_FROM|ALIAS_OF]->()
                DELETE r, r2
                """
            
            for i in range(0, len(ids), batch_size):
                batch = ids[i:i + batch_size]
                counters = self.execute_autocommit_query(query, {"ids": batch, "dirty_files": list(dirty_files)})
                total_affected += (counters.nodes_deleted if debug_mode else counters.relationships_deleted)
            
        logger.info(f"  Total {('nodes purged' if debug_mode else 'relationships purged')}: {total_affected}")

    def delete_property(self, label: Optional[str], property_key: str, all_labels: bool = False) -> int:
        """Deletes a property from nodes with a given label, or from all nodes.
        Supports 'fake_summary' alias to target specific fake content.
        """
        if not label and not all_labels:
            raise ValueError("Either 'label' must be provided or 'all_labels' must be True.")
        if label and all_labels:
            raise ValueError("Cannot specify both 'label' and 'all_labels'. Choose one.")

        target_clause = f"n:{label}" if label else "n"
        
        if property_key == "fake_summary":
            logger.info(f"Targeting 'fake_summary' (values matching: '{FAKE_SUMMARY_CONTENT}') from nodes matching '{target_clause}'...")
            # We remove both the summary and the embedding for fake summaries
            query = f"""
            MATCH ({target_clause}) 
            WHERE n.summary = $fake_content or n.code_analysis = $fake_content
            REMOVE n.code_analysis, n.summary, n.summaryEmbedding 
            RETURN count(n)
            """
            params = {"fake_content": FAKE_SUMMARY_CONTENT}
        else:
            logger.info(f"Deleting property '{property_key}' from nodes matching '{target_clause}'...")
            query = f"MATCH ({target_clause}) WHERE n.{property_key} IS NOT NULL REMOVE n.{property_key} RETURN count(n)"
            params = {}
        
        with self.driver.session() as session:
            result = session.run(query, **params)
            count = result.single()[0] if result.peek() else 0
            
            display_name = property_key if property_key != "fake_summary" else "fake summary and embedding"
            logger.info(f"Removed {display_name} from {count} nodes.")
            return count
