#!/usr/bin/env python3
"""
Summarization Engine for generating RAG data, including summaries and embeddings.
This class is designed to be used via composition by workflow drivers.
Modularized using specialized mixins for different node categories.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, Callable, List, Optional, Set, Dict
from tqdm import tqdm

from neo4j_manager import Neo4jManager
from utils import align_string
from llm_client import setup_llm_client, get_embedding_client
from .prompts import PromptManager
from .node_cache import SummaryCacheManager
from .node_summarizer import NodeSummarizer

# Import logical mixins
from .function_processor import FunctionProcessorMixin
from .scope_processor import ScopeProcessorMixin
from .hierarchy_processor import HierarchyProcessorMixin

# Set up logging for this module
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class SummaryEngine(
    FunctionProcessorMixin,
    ScopeProcessorMixin,
    HierarchyProcessorMixin
):
    """
    Unified engine for code graph summarization.
    Contains core mechanics and inherits specialized logic from mixins.
    """

    def __init__(self, neo4j_mgr: Neo4jManager, project_path: str, args):
        self.neo4j_mgr = neo4j_mgr
        self.project_path = os.path.abspath(project_path)
        self.args = args
        
        # Statistics for the current run
        self.n_restored = 0
        self.n_generated = 0
        self.n_unchanged = 0
        self.n_nochildren = 0
        self.n_failed = 0

        self.num_local_workers = args.num_local_workers
        self.num_remote_workers = args.num_remote_workers

        # L1 cache layer (Node-level summaries and analyses)
        self.summary_cache_manager = SummaryCacheManager(project_path)

        # Initialize LLM Client via centralized helper
        llm_client = setup_llm_client(args, project_path)
        self.is_local_llm = llm_client.is_local

        # Initialize Prompt Manager and Node Processor
        prompt_manager = PromptManager()
        
        # Determine the maximum context size based on model or user input
        max_context_token_size = getattr(args, 'max_context_size', None)
        if not max_context_token_size:
            max_context_token_size = llm_client.get_context_window_size()
            logger.info(f"Using model's default context window size: {max_context_token_size} tokens.")
        else:
            logger.info(f"Using user-specified context window size: {max_context_token_size} tokens.")

        self.node_processor = NodeSummarizer(
            project_path=project_path,
            cache_manager=self.summary_cache_manager,
            llm_client=llm_client,
            prompt_manager=prompt_manager,
            token_encoding=getattr(args, 'token_encoding', 'cl100k_base'),
            max_context_token_size=max_context_token_size
        )

        # Initialize Embedding Client
        self.embedding_client = get_embedding_client(args.llm_api)

    def initialize_run(self):
        """
        Prepares the engine for a summarization run.
        - Loads the L1 cache.
        - Resolves project context (Name and Background).
        - Automatically detects and purges fake summaries if using a real LLM API.
        """
        # 1. Load the L1 node cache
        self.summary_cache_manager.load()

        # 2. Resolve Project Context
        self._resolve_project_context()

        # 3. Automatic Fake Cleanup Logic
        # If we are using a REAL LLM API (not 'fake'), we check for and purge faked content.
        if self.args.llm_api == 'fake':
            return
            
        logger.info(f"Using real LLM API ('{self.args.llm_api}'). Checking for existing fake summaries...")
        
        from llm_client import FAKE_SUMMARY_CONTENT
        
        # --- Check L1 Cache ---
        has_fake_in_cache = False
        for label, entries in self.summary_cache_manager.cache.items():
            for data in entries.values():
                if data.get('summary') == FAKE_SUMMARY_CONTENT or data.get('code_analysis') == FAKE_SUMMARY_CONTENT:
                    has_fake_in_cache = True
                    break
            if has_fake_in_cache: break
        
        # --- Check Neo4j (Fast scan) ---
        has_fake_in_db = False
        check_query = """
        MATCH (n) 
        WHERE n.summary = $fake_content OR n.code_analysis = $fake_content
        RETURN n.id LIMIT 1
        """
        results = self.neo4j_mgr.execute_read_query(check_query, {"fake_content": FAKE_SUMMARY_CONTENT})
        has_fake_in_db = len(results) > 0

        # --- Trigger Surgical Cleanup if needed ---
        if not has_fake_in_cache and not has_fake_in_db:
            logger.info("No fake summaries detected. Proceeding with run.")
            return

        # A. Clean Neo4j
        if has_fake_in_db:
            logger.warning("Detected faked content in database. Initiating automatic surgical cleanup...")            
            removed_count = self.neo4j_mgr.delete_property(label=None, property_key="fake_summary", all_labels=True) 
            assert removed_count > 0, "Found fake summaries but none were removed from database during cleanup."
            logger.info(f"Automatic cleanup of {removed_count} fake summaries complete from database.")

        # B. Clean L1 Cache
        if has_fake_in_cache:
            logger.warning("Detected faked content in L1 cache. Initiating automatic surgical cleanup...")
            removed_count = self.summary_cache_manager.clean_fake_summaries()
            assert removed_count > 0, "Found fake summaries but none were removed from L1 cache during cleanup."
            self.summary_cache_manager._write_cache_to_file()
            logger.info(f"Automatic cleanup of {removed_count} fake summaries complete from L1 cache.")

    def _resolve_project_context(self):
        """Resolves the project name and background info."""
        # 1. Project Name
        query = "MATCH (p:PROJECT) RETURN p.name AS name"
        results = self.neo4j_mgr.execute_read_query(query)
        project_name = results[0]['name'] if results else "Unknown Project"
        
        # 2. Project Background (Tiered resolution)
        project_info = "(N/A)"
        
        # Tier 1: Machine-generated summary from previous run
        cache_dir = os.path.join(self.project_path, ".cache")
        machine_summary_path = os.path.join(cache_dir, "project-summary.md")
        user_info_path = os.path.join(self.project_path, "project-info.md")
        
        if os.path.exists(machine_summary_path):
            try:
                with open(machine_summary_path, 'r') as f:
                    project_info = f.read().strip()
                logger.info("Loaded machine-generated project summary as context.")
            except Exception as e:
                logger.warning(f"Failed to read machine summary at {machine_summary_path}: {e}")

        # Tier 2: User-provided manual info
        if project_info == "(N/A)" and os.path.exists(user_info_path):
            try:
                with open(user_info_path, 'r') as f:
                    project_info = f.read().strip()
                logger.info("Loaded user-provided project info as context.")
            except Exception as e:
                logger.warning(f"Failed to read user info at {user_info_path}: {e}")

        # Fallback to (N/A) is already the default
        
        # Update node_processor
        self.node_processor.project_name = project_name
        self.node_processor.project_info = project_info
        logger.info(f"Project Context Resolved - Name: {project_name}, Info: {project_info[:100]}...")

    def finalize_run(self):
        """
        Finalizes the summarization run.
        - Persists the project summary to .cache/project-summary.md if using a real LLM.
        """
        if self.args.llm_api == 'fake':
            logger.debug("Skipping project summary persistence for fake LLM API.")
            return

        logger.info("Finalizing run: checking for project summary persistence...")
        query = "MATCH (p:PROJECT) RETURN p.summary AS summary"
        results = self.neo4j_mgr.execute_read_query(query)
        
        if results and results[0].get('summary'):
            project_summary = results[0]['summary']
            cache_dir = os.path.join(self.project_path, ".cache")
            os.makedirs(cache_dir, exist_ok=True)
            machine_summary_path = os.path.join(cache_dir, "project-summary.md")
            
            try:
                with open(machine_summary_path, 'w') as f:
                    f.write(project_summary)
                logger.info(f"Project summary persisted to {machine_summary_path}")
            except Exception as e:
                logger.error(f"Failed to persist project summary: {e}")
        else:
            logger.warning("No project summary found in database to persist.")

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
        n_restored = 0
        n_generated = 0
        n_unchanged = 0
        n_nochildren = 0
        n_failed = 0
        
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
                        if data and (data.get('summary') or data.get('code_analysis') or data.get('group_analysis')):
                            self.summary_cache_manager.update_cache_entry(label, key, data)
                        
                        self.summary_cache_manager.set_runtime_status(label, key, "visited")

                        # Add to updated_keys if the DB was successfully touched
                        if status in ["summary_regenerated", "summary_restored", "code_analysis_regenerated", "code_analysis_restored"]:
                            updated_keys.add(key)

                            if status in ["summary_regenerated", "code_analysis_regenerated"]:
                                n_generated += 1
                            elif status in ["summary_restored", "code_analysis_restored"]:
                                n_restored += 1
                        elif status == "no_children":
                            n_nochildren += 1
                        elif status == "unchanged":
                            n_unchanged += 1
                        else: # "generation_failed"
                            n_failed += 1

                        # Conditionally set flags for dependency tracking
                        if status == "code_analysis_regenerated":
                            self.summary_cache_manager.set_runtime_status(label, key, "code_analysis_changed")
                        elif status == "summary_regenerated":
                            self.summary_cache_manager.set_runtime_status(label, key, "summary_changed")

                    except Exception as e:
                        item = futures[future]
                        logger.error(f"Error processing item {item}: {e}", exc_info=True)
                    finally:
                        pbar.update(1)

        self.n_restored += n_restored
        self.n_generated += n_generated
        self.n_unchanged += n_unchanged
        self.n_nochildren += n_nochildren
        self.n_failed += n_failed
        logger.info(f"Restored: {n_restored}, Generated: {n_generated}, Unchanged: {n_unchanged}, No children: {n_nochildren}, Failed: {n_failed}")
        return updated_keys

    def generate_embeddings(self):
        """Generates and updates embeddings for all nodes with summaries."""
        logger.info("\n--- Starting Generating Embeddings ---")
        nodes_to_embed = self._get_nodes_for_embedding()
        if not nodes_to_embed:
            logger.info("No nodes require embedding.")
            return

        logger.info(f"Found {len(nodes_to_embed)} nodes with summaries to embed.")

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
            logger.warning("Embedding generation resulted in no data to update.")
            return

        ingest_batch_size = 1000
        logger.info(f"Updating {len(update_params)} nodes in the database in batches of {ingest_batch_size}...")
        
        update_query = """
        UNWIND $batch AS data
        MATCH (n) WHERE elementId(n) = data.elementId
        SET n.summaryEmbedding = data.embedding
        """
        
        for i in tqdm(range(0, len(update_params), ingest_batch_size), desc=align_string("Updating DB")):
            batch = update_params[i:i + ingest_batch_size]
            self.neo4j_mgr.execute_autocommit_query(update_query, params={'batch': batch})

        logger.info("--- Finished Generating Embeddings ---")

    def _get_nodes_for_embedding(self) -> list[dict]:
        query = """
        MATCH (n)
        WHERE (n:FUNCTION OR n:METHOD OR n:CLASS_STRUCTURE OR n:NAMESPACE OR n:FILE OR n:FOLDER OR n:PROJECT)
          AND n.summary IS NOT NULL
        RETURN elementId(n) AS elementId, n.summary AS summary
        """
        return self.neo4j_mgr.execute_read_query(query)
