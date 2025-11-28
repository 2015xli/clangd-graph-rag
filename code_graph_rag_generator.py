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
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, Callable, List, Optional
from tqdm import tqdm

try:
    import tiktoken
except ImportError:
    tiktoken = None

import re
import input_params
from rag_generation_prompts import RagGenerationPromptManager # New import

_SPECIAL_PATTERN = re.compile(r"<\|[^|]+?\|>")

def sanitize_special_tokens(text: str) -> str:
    """Break up special tokens so the model won't treat them as control tokens."""
    return _SPECIAL_PATTERN.sub(lambda m: f"< |{m.group(0)[2:-2]}| >", text)
from neo4j_manager import Neo4jManager, align_string
from llm_client import get_llm_client, LlmClient, get_embedding_client, EmbeddingClient

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# --- Constants for Summarization ---
TOKEN_ENCODING = "cl100k_base"

# --- Main RAG Generation Logic ---

class RagGenerator:
    """Orchestrates the generation of RAG data.
    
    Designed with a separation of concerns:
    - Graph traversal methods are separate from
    - Single-item processing methods.
    """

    def __init__(self, neo4j_mgr: Neo4jManager, project_path: str, 
                 llm_client: LlmClient, embedding_client: EmbeddingClient, 
                 num_local_workers: int, num_remote_workers: int, max_context_size: int):
        self.neo4j_mgr = neo4j_mgr
        self.project_path = os.path.abspath(project_path)
        self.llm_client = llm_client
        self.embedding_client = embedding_client
        self.num_local_workers = num_local_workers
        self.num_remote_workers = num_remote_workers
        self.max_context_token_size = max_context_size
        self.iterative_chunk_size = int(0.5 * self.max_context_token_size)
        self.iterative_chunk_overlap = int(0.1 * self.iterative_chunk_size)
        self.prompt_manager = RagGenerationPromptManager() # New instantiation
        self.tokenizer = None
        if tiktoken:
            try:
                self.tokenizer = tiktoken.get_encoding(TOKEN_ENCODING)
            except Exception as e:
                logger.warning(f"Could not initialize tiktoken tokenizer: {e}. Falling back to character count heuristic.")

    def _get_token_count(self, text: str) -> int:
        """Returns the number of tokens in a string, using a tokenizer if available."""
        if self.tokenizer:
            safe_text = sanitize_special_tokens(text)
            return len(self.tokenizer.encode(safe_text))
        return len(text) // 4 # Fallback heuristic

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

    def summarize_targeted_update(self, seed_symbol_ids: set, structurally_changed_files: dict):
        """
        Runs a targeted, multi-pass summarization handling both content and structural changes.
        """
        if not seed_symbol_ids and not any(structurally_changed_files.values()):
            logging.info("No seed symbols or structural changes provided for targeted update. Skipping.")
            return

        logging.info(f"\n--- Starting Targeted RAG Update for {len(seed_symbol_ids)} seed symbols and {sum(len(v) for v in structurally_changed_files.values())} structural file changes ---")

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

        # --- File & Folder Roll-up Passes (Content + Structural Changes) ---
        
        # 1. Identify files that trigger a file-level re-summary
        files_with_summary_changes = self._find_files_for_updated_symbols(updated_final_summary_ids)
        added_files = set(structurally_changed_files.get('added', []))
        modified_files = set(structurally_changed_files.get('modified', []))
        files_to_resummarize = files_with_summary_changes.union(added_files).union(modified_files)
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
        max_workers = self.num_local_workers if self.llm_client.is_local else self.num_remote_workers
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
        MATCH (n)
        WHERE n.id IN $function_ids AND (n:FUNCTION OR n:METHOD) AND n.codeSummary IS NULL AND n.body_location IS NOT NULL
        RETURN n.id AS id, n.name AS name, n.path AS path, n.body_location as body_location
        """
        return self.neo4j_mgr.execute_read_query(query, {"function_ids": function_ids})

    def _process_one_function_for_code_summary(self, func: dict) -> str | None:
        """
        Orchestrates the code-only summary for a single function/method.
        This now uses the iterative summarizer for all functions, which handles both large and small functions.
        """
        func_id = func['id']
        body_location = func.get('body_location')
        file_path = func.get('path')

        if not body_location or not isinstance(body_location, list) or len(body_location) != 4 or not file_path:
            logging.warning(f"Invalid or missing body_location/path for function {func_id}. Skipping.")
            return None
        
        start_line, start_col, end_line, end_col = body_location
        source_code = self._get_source_code_for_location(file_path, start_line, end_line)
        if not source_code:
            return None

        # Construct context_info for logging
        summary = self._summarize_function_text_iteratively(source_code, func)

        if not summary:
            return None

        update_query = "MATCH (n {id: $id}) WHERE n:FUNCTION OR n:METHOD SET n.codeSummary = $summary"
        self.neo4j_mgr.execute_autocommit_query(update_query, {"id": func_id, "summary": summary})
        return func_id

    def _chunk_text_by_tokens(self, text: str, chunk_size: int, overlap: int) -> List[str]:
        """Splits text into overlapping chunks based on token count using a stride."""
        if not self.tokenizer:
            # Fallback to character-based chunking if tokenizer is not available
            stride = (chunk_size - overlap) * 4
            return [text[i:i + chunk_size*4] for i in range(0, len(text), stride)]

        safe_text = sanitize_special_tokens(text)
        tokens = self.tokenizer.encode(safe_text)
        if not tokens:
            return []

        stride = chunk_size - overlap
        chunks = []
        
        i = 0
        while True:
            # The last chunk should just be the remainder
            if i + chunk_size >= len(tokens):
                chunks.append(tokens[i:])
                break
            
            chunks.append(tokens[i:i + chunk_size])
            i += stride
            # If the next stride would be the last chunk, but the remainder is small,
            # just make the current chunk the last one to avoid a tiny final chunk.
            if i + chunk_size >= len(tokens) and len(tokens) - i < (chunk_size * 0.5):
                 chunks[-1] = tokens[i-stride:]
                 break

        return [self.tokenizer.decode(chunk) for chunk in chunks]

    def _summarize_function_text_iteratively(self, text: str, func: dict) -> str:
        """Summarizes a piece of text using an iterative, sequential approach."""
        token_count = self._get_token_count(text)
        if token_count <= self.max_context_token_size:
            chunks = [text]
        else:
            context_info = f"function/method {func['name']} ({func['path']}:{func['body_location'][0]+1}:{func['body_location'][1]+1})"
            log_prefix = f"Text of {context_info} is large" if context_info else "Text is large"
            logging.info(f"{log_prefix} ({token_count} tokens), chunking for iterative summarization...")
            chunks = self._chunk_text_by_tokens(text, self.iterative_chunk_size, self.iterative_chunk_overlap)
        
        running_summary = ""

        for i, chunk in enumerate(chunks):
            is_first_chunk = (i == 0)
            is_last_chunk = (i == len(chunks) - 1)

            # Construct the prompt dynamically
            prompt = self.prompt_manager.get_code_summary_prompt(chunk, is_first_chunk, is_last_chunk, running_summary)

            # Get the new summary
            running_summary = self.llm_client.generate_summary(prompt)
            if not running_summary:
                logger.error(f"Iterative summarization failed at chunk {i+1}.")
                return ""

        return running_summary

    # --- Pass 2 Methods ---
    def summarize_functions_with_context(self):
        """PASS 2: Generates a final, context-aware summary for all functions and methods."""
        logging.info("--- Starting Pass 2: Summarizing Functions & Methods With Context ---")
        
        items_to_process = self._get_functions_for_contextual_summary()
        if not items_to_process:
            logging.info("No items require summarization in Pass 2.")
            return
        
        # This pass is not parallelized because the iterative summarization within
        # each item can be resource-intensive.
        for item in tqdm(items_to_process, desc=align_string("Pass 2: Context Summaries")):
            self._process_one_function_for_contextual_summary(item['id'])

        logging.info("--- Finished Pass 2 ---")

    def _summarize_functions_with_context_with_ids(self, function_ids: list[str]) -> set:
        """
        Core logic for Pass 2, operating on a specific list of function/method IDs.
        Returns the set of function IDs that were actually updated.
        """
        if not function_ids:
            return set()

        logging.info(f"Found {len(function_ids)} functions/methods that need a final summary.")
        max_workers = self.num_local_workers if self.llm_client.is_local else self.num_remote_workers
        logging.info(f"Using {max_workers} parallel workers for Pass 2.")

        updated_ids = self._parallel_process(
            items=function_ids,
            process_func=self._process_one_function_for_contextual_summary,
            max_workers=max_workers,
            desc="Pass 2: Context Summaries"
        )
        return set(updated_ids)

    def _get_functions_for_contextual_summary(self) -> list[dict]:
        query = "MATCH (n) WHERE (n:FUNCTION OR n:METHOD) AND n.codeSummary IS NOT NULL AND n.summary IS NULL RETURN n.id AS id"
        return self.neo4j_mgr.execute_read_query(query)

    def _process_one_function_for_contextual_summary(self, func_id: str) -> str | None:
        """Orchestrates the generation of a context-aware summary for a single function/method."""
        context_query = """
        MATCH (n) WHERE n.id = $id AND (n:FUNCTION OR n:METHOD)
        OPTIONAL MATCH (caller)-[:CALLS]->(n) WHERE (caller:FUNCTION OR caller:METHOD) AND caller.codeSummary IS NOT NULL
        OPTIONAL MATCH (n)-[:CALLS]->(callee) WHERE (callee:FUNCTION OR callee:METHOD) AND callee.codeSummary IS NOT NULL
        RETURN n.codeSummary AS codeSummary,
               n.summary AS old_summary,
               collect(DISTINCT caller.codeSummary) AS callerSummaries,
               collect(DISTINCT callee.codeSummary) AS calleeSummaries
        """
        results = self.neo4j_mgr.execute_read_query(context_query, {"id": func_id})
        if not results: return None

        context = results[0]
        code_summary = context.get('codeSummary')
        if not code_summary: return None

        caller_summaries = context.get('callerSummaries', [])
        callee_summaries = context.get('calleeSummaries', [])

        # Check if everything fits in a single pass
        full_context_text = code_summary + " ".join(caller_summaries) + " ".join(callee_summaries)
        if self._get_token_count(full_context_text) < self.max_context_token_size:
            prompt = self._build_function_contextual_prompt(code_summary, caller_summaries, callee_summaries)
            final_summary = self.llm_client.generate_summary(prompt)
        else:
            final_summary = self._summarize_function_context_iteratively(code_summary, caller_summaries, callee_summaries)

        if final_summary and final_summary != context.get('old_summary'):
            update_query = "MATCH (n {id: $id}) WHERE n:FUNCTION OR n:METHOD SET n.summary = $summary REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(update_query, {"id": func_id, "summary": final_summary})
            return func_id
        
        return None


    def _build_function_contextual_prompt(self, code_summary, caller_summaries, callee_summaries) -> str:
        caller_text = ", ".join([s for s in caller_summaries if s]) or "none"
        callee_text = ", ".join([s for s in callee_summaries if s]) or "none"
        return self.prompt_manager.get_contextual_function_prompt(code_summary, caller_text, callee_text)

    def _summarize_function_context_iteratively(self, code_summary: str, caller_summaries: List[str], callee_summaries: List[str]) -> str:
        """Generates a contextual summary by iteratively processing batches of caller and callee summaries."""
        logging.info(f"Context for function is too large, starting iterative contextual summarization...")

        # Stage 1: Fold in caller context
        caller_aware_summary = self._summarize_relations_iteratively(code_summary, caller_summaries, "function_has_callers")

        # Stage 2: Fold in callee context
        final_summary = self._summarize_relations_iteratively(caller_aware_summary, callee_summaries, "function_has_callees")

        return final_summary

    def _summarize_relations_iteratively(self, summary: str, relation_summaries: List[str], relation_name: str) -> str:
        """Generic helper to iteratively fold a list of relation summaries into a base summary."""
        if not relation_summaries:
            return summary

        relations_text = "\n - ".join(relation_summaries)
        relation_chunks = self._chunk_text_by_tokens(relations_text, self.iterative_chunk_size, self.iterative_chunk_overlap)

        running_summary = summary
        for i, chunk in enumerate(relation_chunks):
            if len(relation_chunks) > 1: # Only log if there's more than one chunk
                prompt = self.prompt_manager.get_iterative_relation_prompt(relation_name, running_summary, chunk)
            else:
                logging.info(f"Iteratively processing {relation_name} batch {i+1}/{len(relation_chunks)}...")
                prompt = self.prompt_manager.get_iterative_relation_prompt(relation_name, running_summary, chunk)
            
            running_summary = self.llm_client.generate_summary(prompt)
            if not running_summary:
                logger.error(f"Iterative relation summarization failed at chunk {i+1}.")
                return summary # Return the last good summary
        
        return running_summary

    # --- Pass 3 Methods: Class Summaries ---
    def summarize_class_structures(self):
        """PASS 3: Generates a summary for all class structures in the graph."""
        logging.info("\n--- Starting Pass 3: Summarizing Class Structures ---")
        
        items_to_process = self._get_classes_for_summary()
        if not items_to_process:
            logging.info("No class structures require summarization.")
            return

        logging.info(f"Found {len(items_to_process)} class structures to summarize.")
        max_workers = self.num_local_workers if self.llm_client.is_local else self.num_remote_workers
        
        self._parallel_process(
            items=items_to_process,
            process_func=self._summarize_one_class_structure,
            max_workers=max_workers,
            desc="Pass 3: Class Summaries"
        )
        logging.info("--- Finished Pass 3 ---")

    def _get_classes_for_summary(self) -> list[dict]:
        """Fetches CLASS_STRUCTURE nodes that need a summary."""
        query = """
        MATCH (c:CLASS_STRUCTURE)
        WHERE c.summary IS NULL
        RETURN c.id AS id, c.name AS name
        """
        return self.neo4j_mgr.execute_read_query(query)

    def _summarize_one_class_structure(self, class_info: dict) -> str | None:
        """Orchestrates the generation of a summary for a single class structure."""
        class_id = class_info['id']
        class_name = class_info['name']

        context_query = """
        MATCH (c:CLASS_STRUCTURE {id: $id})
        // Get summaries of directly inherited parent classes
        OPTIONAL MATCH (c)-[:INHERITS]->(parent:CLASS_STRUCTURE)
        WHERE parent.summary IS NOT NULL
        WITH c, collect(DISTINCT parent.summary) AS parentSummaries
        // Get summaries of own methods
        OPTIONAL MATCH (c)-[:HAS_METHOD]->(method:METHOD)
        WHERE method.summary IS NOT NULL
        WITH c, parentSummaries, collect(DISTINCT method.summary) AS methodSummaries
        // Get names and types of own fields
        OPTIONAL MATCH (c)-[:HAS_FIELD]->(field:FIELD)
        RETURN c.summary AS old_summary,
               parentSummaries,
               methodSummaries,
               collect(DISTINCT {name: field.name, type: field.type}) AS fields
        """
        results = self.neo4j_mgr.execute_read_query(context_query, {"id": class_id})
        if not results: return None

        context = results[0]
        parent_summaries = context.get('parentSummaries', [])
        method_summaries = context.get('methodSummaries', [])
        fields = context.get('fields', [])

        # If there's no content to summarize, don't proceed
        if not parent_summaries and not method_summaries and not fields:
            return None

        # Check if everything fits in a single pass
        field_text = ", ".join([f"{f['type']} {f['name']}" for f in fields if f and f.get('name') and f.get('type')])
        full_context_text = " ".join(parent_summaries) + " ".join(method_summaries) + field_text
        
        if self._get_token_count(full_context_text) < self.max_context_token_size:
            prompt = self._build_class_summary_prompt(class_name, parent_summaries, method_summaries, field_text)
            final_summary = self.llm_client.generate_summary(prompt)
        else:
            final_summary = self._summarize_class_context_iteratively(class_name, parent_summaries, method_summaries, field_text)

        if final_summary and final_summary != context.get('old_summary'):
            update_query = "MATCH (c:CLASS_STRUCTURE {id: $id}) SET c.summary = $summary REMOVE c.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(update_query, {"id": class_id, "summary": final_summary})
            return class_id
        
        return None

    def _build_class_summary_prompt(self, class_name: str, parent_summaries: list[str], method_summaries: list[str], field_text: str) -> str:
        """Constructs the prompt for summarizing a class."""
        parent_text = f"It inherits from classes with these roles: [{'; '.join(parent_summaries)}]." if parent_summaries else ""
        field_text_prompt = f"It has the following data members: [{field_text}]." if field_text else ""
        method_text = f"It has methods that perform these functions: [{'; '.join(method_summaries)}]." if method_summaries else ""

        return self.prompt_manager.get_class_summary_prompt(class_name, parent_text, field_text_prompt, method_text)

    def _summarize_class_context_iteratively(self, class_name: str, parent_summaries: list[str], method_summaries: list[str], field_text: str) -> str:
        """Generates a class summary by iteratively processing its context components."""
        logging.info(f"Context for class '{class_name}' is too large, starting iterative summarization...")

        # Start with a base description including fields, as they are usually small.
        base_summary = f"The class '{class_name}' has data members: [{field_text}]."

        # Stage 1: Fold in inheritance context
        inheritance_aware_summary = self._summarize_relations_iteratively(base_summary, parent_summaries, "class_has_parents")

        # Stage 2: Fold in method context
        final_summary = self._summarize_relations_iteratively(inheritance_aware_summary, method_summaries, "class_has_methods")

        return final_summary

    # --- Pass 4 Methods: Namespace Summaries ---
    def summarize_namespaces(self):
        """PASS 4: Generates a summary for all namespace nodes in the graph."""
        logging.info("\n--- Starting Pass 4: Summarizing Namespaces ---")

        namespaces_to_process = self._get_namespaces_for_summary()
        if not namespaces_to_process:
            logging.info("No namespaces require summarization.")
            return

        # Sort namespaces bottom-up (deepest first)
        namespaces_to_process.sort(key=lambda ns: ns['qualified_name'].count('::'), reverse=True)

        logging.info(f"Found {len(namespaces_to_process)} namespaces to summarize.")
        max_workers = self.num_local_workers if self.llm_client.is_local else self.num_remote_workers
        
        self._parallel_process(
            items=namespaces_to_process,
            process_func=self._summarize_one_namespace,
            max_workers=max_workers,
            desc="Pass 4: Namespace Summaries"
        )
        logging.info("--- Finished Pass 4 ---")

    def _get_namespaces_for_summary(self) -> list[dict]:
        """Fetches NAMESPACE nodes that need a summary."""
        query = """
        MATCH (n:NAMESPACE)
        WHERE n.summary IS NULL
        RETURN n.qualified_name AS qualified_name, n.name AS name
        """
        return self.neo4j_mgr.execute_read_query(query)

    def _summarize_one_namespace(self, namespace_info: dict) -> str | None:
        """Orchestrates the generation of a summary for a single namespace."""
        qualified_name = namespace_info['qualified_name']
        ns_name = namespace_info['name']

        context_query = """
        MATCH (ns:NAMESPACE {qualified_name: $qualified_name})-[:CONTAINS]->(child)
        WHERE child.summary IS NOT NULL
        RETURN labels(child)[-1] AS label, child.name AS name, child.summary AS summary
        """
        results = self.neo4j_mgr.execute_read_query(context_query, {"qualified_name": qualified_name})
        child_summaries = [f"The {r['label'].lower()} '{r['name']}' is responsible for: {r['summary']}" for r in results]
        if not child_summaries: return None

        child_summaries_text = '; '.join(child_summaries)
        full_context_text = child_summaries_text
        
        if self._get_token_count(full_context_text) < self.max_context_token_size:
            prompt = self.prompt_manager.get_namespace_summary_prompt(ns_name, child_summaries_text)
            final_summary = self.llm_client.generate_summary(prompt)
        else:
            final_summary = self._summarize_namespace_iteratively(namespace_info, child_summaries)

        if final_summary:
            update_query = "MATCH (n:NAMESPACE {qualified_name: $qualified_name}) SET n.summary = $summary REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(update_query, {"qualified_name": qualified_name, "summary": final_summary})
            return qualified_name
        
        return None

    def _summarize_namespace_iteratively(self, namespace_info: dict, child_summaries: List[str]) -> str:
        """Generates a namespace summary by iteratively processing its child summaries."""
        logging.info(f"Context for namespace '{namespace_info['qualified_name']}' is too large, starting iterative summarization...")

        relations_text = "\n - ".join(child_summaries)
        relation_chunks = self._chunk_text_by_tokens(relations_text, self.iterative_chunk_size, self.iterative_chunk_overlap)

        running_summary = f"The namespace '{namespace_info['qualified_name']}' contains various components."
        for i, chunk in enumerate(relation_chunks):
            if len(relation_chunks) > 1:
                logging.info(f"Iteratively processing namespace children batch {i+1}/{len(relation_chunks)}...")
            
            prompt = self.prompt_manager.get_iterative_namespace_children_prompt(namespace_info['qualified_name'], running_summary, chunk)
            running_summary = self.llm_client.generate_summary(prompt)
            if not running_summary:
                logger.error(f"Iterative namespace summarization failed at chunk {i+1}.")
                return running_summary # Return the last good summary
        
        return running_summary

    # --- Pass 5 Methods ---
    def _summarize_all_files(self):
        logging.info("\n--- Starting Pass 5: Summarizing All Files ---")
        # Query for all files, not just ones with summary is null, to ensure correctness on re-runs
        files_to_process = self.neo4j_mgr.execute_read_query("MATCH (f:FILE) RETURN f.path AS path")
        if not files_to_process:
            logging.info("No files found to summarize.")
            return
        
        file_paths = {f['path'] for f in files_to_process}
        self._summarize_files_with_paths(file_paths)

    def _summarize_files_with_paths(self, file_paths: set):
        """Core logic for summarizing a specific set of FILE nodes."""
        if not file_paths:
            return
        logging.info(f"Summarizing {len(file_paths)} FILE nodes...")
        max_workers = self.num_local_workers if self.llm_client.is_local else self.num_remote_workers
        self._parallel_process(
            items=list(file_paths),
            process_func=self._summarize_one_file,
            max_workers=max_workers,
            desc="File Summaries"
        )

    def _summarize_one_file(self, file_path: str):
        query = """
        MATCH (f:FILE {path: $path})-[:DEFINES]->(s)
        WHERE (s:FUNCTION OR s:CLASS_STRUCTURE) AND s.summary IS NOT NULL
        RETURN s.summary AS summary
        """
        results = self.neo4j_mgr.execute_read_query(query, {"path": file_path})
        summaries = [r['summary'] for r in results if r['summary']]
        if not summaries: return

        prompt = self.prompt_manager.get_file_summary_prompt(os.path.basename(file_path), '; '.join(summaries))
        summary = self.llm_client.generate_summary(prompt)
        if not summary: return

        update_query = "MATCH (f:FILE {path: $path}) SET f.summary = $summary REMOVE f.summaryEmbedding"
        self.neo4j_mgr.execute_autocommit_query(update_query, {"path": file_path, "summary": summary})

    # --- Pass 6 Methods ---
    def _summarize_all_folders(self):
        logging.info("\n--- Starting Pass 6: Summarizing All Folders (bottom-up) ---")
        folders_to_process = self.neo4j_mgr.execute_read_query("MATCH (f:FOLDER) RETURN f.path AS path")
        if not folders_to_process:
            logging.info("No folders found to summarize.")
            return

        folder_paths = {f['path'] for f in folders_to_process}
        self._summarize_folders_with_paths(folder_paths)

    def _summarize_folders_with_paths(self, folder_paths: set):
        """Core logic for summarizing a specific set of FOLDER nodes."""
        if not folder_paths:
            return

        logging.info(f"Found {len(folder_paths)} potentially affected FOLDER nodes. Verifying existence in graph...")
        folder_details_query = "UNWIND $paths as path MATCH (f:FOLDER {path: path}) RETURN f.path as path, f.name as name"
        folder_details = self.neo4j_mgr.execute_read_query(folder_details_query, {"paths": list(folder_paths)})

        if not folder_details:
            logging.info("No affected folders exist in the graph. No roll-up needed.")
            return

        logging.info(f"Rolling up summaries for {len(folder_details)} existing FOLDER nodes...")
        folders_by_depth = {}
        for folder in folder_details:
            depth = folder['path'].count(os.sep)
            if depth not in folders_by_depth:
                folders_by_depth[depth] = []
            folders_by_depth[depth].append(folder)

        max_workers = self.num_local_workers if self.llm_client.is_local else self.num_remote_workers
        for depth in sorted(folders_by_depth.keys(), reverse=True):
            self._parallel_process(
                items=folders_by_depth[depth],
                process_func=lambda f: self._summarize_one_folder(f['path'], f['name']),
                max_workers=max_workers,
                desc=f"Folder Roll-up (Depth {depth})"
            )

    def _summarize_one_folder(self, folder_path: str, folder_name: str):
        query = """
        MATCH (parent:FOLDER {path: $path})-[:CONTAINS]->(child)
        WHERE child.summary IS NOT NULL
        RETURN labels(child)[0] as label, child.name as name, child.summary as summary
        """
        results = self.neo4j_mgr.execute_read_query(query, {"path": folder_path})
        child_summaries = [f"{r['label'].lower()} '{r['name']}' is responsible for: {r['summary']}" for r in results]
        if not child_summaries: return

        prompt = self.prompt_manager.get_folder_summary_prompt(folder_name, '; '.join(child_summaries))
        summary = self.llm_client.generate_summary(prompt)
        if not summary: return

        update_query = "MATCH (f:FOLDER {path: $path}) SET f.summary = $summary REMOVE f.summaryEmbedding"
        self.neo4j_mgr.execute_autocommit_query(update_query, {"path": folder_path, "summary": summary})

    def _summarize_project(self):
        """Summarizes the top-level PROJECT node."""
        logging.info("Summarizing the PROJECT node...")
        query = """
        MATCH (p:PROJECT)-[:CONTAINS]->(child)
        WHERE child.summary IS NOT NULL
        RETURN labels(child)[-1] as label, child.name as name, child.summary as summary
        """
        results = self.neo4j_mgr.execute_read_query(query)
        if not results: 
            logging.warning("No summarized children found for PROJECT node. Skipping.")
            return

        child_summaries = [f"The {r['label'].lower()} '{r['name']}' is responsible for: {r['summary']}" for r in results]
        prompt = self.prompt_manager.get_project_summary_prompt('; '.join(child_summaries))
        summary = self.llm_client.generate_summary(prompt)
        if not summary: return

        update_query = "MATCH (p:PROJECT) SET p.summary = $summary REMOVE p.summaryEmbedding"
        self.neo4j_mgr.execute_autocommit_query(update_query, {"summary": summary})
        logging.info("-> Stored summary for PROJECT node.")

    # --- Pass 7 Methods ---
    def generate_embeddings(self):
        """PASS 7: Generates and stores embeddings for all generated summaries in batches."""
        logging.info("\n--- Starting Pass 7: Generating Embeddings ---")
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

        logging.info("--- Finished Pass 5 ---")

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
    
    # Add argument groups from the centralized module
    input_params.add_core_input_args(parser)
    input_params.add_rag_args(parser)
    input_params.add_worker_args(parser)

    args = parser.parse_args()

    # Resolve paths and convert back to strings
    # project_path is needed, but index_file is not directly used by the generator anymore
    args.project_path = str(args.project_path.resolve())

    try:
        llm_client = get_llm_client(args.llm_api)
        embedding_client = get_embedding_client(args.llm_api)

        with Neo4jManager() as neo4j_mgr:
            if not neo4j_mgr.check_connection(): return 1

            if not neo4j_mgr.verify_project_path(args.project_path):
                return 1
            
            # The standalone generator now assumes the graph has been built
            # with body_location properties. It works directly on the graph.
            generator = RagGenerator(
                neo4j_mgr, 
                args.project_path, 
                llm_client, 
                embedding_client,
                args.num_local_workers,
                args.num_remote_workers,
                args.max_context_size
            )
            
            generator.summarize_code_graph()

            neo4j_mgr.create_vector_indices()

    except Exception as e:
        logging.critical(f"A critical error occurred: {e}", exc_info=True)
        return 1

if __name__ == "__main__":
    main()
