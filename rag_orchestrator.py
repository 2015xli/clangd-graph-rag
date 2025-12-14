#!/usr/bin/env python3
"""
Base orchestrator for RAG generation, containing common worker methods.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, Callable, List, Optional, Set, Dict
from collections import defaultdict
from tqdm import tqdm

from neo4j_manager import Neo4jManager, align_string
from llm_client import get_llm_client, get_embedding_client
from rag_generation_prompts import RagGenerationPromptManager
from summary_cache_manager import SummaryCacheManager
from node_summary_processor import NodeSummaryProcessor

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class RagOrchestrator:
    """Base class for RAG orchestration, containing shared logic."""

    def __init__(self, neo4j_mgr: Neo4jManager, project_path: str, 
                 args: dict):
        self.neo4j_mgr = neo4j_mgr
        self.project_path = os.path.abspath(project_path)
        self.embedding_client = get_embedding_client(args.llm_api)
        self.args = args

        # New component-based architecture
        self.summary_cache_manager = SummaryCacheManager(project_path)
        
        llm_client = get_llm_client(args.llm_api)
        prompt_manager = RagGenerationPromptManager()
        self.node_processor = NodeSummaryProcessor(
            project_path=project_path,
            cache_manager=self.summary_cache_manager,
            llm_client=llm_client,
            prompt_manager=prompt_manager,
            token_encoding=args.token_encoding,
            max_context_token_size=args.max_context_size
        )
        
        self.num_local_workers = args.num_local_workers
        self.num_remote_workers = args.num_remote_workers
        self.is_local_llm = llm_client.is_local

    def _parallel_process(self, items: Iterable, process_func: Callable, max_workers: int, desc: str) -> Set[str]:
        """
        Processes items in parallel and reduces the results serially.
        - Manages a thread pool for the "map" phase.
        - As workers complete, serially processes their results in the "reduce" phase.
        - Returns a set of keys for items that were successfully changed.
        """
        if not items:
            return set()

        updated_keys = set()
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_func, item): item for item in items}
            
            with tqdm(total=len(items), desc=align_string(desc)) as pbar:
                for future in as_completed(futures):
                    try:
                        result_packet = future.result()
                        if not result_packet:
                            continue

                        # Reduce phase: serially update cache and status
                        key = result_packet["key"]
                        label = result_packet["label"]
                        status = result_packet["status"]
                        data = result_packet["data"]

                        # Only update the cache if the data packet contains a valid, non-empty summary.
                        # This prevents 'summary: None' or empty data from polluting the cache.
                        if data and (data.get('summary') or data.get('codeSummary')):
                            self.summary_cache_manager.update_cache_entry(label, key, data)
                        
                        self.summary_cache_manager.set_runtime_status(label, key, "visited")

                        # Add to updated_keys if the DB was successfully touched
                        if status in ["summary_regenerated", "summary_restored", "code_summary_regenerated", "code_summary_restored"]:
                            updated_keys.add(key)

                        # Conditionally set flags for dependency tracking
                        if status == "code_summary_regenerated":
                            self.summary_cache_manager.set_runtime_status(label, key, "code_summary_changed")
                        elif status == "summary_regenerated":
                            self.summary_cache_manager.set_runtime_status(label, key, "summary_changed")

                    except Exception as e:
                        item = futures[future]
                        logging.error(f"Error processing item {item}: {e}", exc_info=True)
                    finally:
                        pbar.update(1)
        
        return updated_keys

    def generate_embeddings(self):
        logging.info("\n--- Starting Generating Embeddings ---")
        nodes_to_embed = self._get_nodes_for_embedding()
        if not nodes_to_embed:
            logging.info("No nodes require embedding.")
            return

        logging.info(f"Found {len(nodes_to_embed)} nodes with summaries to embed.")

        summaries = [node['summary'] for node in nodes_to_embed]
        embeddings = self.embedding_client.generate_embeddings(summaries)

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

        ingest_batch_size = 1000
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
        query = """
        MATCH (n)
        WHERE (n:FUNCTION OR n:METHOD OR n:CLASS_STRUCTURE OR n:NAMESPACE OR n:FILE OR n:FOLDER OR n:PROJECT)
          AND n.summary IS NOT NULL
        RETURN elementId(n) AS elementId, n.summary AS summary
        """
        return self.neo4j_mgr.execute_read_query(query)

    def _summarize_functions_individually_with_ids(self, function_ids: list[str]) -> set:
        """
        Orchestrates the map-reduce process for generating code summaries.
        """
        if not function_ids:
            return set()
            
        functions_to_process = self._get_functions_for_code_summary(function_ids)
        if not functions_to_process:
            logging.info("No functions or methods from the provided list require a code summary.")
            return set()
            
        logging.info(f"Found {len(functions_to_process)} functions/methods that need code summaries.")
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers
        logging.info(f"Using {max_workers} parallel workers for Pass 1.")

        updated_ids = self._parallel_process(
            items=functions_to_process,
            process_func=self._process_one_function_for_code_summary,
            max_workers=max_workers,
            desc="Pass 1: Code Summaries"
        )
        logging.info(f"Pass 1: Code Summaries - Updated {len(updated_ids)} nodes.")
        return updated_ids

    def _get_functions_for_code_summary(self, function_ids: list[str]) -> list[dict]:
        query = """
        MATCH (n:FUNCTION|METHOD)
        WHERE n.id IN $function_ids AND n.body_location IS NOT NULL
        RETURN n.id AS id, n.name AS name, n.path AS path, n.body_location as body_location,
               n.code_hash as db_code_hash, n.codeSummary as db_codeSummary, labels(n)[-1] as label
        """
        return self.neo4j_mgr.execute_read_query(query, {"function_ids": function_ids})

    def _process_one_function_for_code_summary(self, node_data: dict) -> dict:
        """
        Wrapper function for the parallel executor. Calls the stateless processor
        and then performs the Neo4j update.
        """
        status, data = self.node_processor.get_function_code_summary(node_data)

        if status in ["code_summary_regenerated", "code_summary_restored"]:
            update_query = "MATCH (n:FUNCTION|METHOD {id: $id}) SET n.codeSummary = $code_summary, n.code_hash = $code_hash"
            self.neo4j_mgr.execute_autocommit_query(
                update_query, 
                {"id": node_data["id"], "code_summary": data["codeSummary"], "code_hash": data["code_hash"]}
            )
        
        return {
            "key": node_data["id"],
            "label": node_data["label"],
            "status": status,
            "data": data
        }

    def _summarize_functions_with_context_with_ids(self, function_ids: list[str]) -> set:
        if not function_ids:
            return set()

        # The worker function `_process_one_function_for_contextual_summary` will now handle
        # all logic including staleness checks. We just dispatch all potential candidates.
        logging.info(f"Processing contextual summaries for {len(function_ids)} candidate functions.")
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers
        logging.info(f"Using {max_workers} parallel workers for Pass 2.")

        updated_ids = self._parallel_process(
            items=function_ids, # Pass the list of IDs directly
            process_func=self._process_one_function_for_contextual_summary,
            max_workers=max_workers,
            desc="Pass 2: Context Summaries"
        )
        logging.info(f"Pass 2: Context Summaries - Updated {len(updated_ids)} nodes.")
        return updated_ids

    def _process_one_function_for_contextual_summary(self, func_id: str) -> dict:
        """
        Worker function that handles all logistics for processing a single function's
        contextual summary.
        """
        # 1. Preparation: Query DB for the node and its context
        context_query = """
        MATCH (n:FUNCTION|METHOD) WHERE n.id = $id
        OPTIONAL MATCH (caller:FUNCTION|METHOD)-[:CALLS]->(n)
        OPTIONAL MATCH (n)-[:CALLS]->(callee:FUNCTION|METHOD)
        RETURN n, labels(n) as n_labels,
               collect(DISTINCT {id: caller.id, label: labels(caller)[-1]}) AS callers,
               collect(DISTINCT {id: callee.id, label: labels(callee)[-1]}) AS callees
        """
        context_results = self.neo4j_mgr.execute_read_query(context_query, {"id": func_id})
        if not context_results or not context_results[0]['n']:
            logging.warning(f"Could not find function with ID {func_id} for contextual summary.")
            return None

        record = context_results[0]
        node_data = dict(record['n'])
        node_labels = record['n_labels']
        node_data['label'] = [l for l in node_labels if l in ['FUNCTION', 'METHOD']][0]
        
        # Clean up dependency lists in case of no neighbors
        caller_entities = [c for c in record.get('callers', []) if c and c['id']]
        callee_entities = [c for c in record.get('callees', []) if c and c['id']]

        # 2. Delegation: Pass to the processor which handles caching and generation
        status, data = self.node_processor.get_function_contextual_summary(
            node_data, caller_entities, callee_entities
        )

        # 3. Finalization: Update DB if changed
        if status in ["summary_regenerated", "summary_restored"]:
            update_query =f"MATCH (n:{node_data['label']} {{id: $id}}) SET n.summary = $summary REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(
                update_query,
                {"id": func_id, "summary": data["summary"]}
            )
        
        # 4. Return packet for the orchestrator's reduce step
        return {
            "key": func_id,
            "label": node_data["label"],
            "status": status,
            "data": data
        }

    def _get_classes_by_inheritance_level(self, target_class_ids: Optional[Set[str]] = None) -> Dict[int, List[Dict]]:
        classes_by_level = defaultdict(list)
        visited_ids = set()
        current_level = 0

        if target_class_ids:
            query_all_relevant_ids = """
                MATCH (c:CLASS_STRUCTURE)
                WHERE c.id IN $target_class_ids
                OPTIONAL MATCH (c)-[:INHERITS*0..]->(ancestor:CLASS_STRUCTURE)
                RETURN DISTINCT ancestor.id AS id
            """
            result = self.neo4j_mgr.execute_read_query(query_all_relevant_ids, {"target_class_ids": list(target_class_ids)})
            all_relevant_ids = {r['id'] for r in result}
            if not all_relevant_ids:
                return {}
        else:
            query_all_ids = "MATCH (c:CLASS_STRUCTURE) RETURN c.id AS id"
            result = self.neo4j_mgr.execute_read_query(query_all_ids)
            all_relevant_ids = {r['id'] for r in result}
            if not all_relevant_ids:
                return {}

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

    def _summarize_classes_with_ids(self, class_ids: Set[str]) -> Set[str]:
        """
        Orchestrates the map-reduce process for generating class summaries,
        respecting the inheritance hierarchy by processing level by level.
        """
        if not class_ids:
            logging.info("No class IDs provided for summarization.")
            return set()

        logging.info(f"Processing summaries for {len(class_ids)} candidate classes by inheritance level.")
        classes_by_level = self._get_classes_by_inheritance_level(class_ids)
        if not classes_by_level:
            logging.info("No class structures found to summarize.")
            return set()

        all_updated_ids = set()
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers

        for level in sorted(classes_by_level.keys()):
            level_class_ids = [c['id'] for c in classes_by_level[level]]
            if not level_class_ids:
                continue
            
            logging.info(f"Processing {len(level_class_ids)} classes at inheritance level {level}.")
            
            updated_ids_at_level = self._parallel_process(
                items=level_class_ids,
                process_func=self._process_one_class_summary,
                max_workers=max_workers,
                desc=f"Pass 3: Class Summaries (Lvl {level})"
            )
            logging.info(f"Pass 3 (Lvl {level}): Class Summaries - Updated {len(updated_ids_at_level)} nodes.")
            all_updated_ids.update(updated_ids_at_level)
            
        logging.info(f"Pass 3 (all levels): Class Summaries - Updated {len(all_updated_ids)} total nodes across all levels.")
        return all_updated_ids

    def _process_one_class_summary(self, class_id: str) -> dict:
        """
        Worker function that handles all logistics for processing a single class summary.
        """
        # 1. Preparation: Query DB for the node and its context
        context_query = """
        MATCH (c:CLASS_STRUCTURE {id: $id})
        OPTIONAL MATCH (c)-[:INHERITS]->(p:CLASS_STRUCTURE) WHERE p.summary IS NOT NULL
        OPTIONAL MATCH (c)-[:HAS_METHOD]->(m:METHOD) WHERE m.summary IS NOT NULL
        OPTIONAL MATCH (c)-[:HAS_FIELD]->(f:FIELD)
        RETURN c as node,
               collect(DISTINCT {id: p.id, label: 'CLASS_STRUCTURE'}) AS parents,
               collect(DISTINCT {id: m.id, label: 'METHOD'}) AS methods,
               collect(DISTINCT {name: f.name, type: f.type}) as fields
        """
        context_results = self.neo4j_mgr.execute_read_query(context_query, {"id": class_id})
        if not context_results or not context_results[0]['node']:
            logging.warning(f"Could not find class with ID {class_id} for summary.")
            return None

        record = context_results[0]
        node_data = dict(record['node'])
        node_data['label'] = 'CLASS_STRUCTURE'
        
        parent_entities = [p for p in record.get('parents', []) if p and p['id']]
        method_entities = [m for m in record.get('methods', []) if m and m['id']]
        field_entities = [f for f in record.get('fields', []) if f and f['name']]

        # 2. Delegation: Pass to the processor
        status, data = self.node_processor.get_class_summary(
            node_data, parent_entities, method_entities, field_entities
        )

        # 3. Finalization: Update DB if changed
        if status in ["summary_regenerated", "summary_restored"]:
            update_query = "MATCH (n:CLASS_STRUCTURE {id: $id}) SET n.summary = $summary REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(
                update_query,
                {"id": class_id, "summary": data["summary"]}
            )
        
        # 4. Return packet
        return {
            "key": class_id,
            "label": node_data["label"],
            "status": status,
            "data": data
        }

    def _get_namespaces_by_depth(self, namespace_ids: Set[str]) -> Dict[int, List[Dict]]:
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
      
    def _summarize_namespaces_with_ids(self, namespace_ids: Set[str]) -> Set[str]:
        """
        Orchestrates the map-reduce process for generating namespace summaries,
        respecting the nesting hierarchy by processing bottom-up.
        """
        if not namespace_ids:
            logging.info("No namespace IDs provided for summarization.")
            return set()

        logging.info(f"Processing summaries for {len(namespace_ids)} candidate namespaces by nesting depth.")
        namespaces_by_depth = self._get_namespaces_by_depth(namespace_ids)
        if not namespaces_by_depth:
            logging.info("No namespaces found to summarize.")
            return set()

        all_updated_ids = set()
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers

        for depth in sorted(namespaces_by_depth.keys(), reverse=True):
            level_namespace_ids = [ns['id'] for ns in namespaces_by_depth[depth]]
            if not level_namespace_ids:
                continue
            
            logging.info(f"Processing {len(level_namespace_ids)} namespaces at depth {depth}.")
            
            updated_ids_at_level = self._parallel_process(
                items=level_namespace_ids,
                process_func=self._process_one_namespace_summary,
                max_workers=max_workers,
                desc=f"Pass 4: Namespace Summaries (Depth {depth})"
            )
            logging.info(f"Pass 4 (Depth {depth}): Namespace Summaries - Updated {len(updated_ids_at_level)} nodes.")
            all_updated_ids.update(updated_ids_at_level)
            
        logging.info(f"Pass 4: Namespace Summaries - Updated {len(all_updated_ids)} total nodes across all depths.")
        return all_updated_ids

    def _process_one_namespace_summary(self, namespace_id: str) -> dict:
        """
        Worker function that handles all logistics for processing a single namespace summary.
        """
        # 1. Preparation: Query DB for the node and its children
        context_query = """
        MATCH (ns:NAMESPACE {id: $id})
        OPTIONAL MATCH (ns)-[:SCOPE_CONTAINS]->(child)
        WHERE child.summary IS NOT NULL
        RETURN ns as node,
               collect(DISTINCT {id: child.id, label: labels(child)[-1], name: child.name}) as children
        """
        context_results = self.neo4j_mgr.execute_read_query(context_query, {"id": namespace_id})
        if not context_results or not context_results[0]['node']:
            logging.warning(f"Could not find namespace with ID {namespace_id} for summary.")
            return None

        record = context_results[0]
        node_data = dict(record['node'])
        node_data['label'] = 'NAMESPACE'
        
        child_entities = [c for c in record.get('children', []) if c and c['id']]

        # 2. Delegation: Pass to the processor
        status, data = self.node_processor.get_namespace_summary(
            node_data, child_entities
        )

        # 3. Finalization: Update DB if changed
        if status in ["summary_regenerated", "summary_restored"]:
            update_query = "MATCH (n:NAMESPACE {id: $id}) SET n.summary = $summary REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(
                update_query,
                {"id": namespace_id, "summary": data["summary"]}
            )
        
        # 4. Return packet
        return {
            "key": namespace_id,
            "label": node_data["label"],
            "status": status,
            "data": data
        }

    def _summarize_files_with_paths(self, file_paths: list[str]) -> set:
        if not file_paths:
            return set()
        
        logging.info(f"Processing summaries for {len(file_paths)} candidate files.")
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers

        # Prepare items for the generic worker
        items_to_process = [
            (file_path, 'FILE', 
             "MATCH (parent:FILE {path: $key})-[:DEFINES]->(child) WHERE child.summary IS NOT NULL RETURN collect(DISTINCT {id: child.id, label: labels(child)[-1], name: child.name}) as children",
             self.node_processor.get_file_summary)
            for file_path in file_paths
        ]

        updated_ids = self._parallel_process(
            items=items_to_process,
            process_func=self._process_one_hierarchical_node,
            max_workers=max_workers,
            desc="Pass 5: File Summaries"
        )
        logging.info(f"Pass 5: File Summaries - Updated {len(updated_ids)} nodes.")
        return updated_ids

    def _summarize_folders_with_paths(self, folder_paths: list[str]) -> set:
        if not folder_paths:
            return set()

        # Sort by depth to process bottom-up
        paths_by_depth = defaultdict(list)
        for folder_path in folder_paths:
            paths_by_depth[folder_path.count(os.sep)].append(folder_path)

        all_updated_ids = set()
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers

        for depth in sorted(paths_by_depth.keys(), reverse=True):
            level_paths = paths_by_depth[depth]
            logging.info(f"Processing {len(level_paths)} folders at depth {depth}.")

            items_to_process = [
                (path, 'FOLDER',
                 "MATCH (parent:FOLDER {path: $key})-[:CONTAINS]->(child) WHERE child.summary IS NOT NULL RETURN collect(DISTINCT {id: child.id, path: child.path, label: labels(child)[-1], name: child.name}) as children",
                 self.node_processor.get_folder_summary)
                for path in level_paths
            ]

            updated_ids_at_level = self._parallel_process(
                items=items_to_process,
                process_func=self._process_one_hierarchical_node,
                max_workers=max_workers,
                desc=f"Pass 6: Folder Summaries (Depth {depth})"
            )
            logging.info(f"Pass 6 (Depth {depth}): Folder Summaries - Updated {len(updated_ids_at_level)} nodes.")
            all_updated_ids.update(updated_ids_at_level)
            
        logging.info(f"Pass 6 (all Depths): Folder Summaries - Updated {len(all_updated_ids)} total nodes across all depths.")
        return all_updated_ids

    def _summarize_project(self) -> set:
        # The project path is the identifier for the PROJECT node
        project_path = self.project_path
        
        logging.info("Processing summary for PROJECT node.")
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers

        items_to_process = [
            (project_path, 'PROJECT',
             "MATCH (parent:PROJECT {path: $key})-[:CONTAINS]->(child) WHERE child.summary IS NOT NULL RETURN collect(DISTINCT {id: child.id, path: child.path, label: labels(child)[-1], name: child.name}) as children",
             self.node_processor.get_project_summary)
        ]

        return self._parallel_process(
            items=items_to_process,
            process_func=self._process_one_hierarchical_node,
            max_workers=max_workers, # Only 1 item, but keep for consistency
            desc="Pass 7: Project Summary"
        )

    def _process_one_hierarchical_node(self, args) -> dict:
        key, label, dependency_query, processor_func = args

        # 1. Preparation
        node_query = f"MATCH (n:{label} {{path: $key}}) RETURN n, labels(n) as n_labels"
        node_results = self.neo4j_mgr.execute_read_query(node_query, {"key": key})
        if not node_results or not node_results[0]['n']:
            logging.warning(f"Could not find node {label} with path {key} for summary.")
            return None

        node_data = dict(node_results[0]['n'])
        node_data['label'] = label

        deps_result = self.neo4j_mgr.execute_read_query(dependency_query, {"key": key})
        child_entities = deps_result[0]['children'] if deps_result and deps_result[0]['children'] else []

        # 2. Delegation
        status, data = processor_func(node_data, child_entities)

        # 3. Finalization
        if status in ["summary_regenerated", "summary_restored"]:
            update_query = f"MATCH (n:{label} {{path: $key}}) SET n.summary = $summary REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(
                update_query,
                {"key": key, "summary": data["summary"]}
            )
        
        # 4. Return packet
        return {
            "key": key,
            "label": label,
            "status": status,
            "data": data
        }

    

    
