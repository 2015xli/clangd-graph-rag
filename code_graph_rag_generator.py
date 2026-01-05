#!/usr/bin/env python3
"""
This script generates summaries and embeddings for nodes in a code graph.

It connects to an existing Neo4j database populated by the ingestion pipeline
and executes a multi-pass process to enrich the graph with AI-generated
summaries and vector embeddings, as outlined in docs/code_rag_generation_plan.md.
"""

import argparse
import logging
import re

import input_params
from rag_orchestrator import RagOrchestrator
from neo4j_manager import Neo4jManager

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

_SPECIAL_PATTERN = re.compile(r"<\|[^\|]+?\|> ")

def sanitize_special_tokens(text: str) -> str:
    """Break up special tokens so the model won't treat them as control tokens."""
    return _SPECIAL_PATTERN.sub(lambda m: f"< |{m.group(0)[2:-2]}| >", text)

class RagGenerator(RagOrchestrator):
    """Orchestrates the full-build generation of RAG data."""

    def summarize_code_graph(self):
        """Main orchestrator method to run all summarization passes for a full build."""
        self.summary_cache_manager.load()
        
        self.analyze_functions_individually()
        self.summary_cache_manager.save(mode="builder", neo4j_mgr=self.neo4j_mgr, is_intermediate=True)

        self.summarize_functions_with_context()
        self.summary_cache_manager.save(mode="builder", neo4j_mgr=self.neo4j_mgr, is_intermediate=True)

        self.summarize_class_structures()
        self.summary_cache_manager.save(mode="builder", neo4j_mgr=self.neo4j_mgr, is_intermediate=True)

        self.summarize_namespaces()
        self.summary_cache_manager.save(mode="builder", neo4j_mgr=self.neo4j_mgr, is_intermediate=True)

        logging.info("--- Starting File and Folder Summarization ---")
        self._summarize_all_files()
        self._summarize_all_folders()
        self._summarize_project()
        logging.info("--- Finished File and Folder Summarization ---")
        # Final save before embeddings
        self.summary_cache_manager.save(mode="builder", neo4j_mgr=self.neo4j_mgr)

        logging.info(f"Total number of summaries processed: {self.n_restored + self.n_generated + self.n_unchanged + self.n_nochildren + self.n_failed}")
        logging.info(f"  Restored: {self.n_restored}, Generated: {self.n_generated}, Unchanged: {self.n_unchanged}, No children: {self.n_nochildren}, Failed: {self.n_failed}")

        self.generate_embeddings()


    # --- Pass 1 Methods ---
    def analyze_functions_individually(self):
        """PASS 1: Generates a code-only analysis for all functions and methods in the graph."""
        logging.info("\n--- Starting Pass 1: Analyzing Functions & Methods Individually ---")
        
        query = "MATCH (n) WHERE (n:FUNCTION OR n:METHOD) AND n.body_location IS NOT NULL RETURN n.id AS id"
        results = self.neo4j_mgr.execute_read_query(query)
        all_function_ids = [r['id'] for r in results]

        if not all_function_ids:
            logging.warning("No functions or methods with body_location found. Exiting Pass 1.")
            return
        
        self._analyze_functions_individually_with_ids(all_function_ids)
        logging.info("--- Finished Pass 1 ---")

    # --- Pass 2 Methods ---
    def summarize_functions_with_context(self):
        """PASS 2: Generates a final, context-aware summary for all functions and methods."""
        logging.info("--- Starting Pass 2: Summarizing Functions & Methods With Context ---")
        
        query = """
        MATCH (n:FUNCTION|METHOD)
        WHERE n.code_analysis IS NOT NULL
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

    # --- Pass 3 Methods: Class Summaries ---
    def summarize_class_structures(self):
        """
        PASS 3: Generates summaries for all class structures.
        """
        logging.info("\n--- Starting Pass 3: Summarizing Class Structures ---")
        
        query = "MATCH (c:CLASS_STRUCTURE) RETURN c.id AS id"
        results = self.neo4j_mgr.execute_read_query(query)
        all_class_ids = {r['id'] for r in results}

        if not all_class_ids:
            logging.info("No class structures found to summarize.")
            return

        self._summarize_classes_with_ids(all_class_ids)
        logging.info("--- Finished Pass 3 ---")

    # --- Pass 4 Methods: Namespace Summaries ---
    def summarize_namespaces(self):
        """PASS 4: Generates a summary for all namespace nodes in the graph."""
        logging.info("\n--- Starting Pass 4: Summarizing Namespaces ---")

        query = "MATCH (n:NAMESPACE) RETURN n.id as id"
        results = self.neo4j_mgr.execute_read_query(query)
        all_namespace_ids = {r['id'] for r in results}

        if not all_namespace_ids:
            logging.info("No namespaces require summarization.")
            return

        self._summarize_namespaces_with_ids(all_namespace_ids)
        logging.info("--- Finished Pass 4 ---")

    

    # --- Pass 5 Methods ---
    def _summarize_all_files(self):
        logging.info("\n--- Starting Pass 5: Summarizing All Files ---")
        query = "MATCH (f:FILE) RETURN f.path AS path"
        files_to_process = self.neo4j_mgr.execute_read_query(query)
        if not files_to_process:
            logging.info("No files found to summarize.")
            return
        file_paths = [r['path'] for r in files_to_process]
        self._summarize_files_with_paths(file_paths)

    # --- Pass 6 Methods ---
    def _summarize_all_folders(self):
        logging.info("\n--- Starting Pass 6: Summarizing All Folders (bottom-up) ---")
        query = "MATCH (f:FOLDER) RETURN f.path as path"
        folders_to_process = self.neo4j_mgr.execute_read_query(query)
        if not folders_to_process:
            logging.info("No folders found to summarize.")
            return
        folder_paths = [r['path'] for r in folders_to_process]
        self._summarize_folders_with_paths(folder_paths)

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    parser = argparse.ArgumentParser(description='Generate summaries and embeddings for a code graph.')
    
    input_params.add_core_input_args(parser)
    input_params.add_rag_args(parser)
    input_params.add_worker_args(parser)

    args = parser.parse_args()
    args.project_path = str(args.project_path.resolve())

    try:        
        with Neo4jManager() as neo4j_mgr:
            if not neo4j_mgr.check_connection(): return 1
            if not neo4j_mgr.verify_project_path(args.project_path): return 1
            
            generator = RagGenerator(
                neo4j_mgr=neo4j_mgr, 
                project_path=args.project_path, 
                args=args
            )
            
            generator.summarize_code_graph()

    except Exception as e:
        logging.critical(f"A critical error occurred: {e}", exc_info=True)
        return 1

if __name__ == "__main__":
    main()
