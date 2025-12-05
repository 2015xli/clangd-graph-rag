#!/usr/bin/env python3
"""
This script generates summaries and embeddings for nodes in a code graph.

It connects to an existing Neo4j database populated by the ingestion pipeline
and executes a multi-pass process to enrich the graph with AI-generated
summaries and vector embeddings, as outlined in docs/code_rag_generation_plan.md.
"""

import argparse
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, Callable, List, Optional, Set, Dict
from collections import defaultdict
from tqdm import tqdm

import re
import input_params
from rag_generation_prompts import RagGenerationPromptManager # New import

_SPECIAL_PATTERN = re.compile(r"<\|[^|]+?\|>")

def sanitize_special_tokens(text: str) -> str:
    """Break up special tokens so the model won't treat them as control tokens."""
    return _SPECIAL_PATTERN.sub(lambda m: f"< |{m.group(0)[2:-2]}| >", text)

from neo4j_manager import Neo4jManager, align_string
from llm_client import get_llm_client, LlmClient, get_embedding_client, EmbeddingClient
from summary_manager import SummaryManager

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# --- Main RAG Generation Logic ---

class RagGenerator:
    """Orchestrates the generation of RAG data.
    
    Designed with a separation of concerns:
    - Graph traversal methods are separate from
    - Single-item processing methods.
    """

    def __init__(self, neo4j_mgr: Neo4jManager, project_path: str, 
                 embedding_client: EmbeddingClient, 
                 summary_mgr: SummaryManager,
                 num_local_workers: int, num_remote_workers: int):
        self.neo4j_mgr = neo4j_mgr
        self.project_path = os.path.abspath(project_path)
        self.embedding_client = embedding_client
        self.summary_mgr = summary_mgr
        self.num_local_workers = num_local_workers
        self.num_remote_workers = num_remote_workers
        self.is_local_llm = summary_mgr.is_local_llm

    def _parallel_process(self, items: Iterable, process_func: Callable, max_workers: int, desc: str) -> list:
        """
        Processes items in parallel using a thread pool, shows a progress bar,
        and returns a list of the non-None results from the process_func.
        """
        if not items:
            return []

        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_func, item): item for item in items}
            
            for future in tqdm(as_completed(futures), total=len(items), desc=align_string(desc)):
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                except Exception as e:
                    item = futures[future]
                    logging.error(f"Error processing item {item}: {e}", exc_info=True)
        return results

    def summarize_code_graph(self):
        """Main orchestrator method to run all summarization passes for a full build."""
        self.summary_mgr.prepare() # Load cache at the beginning of the RAG process
        self.summarize_functions_individually()
        self.summarize_functions_with_context()
        self.summarize_class_structures()
        self.summarize_namespaces() # New call
        logging.info("--- Starting File and Folder Summarization ---")
        self._summarize_all_files()
        self._summarize_all_folders()
        self._summarize_project()
        logging.info("--- Finished File and Folder Summarization ---")
        self.generate_embeddings()
        self.summary_mgr.finalize(self.neo4j_mgr, mode="builder") # Finalize cache for builder mode

    def summarize_targeted_update(self, seed_symbol_ids: set, structurally_changed_files: dict):
        """
        Runs a targeted, multi-pass summarization handling both content and structural changes.
        """
        if not seed_symbol_ids and not any(structurally_changed_files.values()):
            logging.info("No seed symbols or structural changes provided for targeted update. Skipping.")
            return

        self.summary_mgr.prepare() # Load cache at the beginning of the RAG process
        logging.info(f"--- Starting Targeted RAG Update for {len(seed_symbol_ids)} seed symbols and {sum(len(v) for v in structurally_changed_files.values())} structural file changes ---")

        # --- Function Summary Passes (Content Changes) ---
        logging.info("Targeted Update - Pass 1: Summarizing changed functions individually...")
        updated_code_summary_ids = self._summarize_functions_individually_with_ids(list(seed_symbol_ids))
        logging.info(f"{len(updated_code_summary_ids)} functions received a new code summary.")

        logging.info("Targeted Update - Pass 2: Summarizing functions with context...")
        neighbor_ids = self._get_neighbor_ids(updated_code_summary_ids)
        all_function_ids_to_process = seed_symbol_ids.union(neighbor_ids)
        logging.info(f"Expanded scope for Pass 2 to {len(all_function_ids_to_process)} total functions.")
        
        updated_final_summary_ids = self._summarize_functions_with_context_with_ids(list(all_function_ids_to_process))
        logging.info(f"{len(updated_final_summary_ids)} functions received a new final summary.")

        # --- Targeted Class Summaries ---
        logging.info("Targeted Update - Pass 3: Summarizing changed class structures...")
        # Identify seed classes: those whose methods were updated, or whose files were changed, or whose are in the seed id set.
        seed_class_ids_from_seeds = self._find_classes_of_symbol_ids(seed_symbol_ids)
        seed_class_ids_from_functions = self._find_classes_for_updated_methods(updated_final_summary_ids)
        seed_class_ids_from_files = self._find_classes_for_changed_files(
            structurally_changed_files.get('added', []) + structurally_changed_files.get('modified', [])
        )
        all_seed_class_ids = seed_class_ids_from_functions.union(seed_class_ids_from_files).union(seed_class_ids_from_seeds)
        updated_class_summary_ids = self._summarize_targeted_class_structures(all_seed_class_ids)
        logging.info(f"{len(updated_class_summary_ids)} classes received a new summary.")

        # --- Targeted Namespace Summaries ---
        logging.info("Targeted Update - Pass 4: Summarizing changed namespaces...")
        # Identify seed namespaces: those whose children (functions/classes) were updated, or whose files were changed
        seed_namespace_ids_from_children = self._find_namespaces_for_updated_children(
            updated_final_summary_ids.union(updated_class_summary_ids)
        )
        seed_namespace_ids_from_files = self._find_namespaces_for_changed_files(
            structurally_changed_files.get('added', []) + structurally_changed_files.get('modified', [])
        )
        all_seed_namespace_ids = seed_namespace_ids_from_children.union(seed_namespace_ids_from_files)
        updated_namespace_summary_ids = self._summarize_targeted_namespaces(all_seed_namespace_ids)
        logging.info(f"{len(updated_namespace_summary_ids)} namespaces received a new summary.")

        # --- File & Folder Roll-up Passes (Content + Structural Changes) ---
        
        # 1. Identify files that trigger a file-level re-summary
        files_with_summary_changes = self._find_files_for_updated_symbols(updated_final_summary_ids)
        files_with_class_summary_changes = self._find_files_for_updated_classes(updated_class_summary_ids)
        files_with_namespace_summary_changes = self._find_files_for_updated_namespaces(updated_namespace_summary_ids)
        added_files = set(structurally_changed_files.get('added', []))
        modified_files = set(structurally_changed_files.get('modified', []))
        files_to_resummarize = files_with_summary_changes.union(files_with_class_summary_changes).union(files_with_namespace_summary_changes).union(added_files).union(modified_files)
        self._summarize_files_with_paths(files_to_resummarize)

        # 2. Identify all folders that need their summaries rolled up
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
            
            self._summarize_folders_with_paths(all_affected_folders_paths)
            self._summarize_project()

        # --- Final Pass ---
        self.generate_embeddings()
        logging.info("--- Finished Targeted RAG Update ---")
        self.summary_mgr.finalize(self.neo4j_mgr, mode="updater") # Finalize cache for updater mode

    def _get_neighbor_ids(self, seed_symbol_ids: set) -> set:
        """Finds the 1-hop callers and callees of the seed symbols."""
        if not seed_symbol_ids:
            return set()
        
        query = """
        UNWIND $seed_ids AS seedId
        MATCH (n) WHERE n.id = seedId AND (n:FUNCTION OR n:METHOD)
        // Match direct callers and callees
        OPTIONAL MATCH (neighbor)-[:CALLS*1]-(n) WHERE (neighbor:FUNCTION OR neighbor:METHOD)
        WITH collect(DISTINCT n.id) + collect(DISTINCT neighbor.id) AS allIds
        UNWIND allIds as id
        RETURN collect(DISTINCT id) as ids
        """
        result = self.neo4j_mgr.execute_read_query(query, {"seed_ids": list(seed_symbol_ids)})
        if result and result[0] and result[0]['ids']:
            return set(result[0]['ids'])
        return seed_symbol_ids

    def _find_files_for_updated_symbols(self, symbol_ids: set) -> set:
        """Finds the file paths that define a given set of symbols."""
        if not symbol_ids:
            return set()
        # Optimized query with DISTINCT and a label hint on the symbol node.
        query = """
        UNWIND $symbol_ids as symbolId
        MATCH (f:FILE)-[:DEFINES]->(s) WHERE s.id = symbolId AND (s:FUNCTION OR s:METHOD)
        RETURN DISTINCT f.path AS path
        """
        results = self.neo4j_mgr.execute_read_query(query, {"symbol_ids": list(symbol_ids)})
        return {r['path'] for r in results}

    def _find_classes_of_symbol_ids(self, symbol_ids: set) -> set:
        """Finds the CLASS_STRUCTURE nodes that contain the given symbols."""
        if not symbol_ids:
            return set()
        query = """
            MATCH (c:CLASS_STRUCTURE) 
            WHERE c.id IN $symbol_ids 
            RETURN DISTINCT c.id AS id
        """
        results = self.neo4j_mgr.execute_read_query(query, {"symbol_ids": list(symbol_ids)})
        return results
    
    def _find_classes_for_updated_methods(self, method_ids: set) -> set:
        """Finds the CLASS_STRUCTURE nodes that contain the given methods."""
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
        """Finds the CLASS_STRUCTURE nodes defined or declared in the given file paths."""
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
        """Finds the file paths that define or declare a given set of classes."""
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
        """Finds the NAMESPACE nodes that contain the given child nodes."""
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
        """Finds the NAMESPACE nodes declared in the given file paths."""
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
        """Finds the file paths that declare a given set of namespaces."""
        if not namespace_ids:
            return set()
        query = """
        UNWIND $namespace_ids as namespaceId
        MATCH (f:FILE)-[:DECLARES]->(ns:NAMESPACE) WHERE ns.id = namespaceId
        RETURN DISTINCT f.path AS path
        """
        results = self.neo4j_mgr.execute_read_query(query, {"namespace_ids": list(namespace_ids)})
        return {r['path'] for r in results}

    # --- Pass 1 Methods ---
    def summarize_functions_individually(self):
        """PASS 1: Generates a code-only summary for all functions and methods in the graph."""
        logging.info("\n--- Starting Pass 1: Summarizing Functions & Methods Individually ---")
        
        query = "MATCH (n) WHERE (n:FUNCTION OR n:METHOD) AND n.body_location IS NOT NULL RETURN n.id AS id"
        results = self.neo4j_mgr.execute_read_query(query)
        all_function_ids = [r['id'] for r in results]

        if not all_function_ids:
            logging.warning("No functions or methods with body_location found. Exiting Pass 1.")
            return
        
        self._summarize_functions_individually_with_ids(all_function_ids)
        logging.info("--- Finished Pass 1 ---")

    def _summarize_functions_individually_with_ids(self, function_ids: list[str]) -> set:
        """
        Core logic for Pass 1, operating on a specific list of function/method IDs.
        Returns the set of IDs that were actually updated.
        """
        if not function_ids:
            return set()
            
        functions_to_process = self._get_functions_for_code_summary(function_ids)
        if not functions_to_process:
            logging.info("No functions or methods from the provided list require a code summary.")
            return set()
            
        logging.info(f"Found {len(functions_to_process)} functions/methods that need code summaries.")
        max_workers = self.num_local_workers if self.summary_mgr.is_local_llm else self.num_remote_workers
        logging.info(f"Using {max_workers} parallel workers for Pass 1.")

        updated_ids = self._parallel_process(
            items=functions_to_process,
            process_func=self._process_one_function_for_code_summary,
            max_workers=max_workers,
            desc="Pass 1: Code Summaries"
        )
        return set(updated_ids)

    def _get_functions_for_code_summary(self, function_ids: list[str]) -> list[dict]:
        query = """
        MATCH (n:FUNCTION|METHOD)
        WHERE n.id IN $function_ids AND n.body_location IS NOT NULL
        RETURN n.id AS id, n.name AS name, n.path AS path, n.body_location as body_location,
               n.code_hash as db_code_hash, n.codeSummary as db_codeSummary, labels(n)[-1] as label
        """
        return self.neo4j_mgr.execute_read_query(query, {"function_ids": function_ids})

    def _process_one_function_for_code_summary(self, func: dict) -> str | None:
        """
        Orchestrates getting a code summary and updating the DB if necessary.
        The core logic is delegated to the SummaryManager.
        """
        func_id = func['id']

        # Step 1: Get source code.
        body_location = func.get('body_location')
        file_path = func.get('path')
        if not body_location or not file_path:
            logging.warning(f"Invalid or missing body_location/path for function {func_id}. Skipping.")
            return None
        
        start_line, _, end_line, _ = body_location
        source_code = self._get_source_code_for_location(file_path, start_line, end_line)
        if not source_code:
            return None

        # Step 2: Get a valid summary from the SummaryManager.
        code_hash, code_summary = self.summary_mgr.get_code_summary(func, source_code)

        # Step 3: If a new/updated summary was returned, update the database.
        if code_summary:
            update_query = "MATCH (n:FUNCTION|METHOD {id: $id}) SET n.codeSummary = $code_summary, n.code_hash = $code_hash"
            self.neo4j_mgr.execute_autocommit_query(
                update_query, 
                {"id": func_id, "code_summary": code_summary, "code_hash": code_hash}
            )
            return func_id
        
        return None

    # --- Pass 2 Methods ---
    def summarize_functions_with_context(self):
        """PASS 2: Generates a final, context-aware summary for all functions and methods."""
        logging.info("--- Starting Pass 2: Summarizing Functions & Methods With Context ---")
        
        query = """
        MATCH (n:FUNCTION|METHOD)
        WHERE n.codeSummary IS NOT NULL
        RETURN n.id AS id
        """
        results = self.neo4j_mgr.execute_read_query(query)
        if not results:
            logging.info("No items require summarization in Pass 2.")
            return
        
        all_function_ids = [r['id'] for r in results]
        
        updated_ids = self._summarize_functions_with_context_with_ids(all_function_ids)
        
        logging.info(f"Pass 2 complete. Updated contextual summaries for {len(updated_ids)} functions.")
        logging.info("--- Finished Pass 2 ---")

    def _summarize_functions_with_context_with_ids(self, function_ids: list[str]) -> set:
        """
        Core logic for Pass 2, operating on a specific list of function/method IDs.
        Returns the set of function IDs that were actually updated.
        """
        if not function_ids:
            return set()

        logging.info(f"Found {len(function_ids)} functions/methods that need a final summary.")
        max_workers = self.num_local_workers if self.summary_mgr.is_local_llm else self.num_remote_workers
        logging.info(f"Using {max_workers} parallel workers for Pass 2.")

        updated_ids = self._parallel_process(
            items=function_ids,
            process_func=self._process_one_function_for_contextual_summary,
            max_workers=max_workers,
            desc="Pass 2: Context Summaries"
        )
        return set(updated_ids)

    def _process_one_function_for_contextual_summary(self, func_id: str) -> str | None:
        """
        Orchestrates getting a contextual summary by delegating all logic
        to the SummaryManager.
        """
        # Step 1: Get the entity and its dependencies from the graph.
        entity_query = "MATCH (n:FUNCTION|METHOD) WHERE n.id = $id RETURN n.id as id, labels(n)[-1] as label, n.summary as db_summary"
        entity_result = self.neo4j_mgr.execute_read_query(entity_query, {"id": func_id})
        if not entity_result:
            logging.warning(f"Could not find function with ID {func_id} for contextual summary.")
            return None
        entity = entity_result[0]

        context_query = """
        MATCH (n:FUNCTION|METHOD) WHERE n.id = $id
        OPTIONAL MATCH (caller:FUNCTION|METHOD)-[:CALLS]->(n) where caller.codeSummary IS NOT NULL
        OPTIONAL MATCH (n)-[:CALLS]->(callee:FUNCTION|METHOD) where callee.codeSummary IS NOT NULL
        RETURN collect(DISTINCT {id: caller.id, label: labels(caller)[-1]}) AS callers,
               collect(DISTINCT {id: callee.id, label: labels(callee)[-1]}) AS callees
        """
        context_results = self.neo4j_mgr.execute_read_query(context_query, {"id": func_id})
        if not context_results: return None
        
        caller_entities = context_results[0].get('callers') 
        caller_entities = caller_entities if caller_entities[0]['id'] else []
        callee_entities = context_results[0].get('callees', [])
        callee_entities = callee_entities if callee_entities[0]['id'] else []

        # Step 2: Delegate all caching and generation logic to the SummaryManager.
        final_summary = self.summary_mgr.get_function_contextual_summary(
            entity, caller_entities, callee_entities
        )

        # Step 3: Update the database if a new summary was returned.
        if final_summary:
            update_query = "MATCH (n:FUNCTION|METHOD {id: $id}) SET n.summary = $summary REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(update_query, {"id": func_id, "summary": final_summary})
            return func_id
            
        return None


    # --- Pass 3 Methods: Class Summaries ---
    def _get_classes_by_inheritance_level(self, target_class_ids: Optional[Set[str]] = None) -> Dict[int, List[Dict]]:
        """
        Fetches CLASS_STRUCTURE nodes and groups them by their inheritance level.
        If target_class_ids is provided, it fetches those classes and their ancestors
        to ensure correct inheritance ordering.
        """
        classes_by_level = defaultdict(list)
        visited_ids = set()
        current_level = 0

        # Step 1: Determine the set of all relevant class IDs to consider.
        # If target_class_ids is provided, we need to include them and all their ancestors.
        # Otherwise, we consider all CLASS_STRUCTURE nodes in the graph.
        if target_class_ids:
            # Query to get target classes and all their ancestors
            query_all_relevant_ids = """
                MATCH (c:CLASS_STRUCTURE)
                WHERE c.id IN $target_class_ids
                OPTIONAL MATCH (c)-[:INHERITS*0..]->(ancestor:CLASS_STRUCTURE)
                RETURN DISTINCT ancestor.id AS id
            """
            result = self.neo4j_mgr.execute_read_query(query_all_relevant_ids, {"target_class_ids": list(target_class_ids)})
            all_relevant_ids = {r['id'] for r in result}
            if not all_relevant_ids:
                return {} # No relevant classes found
        else:
            # For full build, get all class IDs
            query_all_ids = "MATCH (c:CLASS_STRUCTURE) RETURN c.id AS id"
            result = self.neo4j_mgr.execute_read_query(query_all_ids)
            all_relevant_ids = {r['id'] for r in result}
            if not all_relevant_ids:
                return {} # No classes in the graph

        # --- LEVEL 0: Get root classes (no parents) from the relevant set ---
        level_0_query = """
            MATCH (c:CLASS_STRUCTURE)
            WHERE c.id IN $all_relevant_ids AND NOT (c)-[:INHERITS]->(:CLASS_STRUCTURE)
            RETURN collect({id: c.id, name: c.name}) AS classes
        """
        result = self.neo4j_mgr.execute_read_query(level_0_query, {"all_relevant_ids": list(all_relevant_ids)})
        level_nodes = result[0]['classes'] if result and result[0]['classes'] else []
        
        if not level_nodes:
            return {}

        classes_by_level[current_level] = level_nodes
        visited_ids.update(n['id'] for n in level_nodes)

        # --- SUBSEQUENT LEVELS ---
        while level_nodes:
            current_level += 1
            
            next_level_query = """
                MATCH (c:CLASS_STRUCTURE)
                WHERE (c.id IN $all_relevant_ids) AND NOT (c.id IN $visited_ids)
                WITH c
                MATCH (c)-[:INHERITS]->(p:CLASS_STRUCTURE)
                WITH c, collect(p.id) AS parent_ids
                WHERE size(parent_ids) > 0 AND all(pid IN parent_ids WHERE pid IN $visited_ids)
                RETURN collect({id: c.id, name: c.name}) AS classes
            """
            result = self.neo4j_mgr.execute_read_query(
                next_level_query, 
                {"all_relevant_ids": list(all_relevant_ids), "visited_ids": list(visited_ids)}
            )
            level_nodes = result[0]['classes'] if result and result[0]['classes'] else []

            if level_nodes:
                classes_by_level[current_level] = level_nodes
                visited_ids.update(n['id'] for n in level_nodes)
        
        return classes_by_level

    def summarize_class_structures(self):
        """
        PASS 3: Generates summaries for class structures, processing level by level
        in parallel to ensure parent summaries are available.
        """
        logging.info("\n--- Starting Pass 3: Summarizing Class Structures (Level by Level) ---")
        
        classes_by_level = self._get_classes_by_inheritance_level()
        
        if not classes_by_level:
            logging.info("No class structures found to summarize.")
            return

        max_workers = self.num_local_workers if self.summary_mgr.is_local_llm else self.num_remote_workers

        for level in sorted(classes_by_level.keys()):
            items_to_process = classes_by_level[level]
            
            logging.info(f"Summarizing {len(items_to_process)} classes at inheritance level {level}.")
            
            self._parallel_process(
                items=items_to_process,
                process_func=self._summarize_one_class_structure,
                max_workers=max_workers,
                desc=f"Pass 3: Class Summaries (Level {level})"
            )
            
        logging.info("--- Finished Pass 3 ---")

    def _summarize_targeted_class_structures(self, seed_class_ids: Set[str]) -> Set[str]:
        """
        Generates summaries for a targeted set of CLASS_STRUCTURE nodes, processing level by level
        in parallel to ensure parent summaries are available.
        """
        if not seed_class_ids:
            logging.info("No seed class IDs provided for targeted summarization. Skipping.")
            return set()

        logging.info(f"\n--- Starting Targeted Class Summaries for {len(seed_class_ids)} seed classes ---")

        # Get classes by inheritance level, including ancestors if necessary
        classes_by_level = self._get_classes_by_inheritance_level(seed_class_ids)
        
        if not classes_by_level:
            logging.info("No class structures found to summarize after inheritance level grouping.")
            return set()

        max_workers = self.num_local_workers if self.summary_mgr.is_local_llm else self.num_remote_workers
        updated_class_ids = set()

        for level in sorted(classes_by_level.keys()):
            items_to_process = classes_by_level[level]
            
            logging.info(f"Summarizing {len(items_to_process)} classes at inheritance level {level}.")
            
            # _summarize_one_class_structure returns the ID of the class if its summary was updated
            updated_ids_in_level = self._parallel_process(
                items=items_to_process,
                process_func=self._summarize_one_class_structure,
                max_workers=max_workers,
                desc=f"Targeted Class Summaries (Level {level})"
            )
            updated_class_ids.update(updated_ids_in_level)
            
        logging.info(f"--- Finished Targeted Class Summaries. Updated {len(updated_class_ids)} classes. ---")
        return updated_class_ids

    def _summarize_one_class_structure(self, class_info: dict) -> str | None:
        """
        Orchestrates getting a class summary by delegating all logic
        to the SummaryManager.
        """
        class_id = class_info['id']

        # Step 1: Get the full entity and its dependencies from the graph.
        context_query = """
        MATCH (c:CLASS_STRUCTURE {id: $id})
        OPTIONAL MATCH (c)-[:INHERITS]->(p:CLASS_STRUCTURE)
        OPTIONAL MATCH (c)-[:HAS_METHOD]->(m:METHOD) where m.summary IS NOT NULL
        OPTIONAL MATCH (c)-[:HAS_FIELD]->(f:FIELD)
        RETURN c.id as id, labels(c)[-1] as label, c.name as name, c.summary as db_summary,
               collect(DISTINCT {id: p.id, label: 'CLASS_STRUCTURE'}) AS parents,
               collect(DISTINCT {id: m.id, label: 'METHOD'}) AS methods,
               collect(DISTINCT {name: f.name, type: f.type}) as fields
        """
        result = self.neo4j_mgr.execute_read_query(context_query, {"id": class_id})
        if not result:
            logging.warning(f"Could not find class with ID {class_id} for summary.")
            return None
        
        class_entity = result[0]
        parent_entities = class_entity.get('parents', [])
        if parent_entities and parent_entities[0]['id'] == None:
            parent_entities = []
        method_entities = class_entity.get('methods', [])
        if method_entities and method_entities[0]['id'] == None:
            method_entities = []
        fields = class_entity.get('fields', [])
        if fields and fields[0]['name'] == None:
            fields = []

        # Step 2: Delegate all caching and generation logic to the SummaryManager.
        final_summary = self.summary_mgr.get_class_summary(
            class_entity, parent_entities, method_entities, fields
        )

        # Step 3: Update the database if a new summary was returned.
        if final_summary:
            update_query = "MATCH (n {id: $id}) SET n.summary = $summary REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(update_query, {"id": class_id, "summary": final_summary})
            return class_id
            
        return None

    # --- Pass 4 Methods: Namespace Summaries ---
    def summarize_namespaces(self):
        """PASS 4: Generates a summary for all namespace nodes in the graph."""
        logging.info("\n--- Starting Pass 4: Summarizing Namespaces ---")

        all_namespaces = self._get_namespaces_for_summary()
        if not all_namespaces:
            logging.info("No namespaces require summarization.")
            return

        # Group namespaces by depth
        namespaces_by_depth = defaultdict(list)
        for ns in all_namespaces:
            depth = ns['qualified_name'].count('::')
            namespaces_by_depth[depth].append(ns)

        if not namespaces_by_depth:
            logging.info("No namespaces to process after grouping by depth.")
            return

        logging.info(f"Found {len(all_namespaces)} namespaces to summarize, grouped into {len(namespaces_by_depth)} depth levels.")
        max_workers = self.num_local_workers if self.summary_mgr.is_local_llm else self.num_remote_workers

        # Process level by level, from deepest to shallowest
        for depth in sorted(namespaces_by_depth.keys(), reverse=True):
            items_to_process = namespaces_by_depth[depth]
            logging.info(f"Summarizing {len(items_to_process)} namespaces at depth {depth}.")
            
            self._parallel_process(
                items=items_to_process,
                process_func=self._summarize_one_namespace,
                max_workers=max_workers,
                desc=f"Pass 4: Namespace Summaries (Depth {depth})"
            )
        logging.info("--- Finished Pass 4 ---")

    def _summarize_targeted_namespaces(self, seed_namespace_ids: Set[str]) -> Set[str]:
        """
        Generates summaries for a targeted set of NAMESPACE nodes, processing level by level
        from deepest to shallowest.
        """
        if not seed_namespace_ids:
            logging.info("No seed namespace IDs provided for targeted summarization. Skipping.")
            return set()

        logging.info(f"\n--- Starting Targeted Namespace Summaries for {len(seed_namespace_ids)} seed namespaces ---")

        namespaces_by_depth = self._get_targeted_namespaces_by_depth(seed_namespace_ids)
        
        if not namespaces_by_depth:
            logging.info("No namespaces found to summarize after depth grouping.")
            return set()

        max_workers = self.num_local_workers if self.summary_mgr.is_local_llm else self.num_remote_workers
        updated_namespace_ids = set()

        # Process level by level, from deepest to shallowest
        for depth in sorted(namespaces_by_depth.keys(), reverse=True):
            items_to_process = namespaces_by_depth[depth]
            
            logging.info(f"Summarizing {len(items_to_process)} namespaces at depth {depth}.")
            
            # _summarize_one_namespace returns the ID of the namespace if its summary was updated
            updated_ids_in_level = self._parallel_process(
                items=items_to_process,
                process_func=self._summarize_one_namespace,
                max_workers=max_workers,
                desc=f"Targeted Namespace Summaries (Depth {depth})"
            )
            updated_namespace_ids.update(updated_ids_in_level)
            
        logging.info(f"--- Finished Targeted Namespace Summaries. Updated {len(updated_namespace_ids)} namespaces. ---")
        return updated_namespace_ids

    def _get_namespaces_for_summary(self) -> list[dict]:
        """Fetches all NAMESPACE nodes with their necessary properties."""
        query = """
        MATCH (n:NAMESPACE)
        RETURN n.id as id, n.qualified_name AS qualified_name, n.name AS name, n.summary as db_summary
        """
        return self.neo4j_mgr.execute_read_query(query)

    def _get_targeted_namespaces_by_depth(self, namespace_ids: Set[str]) -> Dict[int, List[Dict]]:
        """
        Fetches NAMESPACE nodes from the given set and groups them by their nesting depth.
        """
        if not namespace_ids:
            return {}

        namespaces_by_depth = defaultdict(list)

        # Fetch the qualified_name for the target namespaces
        query = """
        MATCH (n:NAMESPACE)
        WHERE n.id IN $namespace_ids
        RETURN n.id as id, n.qualified_name AS qualified_name, n.name AS name
        """
        results = self.neo4j_mgr.execute_read_query(query, {"namespace_ids": list(namespace_ids)})

        for ns in results:
            # Calculate depth based on '::' count in qualified_name
            depth = ns['qualified_name'].count('::')
            namespaces_by_depth[depth].append(ns)
        
        return namespaces_by_depth

    def _summarize_one_namespace(self, namespace_info: dict) -> str | None:
        """Orchestrates the generation of a summary for a single namespace."""
        ns_id = namespace_info['id']
        
        # Step 1: Get child dependencies from the graph.
        context_query = """
        MATCH (ns:NAMESPACE {id: $id})-[:SCOPE_CONTAINS]->(child)
        WHERE child.summary IS NOT NULL
        RETURN collect(DISTINCT {id: child.id, label: labels(child)[-1], name: child.name}) as children
        """
        results = self.neo4j_mgr.execute_read_query(context_query, {"id": ns_id})
        child_entities = results[0]['children'] if results and results[0]['children'] else []

        # Step 2: Delegate to the SummaryManager.
        final_summary = self.summary_mgr.get_namespace_summary(namespace_info, child_entities)

        # Step 3: Update the database if a new summary was returned.
        if final_summary:
            update_query = "MATCH (n:NAMESPACE {id: $id}) SET n.summary = $summary REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(update_query, {"id": ns_id, "summary": final_summary})
            return ns_id
        
        return None

    # --- Pass 5 Methods ---
    def _summarize_all_files(self):
        logging.info("\n--- Starting Pass 5: Summarizing All Files ---")
        query = "MATCH (f:FILE) RETURN f.path AS path, labels(f)[-1] as label, f.summary as db_summary"
        files_to_process = self.neo4j_mgr.execute_read_query(query)
        if not files_to_process:
            logging.info("No files found to summarize.")
            return
        
        self._summarize_files_with_paths(files_to_process)

    def _summarize_files_with_paths(self, file_entities: list[dict]):
        """Core logic for summarizing a specific set of FILE nodes."""
        if not file_entities:
            return
        logging.info(f"Summarizing {len(file_entities)} FILE nodes...")
        max_workers = self.num_local_workers if self.summary_mgr.is_local_llm else self.num_remote_workers
        self._parallel_process(
            items=file_entities,
            process_func=self._summarize_one_file,
            max_workers=max_workers,
            desc="File Summaries"
        )

    def _summarize_one_file(self, file_entity: dict):
        file_path = file_entity['path']
        query = """
        MATCH (f:FILE {path: $path})-[:DEFINES]->(s)
        WHERE (s:FUNCTION OR s:CLASS_STRUCTURE) AND s.summary IS NOT NULL
        RETURN collect(DISTINCT {id: s.id, label: labels(s)[-1], name: s.name}) as children
        """
        results = self.neo4j_mgr.execute_read_query(query, {"path": file_path})
        child_entities = results[0]['children'] if results and results[0]['children'] else []

        final_summary = self.summary_mgr.get_file_summary(file_entity, child_entities)

        if final_summary:
            update_query = "MATCH (f:FILE {path: $path}) SET f.summary = $summary REMOVE f.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(update_query, {"path": file_path, "summary": final_summary})
            return file_path
        return None

    # --- Pass 6 Methods ---
    def _summarize_all_folders(self):
        logging.info("\n--- Starting Pass 6: Summarizing All Folders (bottom-up) ---")
        query = "MATCH (f:FOLDER) RETURN f.path as path, f.name as name, labels(f)[-1] as label, f.summary as db_summary"
        folders_to_process = self.neo4j_mgr.execute_read_query(query)
        if not folders_to_process:
            logging.info("No folders found to summarize.")
            return

        self._summarize_folders_with_paths(folders_to_process)

    def _summarize_folders_with_paths(self, folder_entities: list[dict]):
        """Core logic for summarizing a specific set of FOLDER nodes."""
        if not folder_entities:
            return

        logging.info(f"Rolling up summaries for {len(folder_entities)} FOLDER nodes...")
        folders_by_depth = defaultdict(list)
        for folder in folder_entities:
            depth = folder['path'].count(os.sep)
            folders_by_depth[depth].append(folder)

        max_workers = self.num_local_workers if self.summary_mgr.is_local_llm else self.num_remote_workers
        for depth in sorted(folders_by_depth.keys(), reverse=True):
            self._parallel_process(
                items=folders_by_depth[depth],
                process_func=self._summarize_one_folder,
                max_workers=max_workers,
                desc=f"Folder Roll-up (Depth {depth})"
            )

    def _summarize_one_folder(self, folder_entity: dict):
        folder_path = folder_entity['path']
        query = """
        MATCH (parent:FOLDER {path: $path})-[:CONTAINS]->(child)
        WHERE child.summary IS NOT NULL
        RETURN collect(DISTINCT {id: child.id, path: child.path, label: labels(child)[-1], name: child.name}) as children
        """
        results = self.neo4j_mgr.execute_read_query(query, {"path": folder_path})
        child_entities = results[0]['children'] if results and results[0]['children'] else []

        final_summary = self.summary_mgr.get_folder_summary(folder_entity, child_entities)

        if final_summary:
            update_query = "MATCH (f:FOLDER {path: $path}) SET f.summary = $summary REMOVE f.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(update_query, {"path": folder_path, "summary": final_summary})
            return folder_path
        return None

    def _summarize_project(self):
        """Summarizes the top-level PROJECT node."""
        logging.info("Summarizing the PROJECT node...")
        
        entity_query = "MATCH (p:PROJECT) RETURN p.path as path, p.name as name, labels(p)[-1] as label, p.summary as db_summary"
        project_entity = self.neo4j_mgr.execute_read_query(entity_query)[0]
        
        children_query = """
        MATCH (p:PROJECT)-[:CONTAINS]->(child)
        WHERE child.summary IS NOT NULL
        RETURN collect(DISTINCT {id: child.id, path: child.path, label: labels(child)[-1], name: child.name}) as children
        """
        results = self.neo4j_mgr.execute_read_query(children_query)
        child_entities = results[0]['children'] if results and results[0]['children'] else []

        final_summary = self.summary_mgr.get_project_summary(project_entity, child_entities)

        if final_summary:
            update_query = "MATCH (p:PROJECT) SET p.summary = $summary REMOVE p.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(update_query, {"summary": final_summary})
            logging.info("-> Stored summary for PROJECT node.")
        return None

    # --- Pass 7 Methods ---
    def generate_embeddings(self):
        logging.info("\n--- Starting Generating Embeddings ---")
        nodes_to_embed = self._get_nodes_for_embedding()
        if not nodes_to_embed:
            logging.info("No nodes require embedding.")
            return

        logging.info(f"Found {len(nodes_to_embed)} nodes with summaries to embed.")

        # Step 1: Batch generate embeddings
        # The sentence-transformer library will show its own progress bar here.
        summaries = [node['summary'] for node in nodes_to_embed]
        embeddings = self.embedding_client.generate_embeddings(summaries)

        # Step 2: Prepare data for batch database update
        update_params = []
        for node, embedding in zip(nodes_to_embed, embeddings):
            if embedding:
                update_params.append({
                    'elementId': node['elementId'],
                    'embedding': embedding
                })

        if not update_params:
            logging.warning("Embedding generation resulted in no data to update.")
            return

        # Step 3: Batch update the database
        ingest_batch_size = 1000  # Sensible batch size for DB updates
        logging.info(f"Updating {len(update_params)} nodes in the database in batches of {ingest_batch_size}...")
        
        update_query = """
        UNWIND $batch AS data
        MATCH (n) WHERE elementId(n) = data.elementId
        SET n.summaryEmbedding = data.embedding
        """
        
        for i in tqdm(range(0, len(update_params), ingest_batch_size), desc=align_string("Updating DB")):
            batch = update_params[i:i + ingest_batch_size]
            self.neo4j_mgr.execute_autocommit_query(update_query, params={'batch': batch})

        logging.info("--- Finished Generating Embeddings ---")

    def _get_nodes_for_embedding(self) -> list[dict]:
        # This query finds any node with a final summary but no embedding yet.
        query = """
        MATCH (n)
        WHERE (n:FUNCTION OR n:METHOD OR n:CLASS_STRUCTURE OR n:NAMESPACE OR n:FILE OR n:FOLDER OR n:PROJECT)
          AND n.summary IS NOT NULL
          AND n.summaryEmbedding IS NULL
        RETURN elementId(n) AS elementId, n.summary AS summary
        """
        return self.neo4j_mgr.execute_read_query(query)

    

    # --- Utility Methods ---
    def _get_source_code_for_location(self, file_path: str, start_line: int, end_line: int) -> str:
        # The file_path from the node is relative, construct the absolute path
        full_path = os.path.join(self.project_path, file_path)

        if not os.path.exists(full_path):
            logging.warning(f"File not found when trying to extract source: {full_path}")
            return ""
        
        try:
            with open(full_path, 'r', errors='ignore') as f:
                lines = f.readlines()
            # Adjust for 0-based line numbers
            code_lines = lines[start_line : end_line + 1]
            return "".join(code_lines)
        except Exception as e:
            logging.error(f"Error reading file {full_path}: {e}")
            return ""

import input_params
from pathlib import Path

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    parser = argparse.ArgumentParser(description='Generate summaries and embeddings for a code graph.')
    
    input_params.add_core_input_args(parser)
    input_params.add_rag_args(parser)
    input_params.add_worker_args(parser)

    args = parser.parse_args()
    args.project_path = str(args.project_path.resolve())

    try:
        embedding_client = get_embedding_client(args.llm_api)
        
        summary_mgr = SummaryManager(
            project_path=args.project_path,
            llm_api=args.llm_api,
            token_encoding=args.token_encoding,
            max_context_token_size=args.max_context_size
        )

        with Neo4jManager() as neo4j_mgr:
            if not neo4j_mgr.check_connection(): return 1
            if not neo4j_mgr.verify_project_path(args.project_path): return 1
            
            generator = RagGenerator(
                neo4j_mgr, 
                args.project_path, 
                embedding_client,
                summary_mgr,
                args.num_local_workers,
                args.num_remote_workers
            )
            
            generator.summarize_code_graph()
            neo4j_mgr.create_vector_indices()

    except Exception as e:
        logging.critical(f"A critical error occurred: {e}", exc_info=True)
        return 1

if __name__ == "__main__":
    main()
