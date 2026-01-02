#!/usr/bin/env python3
"""
This module provides the NodeSummaryProcessor class, which acts as a stateless
worker for generating RAG summaries.
"""

import os
import logging
import hashlib
import tiktoken
import re
from typing import Optional, Dict, Any, Tuple, List

from summary_cache_manager import SummaryCacheManager
from llm_client import LlmClient
from rag_generation_prompts import RagGenerationPromptManager

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

_SPECIAL_PATTERN = re.compile(r"<\|[^|]+?\|>")

def _sanitize_special_tokens(text: str) -> str:
    """Breaks up special tokens so they are not treated as control tokens."""
    return _SPECIAL_PATTERN.sub(lambda m: f"< |{m.group(0)[2:-2]}| >", text)

class NodeSummaryProcessor:
    """
    A stateless worker for summary generation. It reads from a shared cache
    but does not mutate any shared state.
    """

    def __init__(self, 
                 project_path: str,
                 cache_manager: SummaryCacheManager,
                 llm_client: LlmClient,
                 prompt_manager: RagGenerationPromptManager,
                 token_encoding: str = 'cl100k_base',
                 max_context_token_size: Optional[int] = None):
        
        self.project_path = project_path
        self.cache_manager = cache_manager
        self.llm_client = llm_client
        self.prompt_manager = prompt_manager
        self.tokenizer = tiktoken.get_encoding(token_encoding)

        if max_context_token_size:
            self.max_context_token_size = max_context_token_size
            self.iterative_chunk_size = int(0.5 * self.max_context_token_size)
            self.iterative_chunk_overlap = int(0.1 * self.iterative_chunk_size)
        else:
            self.max_context_token_size = None

    def get_function_code_analysis(self, node_data: dict) -> Tuple[str, dict]:
        """
        Performs the one-pass staleness check and generation for a function's code_analysis.
        Returns a status and a data dictionary.
        """
        node_id = node_data['id']
        label = node_data['label']
        db_code_hash = node_data.get('db_code_hash')
        db_code_analysis = node_data.get('db_code_analysis')

        # 1. Read source code once
        start_line, _, end_line, _ = node_data.get('body_location')
        source_code = self._get_source_code_for_location(
            node_data.get('path'),
            start_line, end_line
        )
        if not source_code:
            logger.error(f"Cannot generate code analysis for {label} {node_id}: source code not found.")
            return "generation_failed", {} # Cannot process

        # 2. Calculate new hash
        new_code_hash = hashlib.md5(source_code.encode('utf-8')).hexdigest()

        # 3. Staleness Check
        # Path A: DB is up-to-date
        if db_code_hash == new_code_hash and db_code_analysis:
            return "unchanged", {"code_hash": new_code_hash, "code_analysis": db_code_analysis}

        # Path B: DB is stale, check historical cache
        cached_entry = self.cache_manager.get_cache_entry(label, node_id)
        cache_code_analysis = cached_entry.get('code_analysis') if cached_entry else None
        cache_code_hash = cached_entry.get('code_hash') if cached_entry else None
        if cache_code_hash == new_code_hash and cache_code_analysis:
            return "code_analysis_restored", {"code_hash": new_code_hash, "code_analysis": cache_code_analysis}

        # Path C: Cache miss, generate new analysis
        new_code_analysis = self._analyze_function_text_iteratively(source_code, node_data)
        if not new_code_analysis: # Condition 2 Check
            logger.error(f"Failed to generate code analysis for {label} {node_id}")
            return "generation_failed", {"code_hash": new_code_hash, "code_analysis": db_code_analysis or cache_code_analysis}

        return "code_analysis_regenerated", {"code_hash": new_code_hash, "code_analysis": new_code_analysis}

    def get_function_contextual_summary(self, node_data: dict, caller_entities: List[dict], callee_entities: List[dict]) -> Tuple[str, dict]:
        """
        Performs staleness checks and generates a final, context-aware summary for a function.
        """
        node_id = node_data['id']
        label = node_data['label']
        db_summary = node_data.get('summary')
        cached_entry = self.cache_manager.get_cache_entry(label, node_id)
        cache_summary = cached_entry.get('summary') if cached_entry else None

        # Staleness Check
        is_self_stale = self.cache_manager.get_runtime_status(label, node_id).get('code_analysis_changed', False)
        is_neighbor_stale = any(
            self.cache_manager.get_runtime_status(dep['label'], dep['id']).get('code_analysis_changed')
            for dep in caller_entities + callee_entities
        )
        is_stale = is_self_stale or is_neighbor_stale

        # Path A: Perfect state, nothing to do.
        if not is_stale and db_summary:
            return "unchanged", {"summary": db_summary}

        # Path B: DB is missing summary, but cache has a valid one. Restore from cache.
        if not is_stale and cache_summary:
            return "summary_restored", {"summary": cache_summary}

        # Path C: Must regenerate (either because it's stale, or no valid summary exists anywhere)
        own_cached_data = self.cache_manager.get_cache_entry(label, node_id)
        code_analysis = own_cached_data.get('code_analysis') if own_cached_data else None

        if not code_analysis:
            logger.error(f"Cannot generate contextual summary for {node_id}: missing own code_analysis in cache.")
            return "unchanged", {"summary": db_summary}

        caller_analyses = [
            summary
            for c in caller_entities
            if (summary := (self.cache_manager.get_cache_entry(c['label'], c['id']) or {}).get('code_analysis'))
        ]
        callee_analyses = [
            summary
            for c in callee_entities
            if (summary := (self.cache_manager.get_cache_entry(c['label'], c['id']) or {}).get('code_analysis'))
        ]

        full_context_text = code_analysis + " ".join(caller_analyses) + " ".join(callee_analyses)
        if self._get_token_count(full_context_text) < self.max_context_token_size:
            prompt = self._build_function_contextual_prompt(code_analysis, caller_analyses, callee_analyses)
            final_summary = self.llm_client.generate_summary(prompt)
        else:
            final_summary = self._summarize_function_context_iteratively(code_analysis, caller_analyses, callee_analyses)

        if not final_summary:
            logger.error(f"Failed to generate contextual summary for {node_id}.")
            return "generation_failed", {"summary": db_summary or cache_summary}

        return "summary_regenerated", {"summary": final_summary}

    def get_class_summary(self, node_data: dict, parent_entities: List[dict], method_entities: List[dict], field_entities: List[dict]) -> Tuple[str, dict]:
        """
        Generates a summary for a class structure.
        """
        node_id = node_data['id']
        label = 'CLASS_STRUCTURE'
        db_summary = node_data.get('summary')
        cached_entry = self.cache_manager.get_cache_entry(label, node_id)
        cache_summary = cached_entry.get('summary') if cached_entry else None

        is_stale = any(
            self.cache_manager.get_runtime_status(dep['label'], dep['id']).get('summary_changed')
            for dep in parent_entities + method_entities
        )

        if not is_stale and db_summary:
            return "unchanged", {"summary": db_summary}

        if not is_stale and cache_summary:
            return "summary_restored", {"summary": cache_summary}

        parent_summaries = [
            summary
            for p in parent_entities
            if (summary := (self.cache_manager.get_cache_entry(p['label'], p['id']) or {}).get('summary'))
        ]
        method_summaries = [
            summary
            for m in method_entities
            if (summary := (self.cache_manager.get_cache_entry(m['label'], m['id']) or {}).get('summary'))
        ]
        
        field_text = ", ".join([f"{f['type']} {f['name']}" for f in field_entities if f and f.get('name') and f.get('type')])
        full_context_text = " ".join(parent_summaries) + " ".join(method_summaries) + field_text

        if self._get_token_count(full_context_text) < self.max_context_token_size:
            prompt = self._build_class_summary_prompt(node_data['name'], parent_summaries, method_summaries, field_text)
            final_summary = self.llm_client.generate_summary(prompt)
        else:
            logger.info(f"Context for class '{node_data['name']}' is too large, starting iterative summarization...")
            base_summary = f"The class '{node_data['name']}' has data members: [{field_text}]."
            inheritance_aware_summary = self._summarize_relations_iteratively(base_summary, parent_summaries, "class_has_parents", node_data['name'])
            final_summary = self._summarize_relations_iteratively(inheritance_aware_summary, method_summaries, "class_has_methods", node_data['name'])

        if not final_summary:
            logger.error(f"Failed to generate summary for class '{node_data['name']}'.")
            return "generation_failed", {"summary": db_summary or cache_summary}

        return "summary_regenerated", {"summary": final_summary}

    def get_namespace_summary(self, node_data: dict, child_entities: List[dict]) -> Tuple[str, dict]:
        """
        Generates a summary for a namespace.
        """
        node_id = node_data['id']
        label = 'NAMESPACE'
        db_summary = node_data.get('summary')
        cached_entry = self.cache_manager.get_cache_entry(label, node_id)
        cache_summary = cached_entry.get('summary') if cached_entry else None

        is_stale = any(
            self.cache_manager.get_runtime_status(dep['label'], dep['id']).get('summary_changed')
            for dep in child_entities
        )

        if not is_stale and db_summary:
            return "unchanged", {"summary": db_summary}

        if not is_stale and cache_summary:
            return "summary_restored", {"summary": cache_summary}

        child_summaries = [
            f"The {c['label'].lower()} '{c['name']}' is responsible for: {summary}"
            for c in child_entities
            if (summary := (self.cache_manager.get_cache_entry(c['label'], c['id']) or {}).get('summary'))
        ]
        if not child_summaries:
            logger.debug(f"Cannot generate summary for namespace {node_id}: no child summaries found.")
            return "no_children", {"summary": db_summary or cache_summary}

        final_summary = self._generate_hierarchical_summary(
            entity_name=node_data['name'],
            relation_name="namespace_children",
            relation_summaries=child_summaries
        )
        
        if not final_summary:
            logger.error(f"Failed to generate summary for namespace '{node_data['name']}'.")
            return "generation_failed", {"summary": db_summary or cache_summary}

        return "summary_regenerated", {"summary": final_summary}

    def get_file_summary(self, node_data: dict, child_entities: List[dict]) -> Tuple[str, dict]:
        """
        Generates a summary for a file.
        """
        node_id = node_data['path']
        label = 'FILE'
        db_summary = node_data.get('summary')
        cached_entry = self.cache_manager.get_cache_entry(label, node_id)
        cache_summary = cached_entry.get('summary') if cached_entry else None

        is_stale = any(
            self.cache_manager.get_runtime_status(dep['label'], dep['id']).get('summary_changed')
            for dep in child_entities
        )

        if not is_stale and db_summary:
            return "unchanged", {"summary": db_summary}

        if not is_stale and cache_summary:
            return "summary_restored", {"summary": cache_summary}

        child_summaries = [
            f"The {c['label'].lower()} '{c['name']}' is responsible for: {summary}"
            for c in child_entities
            if (summary := (self.cache_manager.get_cache_entry(c['label'], c['id']) or {}).get('summary'))
        ]
        if not child_summaries:
            logger.debug(f"Cannot generate summary for file {node_id}: no child summaries found.")
            return "no_children", {"summary": db_summary or cache_summary}

        final_summary = self._generate_hierarchical_summary(
            entity_name=node_data['name'],
            relation_name="file_children",
            relation_summaries=child_summaries
        )
        if not final_summary:
            logger.error(f"Failed to generate summary for file '{node_data['name']}'.")
            return "generation_failed", {"summary": db_summary or cache_summary}
        return "summary_regenerated", {"summary": final_summary}

    def get_folder_summary(self, node_data: dict, child_entities: List[dict]) -> Tuple[str, dict]:
        """
        Generates a summary for a folder.
        """
        node_id = node_data['path']
        label = 'FOLDER'
        db_summary = node_data.get('summary')
        cached_entry = self.cache_manager.get_cache_entry(label, node_id)
        cache_summary = cached_entry.get('summary') if cached_entry else None

        is_stale = any(
            self.cache_manager.get_runtime_status(dep['label'], dep.get('path') or dep.get('id')).get('summary_changed')
            for dep in child_entities
        )

        if not is_stale and db_summary:
            return "unchanged", {"summary": db_summary}

        if not is_stale and cache_summary:
            return "summary_restored", {"summary": cache_summary}

        child_summaries = [
            f"The {c['label'].lower()} '{c['name']}' is responsible for: {summary}"
            for c in child_entities
            if (summary := (self.cache_manager.get_cache_entry(c['label'], c.get('path') or c.get('id')) or {}).get('summary'))
        ]
        if not child_summaries:
            logger.debug(f"Cannot generate summary for folder {node_id}: no child summaries found.")
            return "no_children", {"summary": db_summary or cache_summary}

        final_summary = self._generate_hierarchical_summary(
            entity_name=node_data['name'],
            relation_name="folder_children",
            relation_summaries=child_summaries
        )
        if not final_summary:
            logger.error(f"Failed to generate summary for folder '{node_data['name']}'.")
            return "generation_failed", {"summary": db_summary or cache_summary}
        return "summary_regenerated", {"summary": final_summary}

    def get_project_summary(self, node_data: dict, child_entities: List[dict]) -> Tuple[str, dict]:
        """
        Generates a summary for the project.
        """
        node_id = node_data['path']
        label = 'PROJECT'
        db_summary = node_data.get('summary')
        cached_entry = self.cache_manager.get_cache_entry(label, node_id)
        cache_summary = cached_entry.get('summary') if cached_entry else None

        is_stale = any(
            self.cache_manager.get_runtime_status(dep['label'], dep.get('path') or dep.get('id')).get('summary_changed')
            for dep in child_entities
        )

        if not is_stale and db_summary:
            return "unchanged", {"summary": db_summary}

        if not is_stale and cache_summary:
            return "summary_restored", {"summary": cache_summary}

        child_summaries = [
            f"The {c['label'].lower()} '{c['name']}' is responsible for: {summary}"
            for c in child_entities
            if (summary := (self.cache_manager.get_cache_entry(c['label'], c.get('path') or c.get('id')) or {}).get('summary'))
        ]
        if not child_summaries:
            logger.debug(f"Cannot generate summary for project {node_id}: no child summaries found.")
            return "no_children", {"summary": db_summary or cache_summary}

        final_summary = self._generate_hierarchical_summary(
            entity_name=node_data['name'],
            relation_name="project_children",
            relation_summaries=child_summaries
        )
        if not final_summary:
            logger.error(f"Failed to generate summary for project '{node_data['name']}'.")
            return "generation_failed", {"summary": db_summary or cache_summary}
        return "summary_regenerated", {"summary": final_summary}

    def _build_class_summary_prompt(self, class_name: str, parent_summaries: list[str], method_summaries: list[str], field_text: str) -> str:
        """Constructs the prompt for summarizing a class."""
        parent_text = f"It inherits from classes with these roles: [{'; '.join(parent_summaries)}]." if parent_summaries else ""
        field_text_prompt = f"It has the following data members: [{field_text}]." if field_text else ""
        method_text = f"It has methods that perform these functions: [{'; '.join(method_summaries)}]." if method_summaries else ""
        return self.prompt_manager.get_class_summary_prompt(class_name, parent_text, field_text_prompt, method_text)

    def _generate_hierarchical_summary(self, entity_name: str, relation_name: str, relation_summaries: List[str]) -> Optional[str]:
        """
        Generic helper to generate a summary for a hierarchical node (namespace, file, folder).
        """
        if not relation_summaries:
            return None

        summaries_text = "; ".join(relation_summaries)
        
        if self._get_token_count(summaries_text) < self.max_context_token_size:
            if relation_name == "namespace_children":
                prompt = self.prompt_manager.get_namespace_summary_prompt(entity_name, summaries_text)
            elif relation_name == "file_children":
                prompt = self.prompt_manager.get_file_summary_prompt(entity_name, summaries_text)
            elif relation_name == "folder_children":
                prompt = self.prompt_manager.get_folder_summary_prompt(entity_name, summaries_text)
            elif relation_name == "project_children": # project
                prompt = self.prompt_manager.get_project_summary_prompt(summaries_text)
            else:
                raise ValueError(f"Unknown relation name: {relation_name}")
            return self.llm_client.generate_summary(prompt)
        else:
            # Fallback to iterative summarization for large contexts
            logger.info(f"Context for {relation_name} '{entity_name}' is too large, starting iterative summarization...")
            
            base_summary = f"The {relation_name.split('_')[0]} '{entity_name}' contains various components to be summarized iteratively."
            return self._summarize_relations_iteratively(base_summary, relation_summaries, relation_name, entity_name)

    def _build_function_contextual_prompt(self, code_analysis, caller_analyses, callee_analyses) -> str:
        caller_text = ", ".join([s for s in caller_analyses if s]) or "none"
        callee_text = ", ".join([s for s in callee_analyses if s]) or "none"
        return self.prompt_manager.get_contextual_function_prompt(code_analysis, caller_text, callee_text)

    def _summarize_function_context_iteratively(self, code_analysis: str, caller_analyses: List[str], callee_analyses: List[str]) -> str:
        """Generates a contextual summary by iteratively processing batches of caller and callee summaries."""
        logger.info(f"Context for function is too large, starting iterative contextual summarization...")
        caller_aware_summary = self._summarize_relations_iteratively(code_analysis, caller_analyses, "function_has_callers")
        final_summary = self._summarize_relations_iteratively(caller_aware_summary, callee_analyses, "function_has_callees")
        return final_summary

    def _summarize_relations_iteratively(self, summary: str, relation_summaries: List[str], relation_name: str, entity_name: Optional[str] = None) -> str:
        """Generic helper to iteratively fold a list of relation summaries into a base summary."""
        if not relation_summaries:
            return summary

        relation_chunks = self._chunk_strings_by_tokens(relation_summaries, self.iterative_chunk_size)
        running_summary = summary
        for i, chunk in enumerate(relation_chunks):
            prompt = self.prompt_manager.get_iterative_relation_prompt(relation_name, running_summary, chunk, entity_name)
            running_summary = self.llm_client.generate_summary(prompt)
            if not running_summary:
                logger.error(f"Iterative relation summarization failed at chunk {i+1}.")
                return summary
        return running_summary

    def _get_source_code_for_location(self, file_path: str, start_line: int, end_line: int) -> str:
        if not file_path or not self.project_path: return ""
        full_path = os.path.join(self.project_path, file_path)

        if not os.path.exists(full_path):
            logger.error(f"File not found when trying to extract source: {full_path}")
            return ""
        
        try:
            with open(full_path, 'r', errors='ignore') as f:
                lines = f.readlines()
            code_lines = lines[start_line : end_line + 1]
            return "".join(code_lines)
        except Exception as e:
            logger.error(f"Error reading file {full_path}: {e}")
            return ""

    def _analyze_function_text_iteratively(self, text: str, func: dict) -> str:
        token_count = self._get_token_count(text)
        if token_count <= self.max_context_token_size:
            chunks = [text]
        else:
            context_info = f"function/method {func['name']} ({func.get('path', '')}:{func.get('body_location', [0,0])[0]+1})"
            logger.info(f"Text of {context_info} is large ({token_count} tokens), chunking...")
            chunks = self._chunk_text_by_tokens(text, self.iterative_chunk_size, self.iterative_chunk_overlap)
        
        running_summary = ""
        for i, chunk in enumerate(chunks):
            prompt = self.prompt_manager.get_code_analysis_prompt(chunk, i == 0, i == len(chunks) - 1, running_summary)
            running_summary = self.llm_client.generate_summary(prompt)
            if not running_summary:
                logger.error(f"Iterative summarization failed at chunk {i+1}.")
                return ""
        return running_summary

    def _get_token_count(self, text: str) -> int:
        if self.tokenizer:
            safe_text = _sanitize_special_tokens(text)
            return len(self.tokenizer.encode(safe_text))
        return len(text) // 4

    def _chunk_text_by_tokens(self, text: str, chunk_size: int, overlap: int) -> list[str]:
        if not self.tokenizer:
            stride = (chunk_size - overlap) * 4
            return [text[i:i + chunk_size*4] for i in range(0, len(text), stride)]

        safe_text = _sanitize_special_tokens(text)
        tokens = self.tokenizer.encode(safe_text)
        if not tokens: return []

        stride = chunk_size - overlap
        chunks = []
        i = 0
        while True:
            if i + chunk_size >= len(tokens):
                chunks.append(tokens[i:])
                break
            chunks.append(tokens[i:i + chunk_size])
            i += stride
            if i + chunk_size >= len(tokens) and len(tokens) - i < (chunk_size * 0.5):
                 chunks[-1] = tokens[i-stride:]
                 break
        return [self.tokenizer.decode(chunk) for chunk in chunks]


    def _chunk_strings_by_tokens(self, strings: List[str],  chunk_size: int) -> List[str]:
        """
        Groups strings so that each group will contain as many strings as possible without exceeding the token limit.
        Returns a list of joined string groups.
        """
        separator: str = "\n - "

        if not strings:
            return []

        # Pre-tokenize each string
        encoded = []
        for s in strings:
            safe = _sanitize_special_tokens(s)
            tokens = self.tokenizer.encode(safe)
            encoded.append((s, len(tokens)))

        chunks = []
        current_strings = []
        current_token_count = 0

        # Token cost of separator (important!)
        sep_token_cost = len(self.tokenizer.encode(separator))

        for s, n_tokens in encoded:
            # Cost to add this string (include separator if not first)
            additional_cost = n_tokens
            if current_strings:
                additional_cost += sep_token_cost

            # If single string exceeds budget → force it alone
            if n_tokens > chunk_size:
                if current_strings:
                    chunks.append(separator.join(current_strings))
                    current_strings = []
                    current_token_count = 0
                chunks.append(s)
                continue

            # If adding exceeds budget → flush
            if current_token_count + additional_cost > chunk_size:
                chunks.append(separator.join(current_strings))
                current_strings = [s]
                current_token_count = n_tokens
            else:
                current_strings.append(s)
                current_token_count += additional_cost

        if current_strings:
            chunks.append(separator.join(current_strings))

        return chunks
