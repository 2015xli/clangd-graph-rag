#!/usr/bin/env python3
"""
Mixin for function and method summarization.
"""

import logging
from typing import Set, List, Dict

# Set up logging for this module
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class FunctionProcessorMixin:
    """
    Encapsulates logic for generating summaries for granular symbols
    like FUNCTIONS and METHODS.
    """

    def analyze_functions_individually_with_ids(self, function_ids: list[str]) -> set:
        """
        Orchestrates the map-reduce process for generating code analyses.
        """
        if not function_ids:
            return set()
            
        updated_ids = set()
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers

        impls_to_process = self._get_functions_for_code_analysis(function_ids)
        if impls_to_process:
            logger.info(f"Found {len(impls_to_process)} functions/methods with bodies.")
            logger.info(f"Using {max_workers} parallel workers for Pass 1.a.")
            updated_ids = self._parallel_process(
                items=impls_to_process,
                process_func=self._process_one_function_for_code_analysis,
                max_workers=max_workers,
                desc="Pass 1a: Code analyses (with bodies)"
            )

        interfaces_to_process = self._get_functions_without_bodies(function_ids)
        if interfaces_to_process:
            logger.info(f"Found {len(interfaces_to_process)} functions/methods without bodies.")
            logger.info(f"Using {max_workers} parallel workers for Pass 1.b.")
            updated_intf_ids = self._parallel_process(
                items=interfaces_to_process,
                process_func=self._process_one_interface_for_analysis,
                max_workers=max_workers,
                desc="Pass 1b: Interface analyses (no bodies)"
            )
            updated_ids.update(updated_intf_ids)

        logger.info(f"Pass 1: Combined analyses - Updated {len(updated_ids)} total nodes.")
        return updated_ids

    def _get_functions_for_code_analysis(self, function_ids: list[str]) -> list[dict]:
        query = """
        MATCH (n:FUNCTION|METHOD)
        WHERE n.id IN $function_ids AND n.body_location IS NOT NULL
        RETURN n.id AS id, n.name AS name, n.kind AS kind, n.path AS path, n.body_location as body_location,
               n.code_hash as db_code_hash, n.code_analysis as db_code_analysis, labels(n) as labels
        """
        return self.neo4j_mgr.execute_read_query(query, {"function_ids": function_ids})

    def _get_functions_without_bodies(self, function_ids: list[str]) -> list[dict]:
        query = """
        MATCH (n:FUNCTION|METHOD)
        WHERE n.id IN $function_ids AND n.body_location IS NULL
        RETURN n.id AS id, n.name AS name, n.kind AS kind, n.signature AS signature, 
               n.return_type AS return_type, n.code_hash as db_code_hash, 
               n.code_analysis as db_code_analysis, labels(n) as labels
        """
        return self.neo4j_mgr.execute_read_query(query, {"function_ids": function_ids})

    def _process_one_function_for_code_analysis(self, node_data: dict) -> dict:
        """
        Wrapper function for the parallel executor. Calls the stateless processor
        and then performs the Neo4j update.
        """
        node_data['label'] = [l for l in node_data['labels'] if l in ['FUNCTION', 'METHOD']][0]
        status, data = self.node_processor.get_function_code_analysis(node_data)

        if status in ["code_analysis_regenerated", "code_analysis_restored"]:
            update_query = "MATCH (n:FUNCTION|METHOD {id: $id}) SET n.code_analysis = $code_analysis, n.code_hash = $code_hash"
            self.neo4j_mgr.execute_autocommit_query(
                update_query, 
                {"id": node_data["id"], "code_analysis": data["code_analysis"], "code_hash": data["code_hash"]}
            )
        
        return {
            "key": node_data["id"],
            "label": node_data["label"],
            "status": status,
            "data": data
        }

    def _process_one_interface_for_analysis(self, node_data: dict) -> dict:
        """Worker for a single interface analysis."""
        node_data['label'] = [l for l in node_data['labels'] if l in ['FUNCTION', 'METHOD']][0]
        
        status, data = self.node_processor.get_interface_analysis(node_data)

        if status in ["code_analysis_regenerated", "code_analysis_restored"]:
            update_query = "MATCH (n:FUNCTION|METHOD {id: $id}) SET n.code_analysis = $code_analysis, n.code_hash = $code_hash"
            self.neo4j_mgr.execute_autocommit_query(
                update_query,
                {"id": node_data["id"], "code_analysis": data["code_analysis"], "code_hash": data["code_hash"]}
            )

        return {
            "key": node_data["id"],
            "label": node_data["label"],
            "status": status,
            "data": data
        }

    def summarize_functions_with_context_with_ids(self, function_ids: list[str]) -> set:
        """Processes contextual summaries for candidate functions."""
        if not function_ids:
            return set()

        logger.info(f"Processing contextual summaries for {len(function_ids)} candidate functions.")
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers
        logger.info(f"Using {max_workers} parallel workers for Pass 2.")

        updated_ids = self._parallel_process(
            items=function_ids,
            process_func=self._process_one_function_for_contextual_summary,
            max_workers=max_workers,
            desc="Pass 2: Context Summaries"
        )
        logger.info(f"Pass 2: Context Summaries - Updated {len(updated_ids)} nodes.")
        return updated_ids

    def _process_one_function_for_contextual_summary(self, func_id: str) -> dict:
        """
        Worker function that handles all logistics for processing a single function's
        contextual summary.
        """
        context_query = """
        MATCH (n:FUNCTION|METHOD) WHERE n.id = $id
        OPTIONAL MATCH (caller:FUNCTION|METHOD)-[:CALLS]->(n)
        OPTIONAL MATCH (n)-[:CALLS]->(callee:FUNCTION|METHOD)
        RETURN n, labels(n) as n_labels,
               collect(DISTINCT {id: caller.id, labels: labels(caller)}) AS callers,
               collect(DISTINCT {id: callee.id, labels: labels(callee)}) AS callees
        """
        context_results = self.neo4j_mgr.execute_read_query(context_query, {"id": func_id})
        if not context_results or not context_results[0]['n']:
            logger.warning(f"Could not find function with ID {func_id} for contextual summary.")
            return None

        record = context_results[0]
        node_data = dict(record['n'])
        node_labels = record['n_labels']
        node_data['label'] = [l for l in node_labels if l in ['FUNCTION', 'METHOD']][0]
        
        caller_entities = [c for c in record.get('callers', []) if c and c['id']]
        callee_entities = [c for c in record.get('callees', []) if c and c['id']]

        for caller in caller_entities:
            caller['label'] = [l for l in caller['labels'] if l in ['FUNCTION', 'METHOD']][0]
        for callee in callee_entities:
            callee['label'] = [l for l in callee['labels'] if l in ['FUNCTION', 'METHOD']][0]

        status, data = self.node_processor.get_function_contextual_summary(
            node_data, caller_entities, callee_entities
        )

        if status in ["summary_regenerated", "summary_restored"]:
            update_query =f"MATCH (n:{node_data['label']} {{id: $id}}) SET n.summary = $summary REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(
                update_query,
                {"id": func_id, "summary": data["summary"]}
            )
        
        return {
            "key": func_id,
            "label": node_data["label"],
            "status": status,
            "data": data
        }
