#!/usr/bin/env python3
"""
This script generates summaries and embeddings for nodes in a code graph.

It connects to an existing Neo4j database populated by the ingestion pipeline
and executes a multi-pass process to enrich the graph with AI-generated
summaries and vector embeddings.
"""

import argparse
import logging
import re

import input_params
from summary_engine import SummarizationEngine
from neo4j_manager import Neo4jManager

# Set up logging for this module
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class FullSummarizer:
    """Orchestrates the full-build generation of RAG data using the SummarizationEngine."""

    def __init__(self, neo4j_mgr: Neo4jManager, project_path: str, args: argparse.Namespace):
        self.neo4j_mgr = neo4j_mgr
        self.project_path = project_path
        self.args = args 
        
        # Initialize the Summarization Engine via composition
        self.engine = SummarizationEngine(
            neo4j_mgr=self.neo4j_mgr,
            project_path=self.project_path,
            args=self.args
        )

    def summarize_code_graph(self):
        """Main orchestrator method to run all summarization passes for a full build."""
        # 0. Initialize the run (loads cache and cleans up faked content if needed)
        self.engine.initialize_run()
        
        # 1. Individual function analysis (Pass 1)
        self.analyze_functions_individually()
        self.engine.summary_cache_manager.save(mode="builder", neo4j_mgr=self.neo4j_mgr, is_intermediate=True)

        # 2. Context-aware function summarization (Pass 2)
        self.summarize_functions_with_context()
        self.engine.summary_cache_manager.save(mode="builder", neo4j_mgr=self.neo4j_mgr, is_intermediate=True)

        # 3. Class structure summarization (Pass 3)
        self.summarize_class_structures()
        self.engine.summary_cache_manager.save(mode="builder", neo4j_mgr=self.neo4j_mgr, is_intermediate=True)

        # 4. Namespace summarization (Pass 4)
        self.summarize_namespaces()
        self.engine.summary_cache_manager.save(mode="builder", neo4j_mgr=self.neo4j_mgr, is_intermediate=True)

        # 5-7. Hierarchical roll-ups (Files, Folders, Project)
        logging.info("--- Starting File and Folder Summarization ---")
        self._summarize_all_files()
        self._summarize_all_folders()
        self.engine.summarize_project()
        logging.info("--- Finished File and Folder Summarization ---")
        
        # Final save of the summary cache
        self.engine.summary_cache_manager.save(mode="builder", neo4j_mgr=self.neo4j_mgr)

        total_processed = (self.engine.n_restored + self.engine.n_generated + 
                           self.engine.n_unchanged + self.engine.n_nochildren + 
                           self.engine.n_failed)
        
        logging.info(f"Total number of summaries processed: {total_processed}")
        logging.info(f"  Restored: {self.engine.n_restored}, Generated: {self.engine.n_generated}, "
                     f"Unchanged: {self.engine.n_unchanged}, No children: {self.engine.n_nochildren}, "
                     f"Failed: {self.engine.n_failed}")

        # 8. Embedding generation
        self.engine.generate_embeddings()


    # --- Pass 1: Individual Code Analysis ---
    def analyze_functions_individually(self):
        """Generates a code-only analysis for all functions and methods in the graph."""
        logging.info("\n--- Starting Pass 1: Analyzing Functions & Methods Individually ---")
        
        query = "MATCH (n) WHERE (n:FUNCTION OR n:METHOD) AND n.body_location IS NOT NULL RETURN n.id AS id"
        results = self.neo4j_mgr.execute_read_query(query)
        all_function_ids = [r['id'] for r in results]

        if not all_function_ids:
            logging.warning("No functions or methods with body_location found. Exiting Pass 1.")
            return
        
        self.engine.analyze_functions_individually_with_ids(all_function_ids)
        logging.info("--- Finished Pass 1 ---")

    # --- Pass 2: Contextual Function Summarization ---
    def summarize_functions_with_context(self):
        """Generates a final, context-aware summary for all functions and methods."""
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
        
        updated_ids = self.engine.summarize_functions_with_context_with_ids(all_function_ids)
        
        logging.info(f"Pass 2 complete. Updated contextual summaries for {len(updated_ids)} functions.")
        logging.info("--- Finished Pass 2 ---")

    # --- Pass 3: Class Summaries ---
    def summarize_class_structures(self):
        """Generates summaries for all class structures."""
        logging.info("\n--- Starting Pass 3: Summarizing Class Structures ---")
        
        query = "MATCH (c:CLASS_STRUCTURE) RETURN c.id AS id"
        results = self.neo4j_mgr.execute_read_query(query)
        all_class_ids = {r['id'] for r in results}

        if not all_class_ids:
            logging.info("No class structures found to summarize.")
            return

        self.engine.summarize_classes_with_ids(all_class_ids)
        logging.info("--- Finished Pass 3 ---")

    # --- Pass 4: Namespace Summaries ---
    def summarize_namespaces(self):
        """Generates a summary for all namespace nodes in the graph."""
        logging.info("\n--- Starting Pass 4: Summarizing Namespaces ---")

        query = "MATCH (n:NAMESPACE) RETURN n.id as id"
        results = self.neo4j_mgr.execute_read_query(query)
        all_namespace_ids = {r['id'] for r in results}

        if not all_namespace_ids:
            logging.info("No namespaces require summarization.")
            return

        self.engine.summarize_namespaces_with_ids(all_namespace_ids)
        logging.info("--- Finished Pass 4 ---")

    # --- Pass 5: File Summaries ---
    def _summarize_all_files(self):
        logging.info("\n--- Starting Pass 5: Summarizing All Files ---")
        query = "MATCH (f:FILE) RETURN f.path AS path"
        files_to_process = self.neo4j_mgr.execute_read_query(query)
        if not files_to_process:
            logging.info("No files found to summarize.")
            return
        file_paths = [r['path'] for r in files_to_process]
        self.engine.summarize_files_with_paths(file_paths)

    # --- Pass 6: Folder Summaries ---
    def _summarize_all_folders(self):
        logging.info("\n--- Starting Pass 6: Summarizing All Folders (bottom-up) ---")
        query = "MATCH (f:FOLDER) RETURN f.path as path"
        folders_to_process = self.neo4j_mgr.execute_read_query(query)
        if not folders_to_process:
            logging.info("No folders found to summarize.")
            return
        folder_paths = [r['path'] for r in folders_to_process]
        self.engine.summarize_folders_with_paths(folder_paths)

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    parser = argparse.ArgumentParser(description='Generate summaries and embeddings for a code graph.')
    
    input_params.add_core_input_args(parser)
    input_params.add_rag_args(parser)
    input_params.add_worker_args(parser)
    input_params.add_llm_cache_args(parser)

    args = parser.parse_args()
    args.project_path = str(args.project_path.resolve())

    try:        
        with Neo4jManager() as neo4j_mgr:
            if not neo4j_mgr.check_connection(): return 1
            if not neo4j_mgr.verify_project_path(args.project_path): return 1
            
            generator = FullSummarizer(
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
