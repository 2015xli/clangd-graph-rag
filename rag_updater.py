#!/usr/bin/env python3
"""
Orchestrates the incremental update of RAG data in the code graph.
"""

import logging
import os
from typing import Set, Dict, List
from collections import defaultdict

from rag_orchestrator import RagOrchestrator

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class RagUpdater(RagOrchestrator):
    """Orchestrates the targeted update of RAG data."""

    def summarize_targeted_update(self, seed_symbol_ids: set, structurally_changed_files: dict):
        """
        Runs a targeted, multi-pass summarization handling both content and structural changes.
        """
        if not seed_symbol_ids and not any(structurally_changed_files.values()):
            logging.info("No seed symbols or structural changes provided for targeted update. Skipping.")
            return

        self.summary_cache_manager.load()
        logging.info(f"--- Starting Targeted RAG Update for {len(seed_symbol_ids)} seed symbols and {sum(len(v) for v in structurally_changed_files.values())} structural file changes ---")

        # --- Function Summary Passes (Content Changes) ---
        logging.info("Targeted Update - Pass 1: Analyzing changed functions individually...")
        
        # No need to pre-filter the seed IDs to only include functions and methods
        # because the _analyze_functions_individually_with_ids function will do it for us
        updated_code_analysis_ids = self._analyze_functions_individually_with_ids(list(seed_symbol_ids))
        logging.info(f"{len(updated_code_analysis_ids)} functions received a new code analysis.")
        self.summary_cache_manager.save(mode="updater", neo4j_mgr=self.neo4j_mgr, is_intermediate=True)

        logging.info("Targeted Update - Pass 2: Summarizing functions with context...")
        neighbor_function_ids = self._get_neighbor_function_ids(updated_code_analysis_ids)
        all_function_ids_to_process = updated_code_analysis_ids.union(neighbor_function_ids)
        logging.info(f"Expanded scope for Pass 2 to {len(all_function_ids_to_process)} total functions.")
        
        updated_final_summary_ids = self._summarize_functions_with_context_with_ids(list(all_function_ids_to_process))
        logging.info(f"{len(updated_final_summary_ids)} functions received a new final summary.")
        self.summary_cache_manager.save(mode="updater", neo4j_mgr=self.neo4j_mgr, is_intermediate=True)

        # --- Targeted Class Summaries ---
        logging.info("Targeted Update - Pass 3: Summarizing changed class structures...")
        seed_class_ids_from_seeds = self._find_classes_of_symbol_ids(seed_symbol_ids)
        seed_class_ids_from_functions = self._find_classes_for_updated_methods(updated_final_summary_ids)
        seed_class_ids_from_files = self._find_classes_for_changed_files(
            structurally_changed_files.get('added', []) + structurally_changed_files.get('modified', [])
        )
        all_seed_class_ids = seed_class_ids_from_functions.union(seed_class_ids_from_files).union(seed_class_ids_from_seeds)
        updated_class_summary_ids = self._summarize_classes_with_ids(all_seed_class_ids)
        logging.info(f"{len(updated_class_summary_ids)} classes received a new summary.")
        self.summary_cache_manager.save(mode="updater", neo4j_mgr=self.neo4j_mgr, is_intermediate=True)

        # --- Targeted Namespace Summaries ---
        logging.info("Targeted Update - Pass 4: Summarizing changed namespaces...")
        seed_namespace_ids_from_children = self._find_namespaces_for_updated_children(
            updated_final_summary_ids.union(updated_class_summary_ids)
        )
        seed_namespace_ids_from_files = self._find_namespaces_for_changed_files(
            structurally_changed_files.get('added', []) + structurally_changed_files.get('modified', [])
        )
        all_seed_namespace_ids = seed_namespace_ids_from_children.union(seed_namespace_ids_from_files)
        updated_namespace_summary_ids = self._summarize_namespaces_with_ids(all_seed_namespace_ids)
        logging.info(f"{len(updated_namespace_summary_ids)} namespaces received a new summary.")
        self.summary_cache_manager.save(mode="updater", neo4j_mgr=self.neo4j_mgr, is_intermediate=True)

        # --- File & Folder Roll-up Passes (Content + Structural Changes) ---
        files_with_summary_changes = self._find_files_for_updated_symbols(updated_final_summary_ids)
        files_with_class_summary_changes = self._find_files_for_updated_classes(updated_class_summary_ids)
        files_with_namespace_summary_changes = self._find_files_for_updated_namespaces(updated_namespace_summary_ids)
        added_files = set(structurally_changed_files.get('added', []))
        modified_files = set(structurally_changed_files.get('modified', []))
        files_to_resummarize = files_with_summary_changes.union(files_with_class_summary_changes).union(files_with_namespace_summary_changes).union(added_files).union(modified_files)
        self._summarize_files_with_paths(list(files_to_resummarize))

        deleted_files = set(structurally_changed_files.get('deleted', []))
        all_trigger_files = files_to_resummarize.union(deleted_files)
        if not all_trigger_files:
            logging.info("No file or folder roll-up needed.")
        else:
            all_affected_folders_paths = set()
            for file_path in all_trigger_files:
                parent = os.path.dirname(file_path)
                while parent and parent != '.':
                    all_affected_folders_paths.add(parent)
                    parent = os.path.dirname(parent)
            
            self._summarize_folders_with_paths(list(all_affected_folders_paths))
            self._summarize_project()
        
        # Final save with healing/pruning logic
        self.summary_cache_manager.save(mode="updater", neo4j_mgr=self.neo4j_mgr)

        logging.info(f"Total number of summaries processed: {self.n_restored + self.n_generated + self.n_unchanged + self.n_nochildren + self.n_failed}")
        logging.info(f"  Restored: {self.n_restored}, Generated: {self.n_generated}, Unchanged: {self.n_unchanged}, No children: {self.n_nochildren}, Failed: {self.n_failed}")

        # --- Final Pass ---
        self.generate_embeddings()
        logging.info("--- Finished Targeted RAG Update ---")
        

    def _get_neighbor_function_ids(self, seed_symbol_ids: set) -> set:
        if not seed_symbol_ids:
            return set()
        
        query = """
        UNWIND $seed_ids AS seedId
        MATCH (n:FUNCTION|METHOD) WHERE n.id = seedId
        OPTIONAL MATCH (neighbor:FUNCTION|METHOD)-[:CALLS*1]-(n)
        WHERE neighbor.body_location IS NOT NULL
        WITH collect(DISTINCT n.id) + collect(DISTINCT neighbor.id) AS allIds
        UNWIND allIds as id
        RETURN collect(DISTINCT id) as ids
        """
        result = self.neo4j_mgr.execute_read_query(query, {"seed_ids": list(seed_symbol_ids)})
        if result and result[0] and result[0]['ids']:
            return set(result[0]['ids'])
        return seed_symbol_ids

    def _find_files_for_updated_symbols(self, symbol_ids: set) -> set:
        if not symbol_ids:
            return set()
        query = """
        UNWIND $symbol_ids as symbolId
        MATCH (f:FILE)-[:DEFINES]->(s) WHERE s.id = symbolId AND (s:FUNCTION OR s:METHOD)
        RETURN DISTINCT f.path AS path
        """
        results = self.neo4j_mgr.execute_read_query(query, {"symbol_ids": list(symbol_ids)})
        return {r['path'] for r in results}

    def _find_classes_of_symbol_ids(self, symbol_ids: set) -> set:
        if not symbol_ids:
            return set()
        query = """
            MATCH (c:CLASS_STRUCTURE) 
            WHERE c.id IN $symbol_ids 
            RETURN DISTINCT c.id AS id
        """
        results = self.neo4j_mgr.execute_read_query(query, {"symbol_ids": list(symbol_ids)})
        return {r['id'] for r in results}
    
    def _find_classes_for_updated_methods(self, method_ids: set) -> set:
        if not method_ids:
            return set()
        query = """
        UNWIND $method_ids as methodId
        MATCH (c:CLASS_STRUCTURE)-[:HAS_METHOD]->(m:METHOD) WHERE m.id = methodId
        RETURN DISTINCT c.id AS id
        """
        results = self.neo4j_mgr.execute_read_query(query, {"method_ids": list(method_ids)})
        return {r['id'] for r in results}

    def _find_classes_for_changed_files(self, file_paths: list[str]) -> set:
        if not file_paths:
            return set()
        query = """
        UNWIND $file_paths as file_path
        MATCH (f:FILE {path: file_path})-[:DEFINES|DECLARES]->(c:CLASS_STRUCTURE)
        RETURN DISTINCT c.id AS id
        """
        results = self.neo4j_mgr.execute_read_query(query, {"file_paths": list(file_paths)})
        return {r['id'] for r in results}

    def _find_files_for_updated_classes(self, class_ids: set) -> set:
        if not class_ids:
            return set()
        query = """
        UNWIND $class_ids as classId
        MATCH (f:FILE)-[:DEFINES|DECLARES]->(c:CLASS_STRUCTURE) WHERE c.id = classId
        RETURN DISTINCT f.path AS path
        """
        results = self.neo4j_mgr.execute_read_query(query, {"class_ids": list(class_ids)})
        return {r['path'] for r in results}

    def _find_namespaces_for_updated_children(self, child_ids: Set[str]) -> Set[str]:
        if not child_ids:
            return set()
        query = """
        UNWIND $child_ids as childId
        MATCH (ns:NAMESPACE)-[:SCOPE_CONTAINS]->(child) WHERE child.id = childId
        RETURN DISTINCT ns.id AS id
        """
        results = self.neo4j_mgr.execute_read_query(query, {"child_ids": list(child_ids)})
        return {r['id'] for r in results}

    def _find_namespaces_for_changed_files(self, file_paths: list[str]) -> Set[str]:
        if not file_paths:
            return set()
        query = """
        UNWIND $file_paths as file_path
        MATCH (f:FILE {path: file_path})-[:DECLARES]->(ns:NAMESPACE)
        RETURN DISTINCT ns.id AS id
        """
        results = self.neo4j_mgr.execute_read_query(query, {"file_paths": list(file_paths)})
        return {r['id'] for r in results}

    def _find_files_for_updated_namespaces(self, namespace_ids: set) -> set:
        if not namespace_ids:
            return set()
        query = """
        UNWIND $namespace_ids as namespaceId
        MATCH (f:FILE)-[:DECLARES]->(ns:NAMESPACE) WHERE ns.id = namespaceId
        RETURN DISTINCT f.path AS path
        """
        results = self.neo4j_mgr.execute_read_query(query, {"namespace_ids": list(namespace_ids)})
        return {r['path'] for r in results}

    
