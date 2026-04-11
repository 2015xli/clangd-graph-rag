#!/usr/bin/env python3
"""
This module provides the NodeSummarizer class, which acts as a stateless
worker for generating RAG summaries.
"""

import os
import logging
import hashlib
import tiktoken
import re
from typing import Optional, Dict, Any, Tuple, List

from .node_cache import SummaryCacheManager
from llm_client import LlmClient
from .prompts import PromptManager, PromptEnv

# Set up logging for this module
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

_SPECIAL_PATTERN = re.compile(r"<\|[^|]+?\|>")

def _sanitize_special_tokens(text: str) -> str:
    """Breaks up special tokens so they are not treated as control tokens."""
    return _SPECIAL_PATTERN.sub(lambda m: f"< |{m.group(0)[2:-2]}| >", text)

class NodeSummarizer:
    """
    A stateless worker for summary generation. It reads from a shared cache
    but does not mutate any shared state.
    """

    def __init__(self, 
                 project_path: str,
                 cache_manager: SummaryCacheManager,
                 llm_client: LlmClient,
                 prompt_manager: PromptManager,
                 project_name: str = "Unknown",
                 project_info: str = "(N/A)",
                 token_encoding: str = 'cl100k_base',
                 max_context_token_size: Optional[int] = None):
        
        self.project_path = project_path
        self.cache_manager = cache_manager
        self.llm_client = llm_client
        self.prompt_manager = prompt_manager
        self.project_name = project_name
        self.project_info = project_info
        self.tokenizer = tiktoken.get_encoding(token_encoding)

        if max_context_token_size:
            self.max_context_token_size = max_context_token_size
            self.iterative_chunk_size = int(0.5 * self.max_context_token_size)
            self.iterative_chunk_overlap = int(0.1 * self.iterative_chunk_size)
        else:
            self.max_context_token_size = None

    def _get_prompt_env(self, node_data: dict) -> PromptEnv:
        """Constructs a PromptEnv instance for the given node."""
        return PromptEnv(
            project_name=self.project_name,
            project_info=self.project_info,
            file_path=node_data.get('path', '(N/A)'),
            node_scope=node_data.get('scope', '(N/A)'),
            node_kind=node_data.get('kind', '(N/A)'),
            node_name=node_data.get('name', '(N/A)')
        )

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

    def get_interface_analysis(self, node_data: dict) -> Tuple[str, dict]:
        """
        Deterministic string generation for function/method interfaces (no body).
        Uses node ID as code_hash for cache invalidation.
        """
        node_id = node_data['id']
        label = node_data['label']
        db_code_hash = node_data.get('db_code_hash')
        db_code_analysis = node_data.get('db_code_analysis')

        # Use ID as hash for automatic invalidation if a body is ever added
        new_code_hash = node_id

        # 1. Staleness Check
        # Path A: DB is up-to-date
        if db_code_hash == new_code_hash and db_code_analysis:
            return "unchanged", {"code_hash": new_code_hash, "code_analysis": db_code_analysis}

        # Path B: DB is stale, check historical cache
        cached_entry = self.cache_manager.get_cache_entry(label, node_id)
        cache_code_analysis = cached_entry.get('code_analysis') if cached_entry else None
        cache_code_hash = cached_entry.get('code_hash') if cached_entry else None
        if cache_code_hash == new_code_hash and cache_code_analysis:
            return "code_analysis_restored", {"code_hash": new_code_hash, "code_analysis": cache_code_analysis}

        # Path C: Cache miss, generate deterministic analysis
        name = node_data.get('name', '')
        signature = node_data.get('signature', '')
        return_type = node_data.get('return_type', '')
        kind = node_data.get('kind', label)
        
        constructed_string = (
            f"This {kind} '{name}' is an interface/declaration that has no code implementation. "
            f"It's declared as: {return_type} {name}{signature}."
        )

        return "code_analysis_regenerated", {"code_hash": new_code_hash, "code_analysis": constructed_string}

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

        # Path C: Must regenerate (either because it's stale, or no valid summary exists anywhere). Let's get the code_analysis for summary generation.
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
            for f in callee_entities
            if (summary := (self.cache_manager.get_cache_entry(f['label'], f['id']) or {}).get('code_analysis'))
        ]

        full_context_text = code_analysis + " ".join(caller_analyses) + " ".join(callee_analyses)
        env = self._get_prompt_env(node_data)
        if self._get_token_count(full_context_text) < self.max_context_token_size:
            prompt = self.prompt_manager.get_contextual_function_prompt(env, code_analysis, caller_analyses, callee_analyses)
            final_summary = self.llm_client.generate_summary(prompt)
        else:
            final_summary = self._summarize_function_context_iteratively(env, code_analysis, caller_analyses, callee_analyses)

        if not final_summary:
            logger.error(f"Failed to generate contextual summary for {node_id}.")
            return "generation_failed", {"summary": db_summary or cache_summary}

        return "summary_regenerated", {"summary": final_summary}

    def get_class_summary(self, node_data: dict, parent_entities: List[dict], method_entities: List[dict], field_entities: List[dict], scc_context: Optional[str] = None) -> Tuple[str, dict]:
        """
        Generates a summary for a class structure using a manifest approach.
        Includes physical hashing for staleness tracking.
        """
        node_id = node_data['id']
        label = node_data['label']
        db_summary = node_data.get('summary')
        db_code_hash = node_data.get('code_hash')

        # 1. Physical Hashing
        start_line, _, end_line, _ = node_data.get('body_location', [0,0,0,0])
        path = node_data.get('path')
        source_code = None
        if path and node_data.get('body_location'):
            source_code = self._get_source_code_for_location(path, start_line, end_line)
        
        if source_code:
            current_code_hash = hashlib.md5(source_code.encode('utf-8')).hexdigest()
        else:
            # For macro-expanded or body-less classes, use ID as hash for invalidation safety
            current_code_hash = node_id

        is_context_stale = any(
            self.cache_manager.get_runtime_status(dep['label'], dep['id']).get('summary_changed')
            for dep in parent_entities + method_entities
        )

        # 2. DB staleness check (Physical + Contextual) 
        is_db_stale = (current_code_hash != db_code_hash) 
        is_stale = is_db_stale or is_context_stale
        if not is_stale and db_summary:
            return "unchanged", {"summary": db_summary, "code_hash": current_code_hash}

        # 3. DB is stale, now checking cache staleness (Physical + Contextual) 
        cached_entry = self.cache_manager.get_cache_entry(label, node_id)
        cache_summary = cached_entry.get('summary') if cached_entry else None
        cache_code_hash = cached_entry.get('code_hash') if cached_entry else None

        is_cache_stale =  (current_code_hash != cache_code_hash)
        is_stale = is_cache_stale or is_context_stale
        if not is_stale and cache_summary:
            return "summary_restored", {"summary": cache_summary, "code_hash": current_code_hash}

        # 4. DB is still, we have to regenerate summary. 
        # 3. Build Template Context
        template_params = node_data.get('template_params')
        spec_args = node_data.get('specialization_args')
        # Priority: 1. Explicit scc_context param (from current run) 
        #           2. Node's own group_analysis (from DB/Cache)
        recursive_context = scc_context or node_data.get('group_analysis')

        template_context_parts = []
        if template_params:
            template_context_parts.append(f"- **Template Parameters**: `{template_params}`")
        if spec_args:
            template_context_parts.append(f"- **Specialization Arguments**: `{spec_args}`")

        if recursive_context:
            template_context_parts.append(f"- **Recursive Context**: This class is part of a recursive structure with the following collective logic: {recursive_context}")

        template_context = "\n".join(template_context_parts)


        # 4.2. Build Definition/Origin Context
        kind = node_data.get('kind', 'Class')
        original_name = node_data.get('original_name')
        is_synthetic = node_data.get('is_synthetic', False)
        
        if original_name:
            definition_context = f"This {kind} is expanded from the macro: `{original_name}`."
        elif is_synthetic:
            definition_context = (
                f"This {kind} is a specialized structural container for `{spec_args or 'implicit types'}`. "
                f"It is implicitly defined due to its member's explicit specialization."
            )
        else:
            if source_code:
                definition_context = f"```cpp\n{source_code}\n```"
            else:
                definition_context = f"Physical definition code for this {kind} could not be retrieved."

        # 4.3. Build Member Summaries
        parent_summaries = [
            summary
            for p in parent_entities
            if (summary := (self.cache_manager.get_cache_entry(p['label'], p['id']) or {}).get('summary'))
        ]
        method_summaries = [
            f"{m['id']}: {summary}" 
            for m in method_entities
            if (summary := (self.cache_manager.get_cache_entry(m['label'], m['id']) or {}).get('summary'))
        ]
        
        field_text = ", ".join([f"{f['type']} {f['name']}" for f in field_entities if f and f.get('name') and f.get('type')])
        
        parent_text = "; ".join(parent_summaries)
        method_text = "; ".join(method_summaries)

        # 4.4. Generate Summary
        env = self._get_prompt_env(node_data)
        prompt = self.prompt_manager.get_class_manifest_summary_prompt(
            env, node_data['name'], kind, template_context, definition_context, 
            parent_text, field_text, method_text
        )
        
        if self._get_token_count(prompt) < self.max_context_token_size:
            final_summary = self.llm_client.generate_summary(prompt)
        else:
            # Fallback to iterative roll-up
            logger.info(f"Context for {kind} '{node_data['name']}' is too large, starting iterative summarization...")
            base_summary = f"The {kind} '{node_data['name']}' {template_context.replace('- **', '').replace('**:', ':')}\nOrigin: {definition_context}"
            inheritance_aware_summary = self._summarize_relations_iteratively(env, base_summary, parent_summaries, "class_has_parents", node_data['name'])
            final_summary = self._summarize_relations_iteratively(env, inheritance_aware_summary, method_summaries, "class_has_methods", node_data['name'])

        if not final_summary:
            logger.error(f"Failed to generate summary for {kind} '{node_data['name']}'.")
            return "generation_failed", {"summary": db_summary or cache_summary, "code_hash": current_code_hash}

        return "summary_regenerated", {"summary": final_summary, "code_hash": current_code_hash}

    def get_scc_group_analysis(self, cluster_metadata: List[dict]) -> Tuple[str, str]:
        """
        Performs collective analysis for a group of recursive classes (SCC).
        Uses a three-stage waterfall (Physical -> DB -> Cache) for validation.
        """
        # --- 1. Physical State & Identity ---
        member_ids = sorted([m['id'] for m in cluster_metadata])
        scc_id = "SCC:" + ",".join(member_ids)
        
        current_member_hashes = []
        bodies = []
        # For SCC, use the first member's context as proxy for the group
        env = self._get_prompt_env(cluster_metadata[0]) if cluster_metadata else None

        for meta in cluster_metadata:
            # Physical read
            start_line, _, end_line, _ = meta.get('body_location', [0,0,0,0])
            path = meta.get('path')
            source_code = self._get_source_code_for_location(path, start_line, end_line) if path else None
            
            name = meta.get('name')
            kind = meta.get('kind')
            if source_code:
                h = hashlib.md5(source_code.encode('utf-8')).hexdigest()
                bodies.append(f"// {kind}: {name}\n{source_code}")
            else:
                h = meta['id'] # Fallback
                bodies.append(f"// {kind}: {name}\n This structure has no body code implementation.")
            
            current_member_hashes.append(h)

        new_group_hash = hashlib.md5(",".join(sorted(current_member_hashes)).encode('utf-8')).hexdigest()

        # --- 2. Database State Check ---
        db_group_analysis = ''
        db_group_hash = None
        db_hashes = [m.get('code_hash', '') for m in cluster_metadata]
        any_none = any(x is None for x in db_hashes)
        if any_none:
            logger.info(f"There are some SCC members missing code_hash in {scc_id}. DB data is stale.")
            db_group_hash = None
        else:
            # retrieve db's group_analysis
            db_group_analysis = cluster_metadata[0].get('group_analysis', '')
            all_equal = [ x.get('group_analysis') == db_group_analysis for x in cluster_metadata]
            if not all_equal: 
                logger.warning(f"There are some SCC members with different group_analysis in {scc_id}. DB data is stale")
                db_group_analysis = ''
            # compute the SCC virtal persistent db group_hash    
            db_hashes = sorted(db_hashes)
            db_group_hash = hashlib.md5(",".join(db_hashes).encode('utf-8')).hexdigest()

        # --- 3. check DB staleness ---
        if new_group_hash == db_group_hash and db_group_analysis:      
            return "unchanged", { 'group_analysis': db_group_analysis, 'group_hash': db_group_hash }

        # --- 4. DB is stale, Cache State Check ---
        cached_entry = self.cache_manager.get_scc_cache_entry(scc_id)
        if cached_entry and cached_entry.get('group_hash') == new_group_hash:
            cache_group_analysis = cached_entry.get('group_analysis')
            return "summary_restored", { 'group_analysis': cache_group_analysis, 'group_hash': new_group_hash }

        # --- 5. Cache is stale too. Regeneration ---
        if not bodies:
            logger.error(f"Failed to generate group_analysis for no-body SCC: {scc_id}")
            return "generation_failed", {'group_analysis': '', 'group_hash': new_group_hash}

        combined_bodies = "\n\n".join(bodies)
        prompt = self.prompt_manager.get_scc_analysis_prompt(env, combined_bodies)
        new_group_analysis = self.llm_client.generate_summary(prompt)

        if new_group_analysis:
            return "summary_regenerated", {'group_analysis': new_group_analysis, 'group_hash': new_group_hash}
        
        logger.error(f"Failed to generate group_analysis for SCC: {scc_id}")
        return "generation_failed", {'group_analysis': '', 'group_hash': new_group_hash}

    def get_namespace_summary(self, node_data: dict, child_inventory: List[dict]) -> Tuple[str, dict]:
        """
        Generates a summary for a namespace using a manifest approach.
        """
        node_id = node_data['id']
        label = node_data['label']
        db_summary = node_data.get('summary')
        db_code_hash = node_data.get('code_hash')

        # 1. Hashing Logic (Membership-based)
        # Create a deterministic identity string for each child
        child_identity_strings = []
        for child in child_inventory:
            # We include ID, Name, and Aliased spelling to detect any logical change
            alias_info = child.get('aliased_canonical_spelling', '')
            child_identity_strings.append(f"{child['id']}:{child['name']}:{alias_info}")

        if child_identity_strings:        
            current_code_hash = hashlib.md5(",".join(sorted(child_identity_strings)).encode('utf-8')).hexdigest()
        else:
            current_code_hash = node_id

        # 2. Staleness Check
        is_context_stale = any(
            self.cache_manager.get_runtime_status(dep['label'], dep['id']).get('summary_changed')
            for dep in child_inventory
        )

        # 3. Waterfall Decision
        # 3.1. DB Check
        is_db_stale = (current_code_hash != db_code_hash)
        is_stale = is_db_stale or is_context_stale
        if not is_stale and db_summary:
            return "unchanged", {"summary": db_summary, "code_hash": current_code_hash}

        # 3.2. Cache Check
        cached_entry = self.cache_manager.get_cache_entry(label, node_id)
        cache_summary = cached_entry.get('summary') if cached_entry else None
        cache_code_hash = cached_entry.get('code_hash') if cached_entry else None
        
        is_cache_stale = (current_code_hash != cache_code_hash)
        is_stale = is_cache_stale or is_context_stale
        if not is_stale and cache_summary:
            return "summary_restored", {"summary": cache_summary, "code_hash": current_code_hash}

        # 4. Generate Summary
        inventory_entries = []
        for child in child_inventory:
            kind = child['label']
            name = child.get('name', 'unnamed')
            
            # Fetch summary from cache (Reduce phase ensured it's available)
            cached_child = self.cache_manager.get_cache_entry(child['label'], child['id'])
            summary = cached_child.get('summary') if cached_child else None
            
            entry = f"- {kind} '{name}'"
            if child.get('aliased_canonical_spelling'):
                entry += f" (alias of '{child['aliased_canonical_spelling']}')"
            if summary:
                entry += f": {summary}"
            
            inventory_entries.append(entry)

        if not inventory_entries:
            logger.debug(f"Cannot generate summary for namespace {node_id}: no children found.")
            return "no_children", {"summary": "This namespace is empty or contains no recognized source files.", "code_hash": current_code_hash}

        inventory_text = "\n".join(inventory_entries)
        env = self._get_prompt_env(node_data)
        prompt = self.prompt_manager.get_namespace_summary_prompt(env, node_data['name'], inventory_text)
        
        if self._get_token_count(prompt) < self.max_context_token_size:
            final_summary = self.llm_client.generate_summary(prompt)
        else:
            logger.info(f"Inventory for namespace '{node_data['name']}' is too large, starting iterative summarization...")
            base_summary = f"The C++ namespace '{node_data['name']}' organizes various logical components."
            final_summary = self._summarize_relations_iteratively(env, base_summary, inventory_entries, "namespace_children", node_data['name'])
        
        if not final_summary:
            logger.error(f"Failed to generate summary for namespace '{node_data['name']}'.")
            return "generation_failed", {"summary": db_summary or cache_summary, "code_hash": current_code_hash}

        return "summary_regenerated", {"summary": final_summary, "code_hash": current_code_hash}

    def get_file_summary(self, node_data: dict, manifest_data: dict) -> Tuple[str, dict]:
        """
        Generates a summary for a file using a manifest approach.
        """
        node_id = node_data['path']
        label = node_data['label']
        db_summary = node_data.get('summary')
        db_code_hash = node_data.get('code_hash')

        symbol_inventory = manifest_data.get('symbol_inventory', [])
        # 1. Staleness Check: 
        # 1.1. A file is stale if ANY of its definitions/declarations have changed.
        is_context_stale = False
        no_symbol = False
        # A file may not have clangd-indexed symbols, because 
        # 1. It only have INCLUDES relationships, which is not indexed symbol.
        # 2. It may have symbol but the id is same as other symbol hence being overriden in clangd index (who only shows one symbol for all symbols of same USR/id).
        for sym in symbol_inventory:
            if not sym['labels']: 
                no_symbol = True
                break
            # We map labels since Cypher returns a list
            sym_label = [l for l in sym['labels'] if l in ["CLASS_STRUCTURE", "DATA_STRUCTURE", "FUNCTION", "VARIABLE", "TYPE_ALIAS", "MACRO", "NAMESPACE"]][0]
            if self.cache_manager.get_runtime_status(sym_label, sym['id']).get('summary_changed'):
                is_context_stale = True
                break

        # 1.2. A file is stale if its content is changed
        # read content from source file 
        file_body = self._get_source_code_for_location(node_data['path'])
        # compute the code_hash of the file
        new_code_hash = hashlib.md5(file_body.encode('utf-8')).hexdigest()

        # 2. If no staleness in DB, just return the original 
        if not is_context_stale and not new_code_hash != db_code_hash and db_summary:
            return "unchanged", {"summary": db_summary, "code_hash": db_code_hash}

        # 3. DB is stale, check if cache matches current physical state
        cached_entry = self.cache_manager.get_cache_entry(label, node_id)
        cache_summary = cached_entry.get('summary') if cached_entry else None
        cache_code_hash = cached_entry.get('code_hash') if cached_entry else None
        
        # Compare against new_code_hash, not db_code_hash
        if not is_context_stale and cache_code_hash == new_code_hash and cache_summary:
            return "summary_restored", {"summary": cache_summary, "code_hash": new_code_hash}

        # 4. Have to Generate new summary
        # 4.1. Get includes
        include_paths = manifest_data.get('include_paths', [])

        # 4.2. Build Manifest Components
        includes_text = ", ".join(include_paths)
        inventory_entries = []
        if not no_symbol:
            for sym in symbol_inventory:
                kind = sym.get('kind', 'Symbol')
                name = sym.get('name', 'unnamed')
                summary = sym.get('summary')
                if summary:
                    inventory_entries.append(f"- {kind} '{name}': {summary}")
                else:
                    inventory_entries.append(f"- {kind} '{name}'")
        
        # 4.3. Add file source code (sanitize and potentially truncate if too large)
        # We cap the raw code at 50% of context size to allow room for symbols
        code_token_limit = self.max_context_token_size // 2
        if self._get_token_count(file_body) > code_token_limit:
            # Truncate or use a snippet if needed, here we just note it's the first part
            code_snippet = self._chunk_text_by_tokens(file_body, code_token_limit, 0)[0]
            inventory_entries.append(f"\n- **Source code (first part)**:\n```cpp\n{code_snippet}\n```")
        else:
            inventory_entries.append(f"\n- **Source code**:\n```cpp\n{file_body}\n```")

        inventory_text = "\n".join(inventory_entries)

        # 5. Generate Summary
        env = self._get_prompt_env(node_data)
        prompt = self.prompt_manager.get_file_manifest_summary_prompt(
            env, node_data['name'], includes_text, inventory_text
        )
        
        if self._get_token_count(prompt) < self.max_context_token_size:
            final_summary = self.llm_client.generate_summary(prompt)
        else:
            # Iterative roll-up for huge files
            logger.info(f"Inventory for file '{node_data['name']}' is too large, starting iterative summarization...")
            base_summary = f"The file '{node_data['name']}' includes [{includes_text}]."
            final_summary = self._summarize_relations_iteratively(env, base_summary, inventory_entries, "file_children", node_data['name'])

        if not final_summary:
            logger.error(f"Failed to generate summary for file '{node_data['name']}'.")
            return "generation_failed", {"summary": db_summary or cache_summary, "code_hash": new_code_hash}

        return "summary_regenerated", {"summary": final_summary, "code_hash": new_code_hash}

    def get_folder_summary(self, node_data: dict, manifest_data: dict) -> Tuple[str, dict]:
        """
        Generates a summary for a folder using a manifest approach.
        """
        node_id = node_data['path']
        label = node_data['label']
        db_summary = node_data.get('summary')
        cached_entry = self.cache_manager.get_cache_entry(label, node_id)
        cache_summary = cached_entry.get('summary') if cached_entry else None

        children_inventory = manifest_data.get('children_inventory', [])

        # Staleness Check
        is_stale = False
        for child in children_inventory:
            child_label = [l for l in child['labels'] if l in ["FILE", "FOLDER"]][0]
            child_key = child.get('path') or child.get('id')
            if self.cache_manager.get_runtime_status(child_label, child_key).get('summary_changed'):
                is_stale = True
                break

        if not is_stale and db_summary:
            return "unchanged", {"summary": db_summary}

        if not is_stale and cache_summary:
            return "summary_restored", {"summary": cache_summary}

        # 1. Build Manifest Components
        inventory_entries = []
        for child in children_inventory:
            kind = [l for l in child['labels'] if l in ["FILE", "FOLDER"]][0]
            name = child.get('name', 'unnamed')
            summary = child.get('summary')
            if summary:
                inventory_entries.append(f"- {kind} '{name}': {summary}")
            else:
                inventory_entries.append(f"- {kind} '{name}'")

        inventory_text = "\n".join(inventory_entries)

        # 2. Generate Summary
        if not inventory_entries:
            return "no_children", {"summary": "This folder is empty or contains no recognized source files."}

        env = self._get_prompt_env(node_data)
        prompt = self.prompt_manager.get_folder_manifest_summary_prompt(
            env, node_data['name'], inventory_text
        )
        
        if self._get_token_count(prompt) < self.max_context_token_size:
            final_summary = self.llm_client.generate_summary(prompt)
        else:
            logger.info(f"Inventory for folder '{node_data['name']}' is too large, starting iterative summarization...")
            base_summary = f"The folder '{node_data['name']}' contains various project components."
            final_summary = self._summarize_relations_iteratively(env, base_summary, inventory_entries, "folder_children", node_data['name'])

        if not final_summary:
            logger.error(f"Failed to generate summary for folder '{node_data['name']}'.")
            return "generation_failed", {"summary": db_summary or cache_summary}

        return "summary_regenerated", {"summary": final_summary}

    def get_project_summary(self, node_data: dict, manifest_data: dict) -> Tuple[str, dict]:
        """
        Generates a summary for the project using a manifest approach.
        """
        node_id = node_data['path']
        label = node_data['label']
        db_summary = node_data.get('summary')
        cached_entry = self.cache_manager.get_cache_entry(label, node_id)
        cache_summary = cached_entry.get('summary') if cached_entry else None

        children_inventory = manifest_data.get('children_inventory', [])

        is_stale = False
        for child in children_inventory:
            child_label = [l for l in child['labels'] if l in ["FILE", "FOLDER"]][0]
            child_key = child.get('path') or child.get('id')
            if self.cache_manager.get_runtime_status(child_label, child_key).get('summary_changed'):
                is_stale = True
                break

        if not is_stale and db_summary:
            return "unchanged", {"summary": db_summary}

        if not is_stale and cache_summary:
            return "summary_restored", {"summary": cache_summary}

        # 1. Build Manifest Components
        inventory_entries = []
        for child in children_inventory:
            kind = [l for l in child['labels'] if l in ["FILE", "FOLDER"]][0]
            name = child.get('name', 'unnamed')
            summary = child.get('summary')
            if summary:
                inventory_entries.append(f"- {kind} '{name}': {summary}")
            else:
                inventory_entries.append(f"- {kind} '{name}'")

        # 2. Generate Summary
        if not inventory_entries:
            return "no_children", {"summary": "This project is empty or contains no recognized source files."}

        env = self._get_prompt_env(node_data)
        summaries_text = "; ".join(inventory_entries)
        if self._get_token_count(summaries_text) < self.max_context_token_size:
            prompt = self.prompt_manager.get_project_summary_prompt(env, summaries_text)
            final_summary = self.llm_client.generate_summary(prompt)
        else:
            logger.info(f"Inventory for project is too large, starting iterative summarization...")
            base_summary = "This software project contains various top-level components."
            final_summary = self._summarize_relations_iteratively(env, base_summary, inventory_entries, "project_children", "Project")

        if not final_summary:
            logger.error(f"Failed to generate summary for project.")
            return "generation_failed", {"summary": db_summary or cache_summary}

        return "summary_regenerated", {"summary": final_summary}

    def _summarize_function_context_iteratively(self, env: PromptEnv, code_analysis: str, caller_analyses: List[str], callee_analyses: List[str]) -> str:
        """Generates a contextual summary by iteratively processing batches of caller and callee summaries."""
        logger.info(f"Context for function is too large, starting iterative contextual summarization...")
        caller_aware_summary = self._summarize_relations_iteratively(env, code_analysis, caller_analyses, "function_has_callers")
        final_summary = self._summarize_relations_iteratively(env, caller_aware_summary, callee_analyses, "function_has_callees")
        return final_summary

    def _summarize_relations_iteratively(self, env: PromptEnv, summary: str, relation_summaries: List[str], relation_name: str, entity_name: Optional[str] = None) -> str:
        """Generic helper to iteratively fold a list of relation summaries into a base summary."""
        if not relation_summaries:
            return summary

        relation_chunks = self._chunk_strings_by_tokens(relation_summaries, self.iterative_chunk_size)
        running_summary = summary
        for i, chunk in enumerate(relation_chunks):
            prompt = self.prompt_manager.get_iterative_relation_prompt(env, relation_name, running_summary, chunk, entity_name)
            running_summary = self.llm_client.generate_summary(prompt)
            if not running_summary:
                logger.error(f"Iterative relation summarization failed at chunk {i+1}.")
                return summary
        return running_summary

    def _get_source_code_for_location(self, file_path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
        if not file_path or not self.project_path: return ""
        full_path = os.path.join(self.project_path, file_path)

        if not os.path.exists(full_path):
            logger.error(f"File not found when trying to extract source: {full_path}")
            return ""
        
        try:
            with open(full_path, 'r', errors='ignore') as f:
                lines = f.readlines()
            if end_line: end_line += 1
            code_lines = lines[start_line : end_line]
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
        env = self._get_prompt_env(func)
        for i, chunk in enumerate(chunks):
            prompt = self.prompt_manager.get_code_analysis_prompt(env, chunk, i == 0, i == len(chunks) - 1, running_summary)
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
