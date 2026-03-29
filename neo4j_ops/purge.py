import logging
from typing import List, Dict, Set, Tuple, Optional
from collections import defaultdict
from tqdm import tqdm
from utils import align_string

logger = logging.getLogger(__name__)

class PurgeMixin:
    """Methods for purging data, symbols, and relationships from the graph."""

    def purge_files(self, file_paths: List[str]) -> int:
        if not file_paths: return 0
        with self.driver.session() as session:
            query = "UNWIND $paths AS path MATCH (f:FILE {path: path}) DETACH DELETE f"
            result = session.run(query, paths=file_paths)
            deleted_count = result.consume().counters.nodes_deleted
            logger.info(f"Purged {deleted_count} FILE nodes.")
            return deleted_count

    def cleanup_empty_paths_recursively(self) -> Tuple[int, int]:
        deleted_files = 0
        deleted_folders = 0
        with self.driver.session() as session:
            del_files_query = "MATCH (f:FILE) WHERE NOT (f)-->() DETACH DELETE f"
            result = session.run(del_files_query)
            deleted_files = result.consume().counters.nodes_deleted
            logger.info(f"Deleted {deleted_files} empty FILE nodes.")
            while True:
                del_folders_query = "MATCH (d:FOLDER) WHERE NOT (d)-[:CONTAINS]->() DETACH DELETE d RETURN count(d)"
                result = session.run(del_folders_query)
                count = result.single()[0]
                if count == 0: break
                deleted_folders += count
        return deleted_files, deleted_folders

    def cleanup_empty_namespaces_recursively(self) -> int:
        total_deleted = 0
        while True:
            query = "MATCH (ns:NAMESPACE) WHERE NOT EXISTS((ns)-[:SCOPE_CONTAINS]->()) DETACH DELETE ns RETURN count(ns) AS deletedCount"
            with self.driver.session() as session:
                result = session.run(query)
                count = result.single()['deletedCount']
                if count == 0: break
                total_deleted += count
        return total_deleted

    def purge_symbols_defined_in_files(self, file_paths: List[str]) -> int:
        if not file_paths: return 0
        query = "UNWIND $paths AS path MATCH (file:FILE {path: path})-[:DEFINES]->(s) DETACH DELETE s"
        with self.driver.session() as session:
            result = session.run(query, paths=file_paths)
            count = result.consume().counters.nodes_deleted
            logger.info(f"Purged {count} symbols defined in {len(file_paths)} files.")
            return count

    def purge_symbols_declared_in_files(self, file_paths: List[str]) -> int:
        if not file_paths: return 0
        query = "UNWIND $paths AS path MATCH (file:FILE {path: path})-[:DECLARES]->(s) DETACH DELETE s"
        with self.driver.session() as session:
            result = session.run(query, paths=file_paths)
            count = result.consume().counters.nodes_deleted
            logger.info(f"Purged {count} symbols declared in {len(file_paths)} files.")
            return count

    def ingest_include_relations(self, relations: List[Dict], batch_size: int = 1000):
        if not relations: return
        query = "UNWIND $batch as relation MATCH (including:FILE {path: relation.including_path}) MATCH (included:FILE {path: relation.included_path}) MERGE (including)-[:INCLUDES]->(included)"
        for i in tqdm(range(0, len(relations), batch_size), desc=align_string("Ingesting INCLUDES relationships")):
            self.execute_autocommit_query(query, {"batch": relations[i:i + batch_size]})

    def purge_include_relations_from_files(self, file_paths: List[str]) -> int:
        if not file_paths: return 0
        query = "UNWIND $paths AS path MATCH (:FILE {path: path})-[r:INCLUDES]->() DELETE r RETURN count(r)"
        with self.driver.session() as session:
            result = session.run(query, paths=file_paths)
            count = result.single()[0]
            logger.info(f"Purged {count} :INCLUDES relationships from {len(file_paths)} files.")
            return count

    def purge_nodes_by_path(self, file_paths: List[str], batch_size: int = 1000) -> int:
        if not file_paths: return 0
        logger.info(f"Purging all nodes anchored to {len(file_paths)} files...")
        query = "UNWIND $paths AS p MATCH (n {path: p}) WHERE NOT n:FILE AND NOT n:FOLDER DETACH DELETE n"
        total_deleted = 0
        for i in tqdm(range(0, len(file_paths), batch_size), desc=align_string("Purging nodes by path")):
            counters = self.execute_autocommit_query(query, {"paths": file_paths[i:i + batch_size]})
            total_deleted += counters.nodes_deleted
        return total_deleted

    def purge_guest_declarations(self, file_paths: List[str], batch_size: int = 1000) -> int:
        if not file_paths: return 0
        logger.info(f"Purging guest declarations from {len(file_paths)} files...")
        query = "UNWIND $paths AS p MATCH (f:FILE {path: p})-[r:DECLARES]->(s) DELETE r"
        total_purged = 0
        for i in tqdm(range(0, len(file_paths), batch_size), desc=align_string("Purging guest decls")):
            counters = self.execute_autocommit_query(query, {"paths": file_paths[i:i + batch_size]})
            total_purged += counters.relationships_deleted
        return total_purged

    def purge_nodes_by_id(self, symbol_to_label: Dict[str, str], dirty_files: Set[str], debug_mode: bool, batch_size: int = 1000):
        if not symbol_to_label: return
        mode_str = "Isolation (DETACH DELETE)" if debug_mode else "Aggregation (Relationship Purge)"
        logger.info(f"Resolving {len(symbol_to_label)} seed identities using {mode_str}...")
        ids_by_label = defaultdict(list)
        for sid, label in symbol_to_label.items(): ids_by_label[label].append(sid)
        total_affected = 0
        for label, ids in ids_by_label.items():
            if debug_mode:
                query = f"UNWIND $ids AS sid MATCH (n:{label} {{id: sid}}) WHERE NOT n.path IN $dirty_files DETACH DELETE n"
            else:
                query = f"UNWIND $ids AS sid MATCH (n:{label} {{id: sid}}) WHERE NOT n.path IN $dirty_files OPTIONAL MATCH (f:FILE)-[r:DEFINES|DECLARES]->(n) OPTIONAL MATCH (n)-[r2:EXPANDED_FROM|ALIAS_OF]->() DELETE r, r2"
            for i in range(0, len(ids), batch_size):
                counters = self.execute_autocommit_query(query, {"ids": ids[i:i + batch_size], "dirty_files": list(dirty_files)})
                total_affected += (counters.nodes_deleted if debug_mode else counters.relationships_deleted)
        logger.info(f"  Total {('nodes purged' if debug_mode else 'relationships purged')}: {total_affected}")

    def delete_property(self, label: Optional[str], property_key: str, all_labels: bool = False) -> int:
        target_clause = f"n:{label}" if label else "n"
        query = f"MATCH ({target_clause}) WHERE n.{property_key} IS NOT NULL REMOVE n.{property_key} RETURN count(n)"
        with self.driver.session() as session:
            result = session.run(query)
            return result.single()[0] if result.peek() else 0
