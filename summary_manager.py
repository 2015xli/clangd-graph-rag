#!/usr/bin/env python3
"""
This module provides the SummaryManager class, which manages an in-memory cache
for RAG summaries, handles persistence, and orchestrates summary generation.

It also includes a command-line tool for populating the cache from Neo4j (backup)
and writing the cache back to Neo4j (restore).
"""

import os
import logging
import argparse
import json
import sys
import hashlib
import tiktoken
import re
from typing import Optional, Dict, Any, List, Tuple
from collections import defaultdict

# Add the parent directory to the path to allow imports from other modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from neo4j_manager import Neo4jManager
from log_manager import init_logging
from llm_client import get_llm_client
from rag_generation_prompts import RagGenerationPromptManager

init_logging()
logger = logging.getLogger(__name__)

_SPECIAL_PATTERN = re.compile(r"<\|[^|]+?\|>")

def _sanitize_special_tokens(text: str) -> str:
    """Break up special tokens so the model won't treat them as control tokens."""
    return _SPECIAL_PATTERN.sub(lambda m: f"< |{m.group(0)[2:-2]}| >", text)

class SummaryManager:
    """
    Manages summary caching, persistence, and generation logic.
    """

    DEFAULT_CACHE_FILENAME = "summary_backup.json"

    def __init__(self, project_path: Optional[str] = None, 
                 llm_api: str = 'fake', 
                 token_encoding: str = 'cl100k_base', # Revert to default value
                 max_context_token_size: Optional[int] = None):
        """
        Initializes the SummaryManager.
        """
        self.cache_data: Dict[str, Dict[str, Any]] = defaultdict(dict)
        self.cache_status: Dict[str, Dict[str, Any]] = defaultdict(dict)
        
        self.project_path = project_path
        self.cache_path: Optional[str] = None
        if project_path:
            self.cache_path = os.path.join(self.project_path, ".cache", self.DEFAULT_CACHE_FILENAME)
        
        self.llm_client = get_llm_client(llm_api)
        self.prompt_manager = RagGenerationPromptManager()
        self.tokenizer = tiktoken.get_encoding(token_encoding)
        self.llm_api = llm_api # Store for potential internal use or logging

        if max_context_token_size:
            self.max_context_token_size = max_context_token_size
            self.iterative_chunk_size = int(0.5 * self.max_context_token_size)
            self.iterative_chunk_overlap = int(0.1 * self.iterative_chunk_size)
        else:
            self.max_context_token_size = None

        logger.info("SummaryManager initialized.")

    @property
    def is_local_llm(self) -> bool:
        return self.llm_client.is_local

    def _init_cache_status(self, cache: Dict[str, Dict[str, Any]]):
        """Initializes the runtime cache status for all entries in the cache."""
        self.cache_status = {
            label: {
                entry_key: {'entry_is_visited': False, 'code_is_same': False, 'summary_is_same': False} 
                for entry_key in entries.keys()
            } 
            for label, entries in cache.items()
        }
        logger.info(f"Initialized cache status for {sum(len(v) for v in self.cache_status.values())} entries.")

    def _code_summary_status_update(self, entity: dict, code_is_same: bool):
        """Updates the runtime cache status for a specific entry."""
        label = entity.get('label')
        entity_id = entity.get('id')
        if not label or not entity_id:
            return

        if label not in self.cache_status:
            self.cache_status[label] = {}
        if entity_id not in self.cache_status[label]:
            self.cache_status[label][entity_id] = {'entry_is_visited': True, 'code_is_same': code_is_same}
        else:
            self.cache_status[label][entity_id]['entry_is_visited'] = True
            self.cache_status[label][entity_id]['code_is_same'] = code_is_same

    def _final_summary_status_update(self, entity: dict, summary_is_same: bool):
        """Updates the runtime cache status for the final summary."""
        label = entity.get('label')
        entity_key = entity.get('path') if label in ['FILE', 'FOLDER', 'PROJECT'] else entity.get('id')
        if not label or not entity_key:
            return

        if label not in self.cache_status:
            self.cache_status[label] = {}
        if entity_key not in self.cache_status[label]:
            self.cache_status[label][entity_key] = {'entry_is_visited': True, 'summary_is_same': summary_is_same}
        else:
            # Note: is_visited for the entity should have been set during code summary pass
            self.cache_status[label][entity_key]['summary_is_same'] = summary_is_same

    def get_code_summary(self, entity: dict, source_code: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Gets a valid code summary for a function/method entity.
        """
        entity_id = entity['id']
        label = entity['label']
        db_code_hash = entity.get('db_code_hash')
        db_code_summary = entity.get('db_codeSummary')
        
        new_code_hash = hashlib.md5(source_code.encode('utf-8')).hexdigest()

        if db_code_hash == new_code_hash and db_code_summary:
            self._code_summary_status_update(entity, code_is_same=True)
            self.set_cache_entry(label, entity_id, {'code_hash': new_code_hash, 'codeSummary': db_code_summary})
            return (None, None)

        cached_data = self.get_cache_entry(label, entity_id)
        if cached_data and cached_data.get('code_hash') == new_code_hash:
            self._code_summary_status_update(entity, code_is_same=True)
            return (new_code_hash, cached_data.get('codeSummary'))

        self._code_summary_status_update(entity, code_is_same=False)
        if not self.llm_client:
            raise ValueError("LLM client must be configured for summary generation.")
        
        new_code_summary = self._summarize_function_text_iteratively(source_code, entity)
        if not new_code_summary:
            logging.error(f"Failed to generate code summary for {entity_id}.")
            return (None, None)

        self.set_cache_entry(label, entity_id, {'code_hash': new_code_hash, 'codeSummary': new_code_summary})
        return (new_code_hash, new_code_summary)

    def get_function_contextual_summary(self, entity: dict, callers: List[dict], callees: List[dict]) -> Optional[str]:
        """
        Gets a valid contextual summary for a function/method entity.
        This is the new public interface that encapsulates all caching and generation logic.
        Returns a new summary string if one was generated or retrieved from cache,
        otherwise returns None (indicating the DB version is up-to-date).
        """
        entity_id = entity['id']
        label = entity['label']
        db_summary = entity.get('db_summary')

        # Step 1: Check for staleness based on dependencies' code summaries.
        is_stale = any(self.is_code_changed(dep) for dep in [entity] + callers + callees)

        # Step 2: If not stale and DB has a summary, we are done.
        if not is_stale and db_summary:
            self.set_cache_entry(label, entity_id, {'summary': db_summary})
            self._final_summary_status_update(entity, summary_is_same=True)
            return None # Nothing to update in the DB

        # Step 3: If not stale, try to find a valid summary in the cache.
        if not is_stale:
            cached_data = self.get_cache_entry(label, entity_id)
            if cached_data and 'summary' in cached_data:
                self._final_summary_status_update(entity, summary_is_same=True)
                # Return the cached summary so the caller can update the DB
                return cached_data['summary']

        # Step 4: If we are here, we must regenerate. Mark as a "miss".
        self._final_summary_status_update(entity, summary_is_same=False)
        
        # Step 5: Gather necessary context summaries from the cache.
        code_summary_data = self.get_cache_entry(label, entity_id)
        if not code_summary_data or 'codeSummary' not in code_summary_data:
            logging.warning(f"Cannot generate contextual summary for {entity_id}: missing codeSummary in cache.")
            return None
        code_summary = code_summary_data['codeSummary']

        caller_summaries = [self.get_cache_entry(c['label'], c['id']).get('codeSummary', '') for c in callers if self.get_cache_entry(c['label'], c['id'])]
        callee_summaries = [self.get_cache_entry(c['label'], c['id']).get('codeSummary', '') for c in callees if self.get_cache_entry(c['label'], c['id'])]
        
        # Step 6: Call the internal method to generate the new summary.
        final_summary = self._generate_contextual_summary(
            entity, code_summary, caller_summaries, callee_summaries
        )
        
        if not final_summary: 
            logging.error(f"Failed to generate contextual summary for {entity_id}.")
            return None

        # Update the cache with the newly generated summary.
        self.set_cache_entry(label, entity_id, {'summary': final_summary})
        
        # Step 7: Return the newly generated summary for DB update.
        return final_summary

    def is_code_changed(self, entity: dict) -> bool:
        """
        Checks if an entity's code summary was regenerated in this run (a "miss").
        """
        label = entity.get('label')
        entity_id = entity.get('id')
        if not label or not entity_id:
            logger.error(f"Error: Cannot find entity {entity} when looking for its code cache status. ")
            return True # This should never happen.

        status = self.cache_status.get(label, {}).get(entity_id)
        if not status or not status.get('entry_is_visited'):
            logger.error(f"Error: Cannot determine cache status for entity {entity}.")
            return True  # If it has no status, it's new/unprocessed, so it's considered changed
        
        return not status.get('code_is_same')

    def is_summary_changed(self, entity: dict) -> bool:
        """
        Checks if an entity's final summary was regenerated in this run.
        """
        label = entity.get('label')
        entity_key = entity.get('path') if label in ['FILE', 'FOLDER', 'PROJECT'] else entity.get('id')
        if not label or not entity_key:
            logger.error(f"Error: Cannot find entity {entity} when looking for its summary cache status. ")
            return True

        status = self.cache_status.get(label, {}).get(entity_key)
        if not status or not status.get('entry_is_visited'):
            logger.error(f"Error: Cannot determine cache summary status for entity {entity}.")
            return True # If it has no status, it's new/unprocessed, so it's changed.
        
        return not status.get('summary_is_same')

    def _generate_contextual_summary(self, entity: dict, code_summary: str, caller_summaries: List[str], callee_summaries: List[str]) -> Optional[str]:
        """
        Generates a new contextual summary for a function.
        This method is called by the RagGenerator only when a new summary is required.
        """
        label = entity['label']
        entity_id = entity['id']
        logging.info(f"Generating new contextual summary for {label} {entity_id}.")
        
        full_context_text = code_summary + " ".join(caller_summaries) + " ".join(callee_summaries)
        if self._get_token_count(full_context_text) < self.max_context_token_size:
            prompt = self._build_function_contextual_prompt(code_summary, caller_summaries, callee_summaries)
            final_summary = self.llm_client.generate_summary(prompt)
        else:
            final_summary = self._summarize_function_context_iteratively(code_summary, caller_summaries, callee_summaries)

        return final_summary
        
    def _build_function_contextual_prompt(self, code_summary, caller_summaries, callee_summaries) -> str:
        caller_text = ", ".join([s for s in caller_summaries if s]) or "none"
        callee_text = ", ".join([s for s in callee_summaries if s]) or "none"
        return self.prompt_manager.get_contextual_function_prompt(code_summary, caller_text, callee_text)

    def _summarize_function_context_iteratively(self, code_summary: str, caller_summaries: List[str], callee_summaries: List[str]) -> str:
        """Generates a contextual summary by iteratively processing batches of caller and callee summaries."""
        logging.info(f"Context for function is too large, starting iterative contextual summarization...")
        caller_aware_summary = self._summarize_relations_iteratively(code_summary, caller_summaries, "function_has_callers")
        final_summary = self._summarize_relations_iteratively(caller_aware_summary, callee_summaries, "function_has_callees")
        return final_summary

    def _summarize_relations_iteratively(self, summary: str, relation_summaries: List[str], relation_name: str, context_name: Optional[str] = None) -> str:
        """Generic helper to iteratively fold a list of relation summaries into a base summary."""
        if not relation_summaries:
            return summary
        relations_text = "\n - ".join(relation_summaries)
        relation_chunks = self._chunk_text_by_tokens(relations_text, self.iterative_chunk_size, self.iterative_chunk_overlap)
        running_summary = summary
        for i, chunk in enumerate(relation_chunks):
            prompt = self.prompt_manager.get_iterative_relation_prompt(relation_name, running_summary, chunk, context_name)
            running_summary = self.llm_client.generate_summary(prompt)
            if not running_summary:
                logger.error(f"Iterative relation summarization failed at chunk {i+1}.")
                return summary

        return running_summary

    def _generate_class_summary(self, class_entity: dict, parent_summaries: List[str], method_summaries: List[str], fields: List[dict]) -> Optional[str]:
        """
        Generates a new summary for a class structure.
        """
        class_id = class_entity['id']
        class_name = class_entity['name']
        label = class_entity['label']
        #logging.info(f"Generating new summary for {label} {class_name} ({class_id}).")
        field_text = ", ".join([f"{f['type']} {f['name']}" for f in fields if f and f.get('name') and f.get('type')])
        full_context_text = " ".join(parent_summaries) + " ".join(method_summaries) + field_text
        if self._get_token_count(full_context_text) < self.max_context_token_size:
            prompt = self._build_class_summary_prompt(class_name, parent_summaries, method_summaries, field_text)
            final_summary = self.llm_client.generate_summary(prompt)
        else:
            logging.info(f"Context for class '{class_name}' is too large, starting iterative summarization...")
            base_summary = f"The class '{class_name}' has data members: [{field_text}]."
            inheritance_aware_summary = self._summarize_relations_iteratively(base_summary, parent_summaries, "class_has_parents", class_name)
            final_summary = self._summarize_relations_iteratively(inheritance_aware_summary, method_summaries, "class_has_methods", class_name)

        return final_summary

    def get_class_summary(self, class_entity: dict, parent_entities: List[dict], method_entities: List[dict], fields: List[dict]) -> Optional[str]:
        """
        Gets a valid summary for a class structure, handling caching and staleness checks.
        """
        class_id = class_entity['id']
        label = class_entity['label']
        db_summary = class_entity.get('db_summary')

        # Step 1: Check for staleness based on dependencies' final summaries.
        is_stale = any(self.is_summary_changed(dep) for dep in parent_entities + method_entities)

        # Step 2: If not stale and DB has a summary, we are done.
        if not is_stale and db_summary:
            self.set_cache_entry(label, class_id, {'summary': db_summary})
            self._final_summary_status_update(class_entity, summary_is_same=True)
            return None

        # Step 3: If not stale, check cache for a valid summary.
        if not is_stale:
            cached_data = self.get_cache_entry(label, class_id)
            if cached_data and 'summary' in cached_data:
                self._final_summary_status_update(class_entity, summary_is_same=True)
                return cached_data['summary']

        # Step 4: Must regenerate. Mark as a "miss".
        self._final_summary_status_update(class_entity, summary_is_same=False)

        # Step 5: Gather context from cache.
        parent_summaries = [self.get_cache_entry(p['label'], p['id']).get('summary', '') for p in parent_entities if self.get_cache_entry(p['label'], p['id'])]
        method_summaries = [self.get_cache_entry(m['label'], m['id']).get('summary', '') for m in method_entities if self.get_cache_entry(m['label'], m['id'])]

        # Step 6: Generate the new summary.
        final_summary = self._generate_class_summary(
            class_entity, parent_summaries, method_summaries, fields
        )

        if not final_summary:
            logging.error(f"Failed to generate summary for class '{class_entity['name']}'.")
            return None

        self.set_cache_entry(label, class_id, {'summary': final_summary})

        return final_summary

    def get_namespace_summary(self, namespace_entity: dict, child_entities: List[dict]) -> Optional[str]:
        """
        Gets a valid summary for a namespace, handling caching and staleness checks.
        """
        ns_id = namespace_entity['id']
        label = namespace_entity['label']
        db_summary = namespace_entity.get('db_summary')

        is_stale = any(self.is_summary_changed(dep) for dep in child_entities)

        if not is_stale and db_summary:
            self.set_cache_entry(label, ns_id, {'summary': db_summary})
            self._final_summary_status_update(namespace_entity, summary_is_same=True)
            return None

        if not is_stale:
            cached_data = self.get_cache_entry(label, ns_id)
            if cached_data and 'summary' in cached_data:
                self._final_summary_status_update(namespace_entity, summary_is_same=True)
                return cached_data['summary']

        self._final_summary_status_update(namespace_entity, summary_is_same=False)

        child_summaries = [self.get_cache_entry(c['label'], c['id']).get('summary', '') for c in child_entities if self.get_cache_entry(c['label'], c['id'])]
        if not child_summaries:
            return None

        child_summaries_text = [f"The {c['label'].lower()} '{c['name']}' is responsible for: {s}" for c, s in zip(child_entities, child_summaries) if s]
        
        final_summary = self._generate_hierarchical_summary(
            context_name=namespace_entity['name'],
            relation_name="namespace_children",
            relation_summaries=child_summaries_text
        )
        
        if not final_summary:
            logging.error(f"Failed to generate summary for namespace '{namespace_entity['name']}'.")
            return None

        self.set_cache_entry(label, ns_id, {'summary': final_summary})
        
        return final_summary

    def get_file_summary(self, file_entity: dict, child_entities: List[dict]) -> Optional[str]:
        """
        Gets a valid summary for a file, handling caching and staleness checks.
        """
        file_path = file_entity['path']
        label = file_entity['label']
        db_summary = file_entity.get('db_summary')

        is_stale = any(self.is_summary_changed(dep) for dep in child_entities)

        if not is_stale and db_summary:
            self.set_cache_entry(label, file_path, {'summary': db_summary})
            self._final_summary_status_update(file_entity, summary_is_same=True)
            return None

        if not is_stale:
            cached_data = self.get_cache_entry(label, file_path)
            if cached_data and 'summary' in cached_data:
                self._final_summary_status_update(file_entity, summary_is_same=True)
                return cached_data['summary']

        self._final_summary_status_update(file_entity, summary_is_same=False)

        child_summaries = [self.get_cache_entry(c['label'], c['id']).get('summary', '') for c in child_entities if self.get_cache_entry(c['label'], c['id'])]
        if not child_summaries:
            return None

        final_summary = self._generate_hierarchical_summary(
            context_name=os.path.basename(file_path),
            relation_name="file_children",
            relation_summaries=child_summaries
        )
        
        if not final_summary:
            logging.error(f"Failed to generate summary for file '{file_entity['name']}'.")
            return None

        self.set_cache_entry(label, file_path, {'summary': final_summary})
        
        return final_summary

    def get_folder_summary(self, folder_entity: dict, child_entities: List[dict]) -> Optional[str]:
        """
        Gets a valid summary for a folder, handling caching and staleness checks.
        """
        folder_path = folder_entity['path']
        label = folder_entity['label']
        db_summary = folder_entity.get('db_summary')

        is_stale = any(self.is_summary_changed(dep) for dep in child_entities)

        if not is_stale and db_summary:
            self.set_cache_entry(label, folder_path, {'summary': db_summary})
            self._final_summary_status_update(folder_entity, summary_is_same=True)
            return None

        if not is_stale:
            cached_data = self.get_cache_entry(label, folder_path)
            if cached_data and 'summary' in cached_data:
                self._final_summary_status_update(folder_entity, summary_is_same=True)
                return cached_data['summary']

        self._final_summary_status_update(folder_entity, summary_is_same=False)

        child_summaries = [self.get_cache_entry(c['label'], c.get('id') or c.get('path')).get('summary', '') for c in child_entities if self.get_cache_entry(c['label'], c.get('id') or c.get('path'))]
        if not child_summaries:
            return None
        
        child_summaries_text = [f"The {c['label'].lower()} '{c['name']}' is responsible for: {s}" for c, s in zip(child_entities, child_summaries) if s]

        final_summary = self._generate_hierarchical_summary(
            context_name=folder_entity['name'],
            relation_name="folder_children",
            relation_summaries=child_summaries_text
        )
        
        if not final_summary:
            logging.error(f"Failed to generate summary for folder '{folder_entity['name']}'.")
            return None

        self.set_cache_entry(label, folder_path, {'summary': final_summary})
        
        return final_summary

    def get_project_summary(self, project_entity: dict, child_entities: List[dict]) -> Optional[str]:
        """
        Gets a valid summary for the project, handling caching and staleness checks.
        """
        project_path = project_entity['path']
        label = project_entity['label']
        db_summary = project_entity.get('db_summary')

        is_stale = any(self.is_summary_changed(dep) for dep in child_entities)

        if not is_stale and db_summary:
            self.set_cache_entry(label, project_path, {'summary': db_summary})
            self._final_summary_status_update(project_entity, summary_is_same=True)
            return None

        if not is_stale:
            cached_data = self.get_cache_entry(label, project_path)
            if cached_data and 'summary' in cached_data:
                self._final_summary_status_update(project_entity, summary_is_same=True)
                return cached_data['summary']

        self._final_summary_status_update(project_entity, summary_is_same=False)

        child_summaries = [self.get_cache_entry(c['label'], c.get('id') or c.get('path')).get('summary', '') for c in child_entities if self.get_cache_entry(c['label'], c.get('id') or c.get('path'))]
        if not child_summaries:
            return None
            
        child_summaries_text = [f"The {c['label'].lower()} '{c['name']}' is responsible for: {s}" for c, s in zip(child_entities, child_summaries) if s]

        final_summary = self._generate_hierarchical_summary(
            context_name=project_entity['name'],
            relation_name="project_children",
            relation_summaries=child_summaries_text
        )
        
        if not final_summary:
            logging.error(f"Failed to generate summary for project '{project_entity['name']}'.")
            return None

        self.set_cache_entry(label, project_path, {'summary': final_summary})
        
        return final_summary
        
    def _build_class_summary_prompt(self, class_name: str, parent_summaries: list[str], method_summaries: list[str], field_text: str) -> str:
        """Constructs the prompt for summarizing a class."""
        parent_text = f"It inherits from classes with these roles: [{'; '.join(parent_summaries)}]." if parent_summaries else ""
        field_text_prompt = f"It has the following data members: [{field_text}]." if field_text else ""
        method_text = f"It has methods that perform these functions: [{'; '.join(method_summaries)}]." if method_summaries else ""
        return self.prompt_manager.get_class_summary_prompt(class_name, parent_text, field_text_prompt, method_text)

    def _get_token_count(self, text: str) -> int:
        if self.tokenizer:
            safe_text = _sanitize_special_tokens(text)
            return len(self.tokenizer.encode(safe_text))
        return len(text) // 4

    def _chunk_text_by_tokens(self, text: str, chunk_size: int, overlap: int) -> List[str]:
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

    def _generate_hierarchical_summary(self, context_name: str, relation_name: str, relation_summaries: List[str]) -> Optional[str]:
        """
        Generic helper to generate a summary for a hierarchical node (namespace, file, folder).
        """
        if not relation_summaries:
            return None

        summaries_text = "; ".join(relation_summaries)
        
        if self._get_token_count(summaries_text) < self.max_context_token_size:
            if relation_name == "namespace_children":
                prompt = self.prompt_manager.get_namespace_summary_prompt(context_name, summaries_text)
            elif relation_name == "file_children":
                prompt = self.prompt_manager.get_file_summary_prompt(context_name, summaries_text)
            elif relation_name == "folder_children":
                prompt = self.prompt_manager.get_folder_summary_prompt(context_name, summaries_text)
            elif relation_name == "project_children": # project
                prompt = self.prompt_manager.get_project_summary_prompt(summaries_text)
            else:
                raise ValueError(f"Unknown relation name: {relation_name}")
            return self.llm_client.generate_summary(prompt)
        else:
            # Fallback to iterative summarization for large contexts
            logging.info(f"Context for {relation_name} '{context_name}' is too large, starting iterative summarization...")
            
            base_summary = f"The {relation_name.split('_')[0]} '{context_name}' contains various components."
            return self._summarize_relations_iteratively(base_summary, relation_summaries, relation_name, context_name)

    def _summarize_function_text_iteratively(self, text: str, func: dict) -> str:
        token_count = self._get_token_count(text)
        if token_count <= self.max_context_token_size:
            chunks = [text]
        else:
            context_info = f"function/method {func['name']} ({func.get('path', '')}:{func.get('body_location', [0,0])[0]+1})"
            logging.info(f"Text of {context_info} is large ({token_count} tokens), chunking...")
            chunks = self._chunk_text_by_tokens(text, self.iterative_chunk_size, self.iterative_chunk_overlap)
        
        running_summary = ""
        for i, chunk in enumerate(chunks):
            prompt = self.prompt_manager.get_code_summary_prompt(chunk, i == 0, i == len(chunks) - 1, running_summary)
            running_summary = self.llm_client.generate_summary(prompt)
            if not running_summary:
                logger.error(f"Iterative summarization failed at chunk {i+1}.")
                return ""
        return running_summary

    def configure_from_graph(self, neo4j_mgr: Neo4jManager):
        if self.project_path: return
        project_path = self._get_project_path_from_graph(neo4j_mgr)
        if not project_path:
            raise ValueError("Could not determine project path from Neo4j.")
        self.project_path = project_path
        self.cache_path = os.path.join(self.project_path, ".cache", self.DEFAULT_CACHE_FILENAME)
        logger.info(f"Discovered and configured project path: {self.project_path}")

    def _get_project_path_from_graph(self, neo4j_mgr: Neo4jManager) -> Optional[str]:
        query = "MATCH (p:PROJECT) RETURN p.path AS path"
        result = neo4j_mgr.execute_read_query(query)
        return result[0].get('path') if result and len(result) == 1 else None

    def get_cache_entry(self, label: str, identifier: str) -> Optional[Dict[str, Any]]:
        return self.cache_data.get(label, {}).get(identifier)

    def set_cache_entry(self, label: str, identifier: str, data: Dict[str, Any]):
        if label not in self.cache_data:
            self.cache_data[label] = {}
        # Merge data to preserve other keys like 'summary'
        if identifier in self.cache_data[label]:
            self.cache_data[label][identifier].update(data)
        else:
            self.cache_data[label][identifier] = data

    def _load_summary_cache_file(self, cache_path: Optional[str] = None) -> bool:
        path = cache_path or self.cache_path
        if not path:
            logger.error("Cannot load summary cache: cache path is not configured.")
            return False
        if not os.path.exists(path):
            logger.warning(f"Summary cache file not found at {path}. Starting with empty cache.")
            self.cache_data = defaultdict(dict)
            self._init_cache_status(self.cache_data)
            return False
        try:
            with open(path, 'r') as f:
                loaded_data = json.load(f)
                self.cache_data = defaultdict(dict, {k: dict(v) for k, v in loaded_data.items()})

            total_cache_entries = sum(len(v) for v in self.cache_data.values())
            logger.info(f"Loaded {total_cache_entries} entries from summary cache file {path}.")
            self._init_cache_status(self.cache_data)
            return True
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Failed to read or parse summary cache file {path}: {e}")
            self.cache_data = defaultdict(dict)
            self._init_cache_status(self.cache_data)
            return False

    def _save_summary_cache_file(self, cache_path: Optional[str] = None):
        path = cache_path or self.cache_path
        if not path:
            logger.error("Cannot save summary cache: cache path is not configured.")
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                json.dump(self.cache_data, f, indent=2)
            total_entries = sum(len(v) for v in self.cache_data.values())
            logger.info(f"Successfully wrote {total_entries} entries to summary cache file {path}.")
        except IOError as e:
            logger.error(f"Failed to write to summary cache file {path}: {e}")

    def prepare(self):
        # prepare cache file
        self._load_summary_cache_file()


    def finalize(self, neo4j_mgr: Neo4jManager, mode: str):
        """
        Finalizes the cache by performing bloat checks, conditional pruning,
        and saving to file.
        mode: "builder" or "updater"
        """
        logger.info(f"Finalizing cache in {mode} mode...")

        # 1. Conditional Pruning (for builder mode)
        if mode == "builder":
            logger.info("Pruning dormant entries from cache for builder mode...")
            pruned_cache_data = defaultdict(dict)
            for label, entries in self.cache_data.items():
                for entity_id, data in entries.items():
                    status = self.cache_status.get(label, {}).get(entity_id)
                    if status and status.get('entry_is_visited'):
                        pruned_cache_data[label][entity_id] = data
            self.cache_data = pruned_cache_data
            logger.info(f"Cache pruned. New total entries: {sum(len(v) for v in self.cache_data.values())}.")
        elif mode == "updater":
            logger.info("Retaining all cache entries for updater mode (no pruning of dormant entries).")
        else:
            logger.error(f"Unknown cache finalization mode: {mode}. No specific pruning applied.")

        # 2. Cache Bloat Check and Full Sync
        # Threshold: If cache entries are more than 1.2 times the actual graph nodes, trigger full sync
        CACHE_BLOAT_THRESHOLD = 1.2 
        total_graph_nodes = neo4j_mgr.total_nodes_in_graph()
        total_cache_entries = sum(len(v) for v in self.cache_data.values())

        if total_graph_nodes > 0 and total_cache_entries > total_graph_nodes * CACHE_BLOAT_THRESHOLD:
            logger.warning(f"Cache bloat detected! Cache entries ({total_cache_entries}) "
                           f"exceed graph nodes ({total_graph_nodes}) by more than {CACHE_BLOAT_THRESHOLD}x. "
                           f"Triggering full cache backup sync.")
            self.backup_cache_file_from_neo4j(neo4j_mgr, self.cache_path) # This rebuilds self.cache_data and self.cache_status
        else:
            self._save_summary_cache_file()
            logger.info(f"Cache size ({total_cache_entries}) is within limits compared to graph nodes ({total_graph_nodes}).")

        logger.info("Cache finalization complete.")

    def backup_cache_file_from_neo4j(self, neo4j_mgr: Neo4jManager, cache_file_path: str, batch_size: int = 10000):
        logger.info("Populating summary cache from Neo4j...")
        """
        Connects to Neo4j, reads all summaries and code hashes in batches,
        and writes them to the cache file in the new nested format.
        """
        logger.info("Starting summary backup from Neo4j to file...")
        
        final_cache_data = defaultdict(dict)

        # Define categories of nodes to query
        query_configs = {
            "id_based_full": {
                "labels": ["FUNCTION", "METHOD"],
                "query": """
                    MATCH (n:{label})
                    WHERE n.summary IS NOT NULL OR n.codeSummary IS NOT NULL
                    RETURN n.id AS identifier, n.codeSummary AS codeSummary, n.summary AS summary
                    ORDER BY n.id SKIP $skip LIMIT $limit
                """
            },
            "id_based_simple": {
                "labels": ["NAMESPACE", "CLASS_STRUCTURE", "DATA_STRUCTURE"],
                "query": """
                    MATCH (n:{label})
                    WHERE n.summary IS NOT NULL
                    RETURN n.id AS identifier, n.summary AS summary
                    ORDER BY n.id SKIP $skip LIMIT $limit
                """
            },
            "path_based": {
                "labels": ["FILE", "FOLDER", "PROJECT"],
                "query": """
                    MATCH (n:{label})
                    WHERE n.summary IS NOT NULL
                    RETURN n.path AS identifier, n.summary AS summary
                    ORDER BY n.path SKIP $skip LIMIT $limit
                """
            }
        }

        for config_name, config in query_configs.items():
            for label in config["labels"]:
                logger.info(f"Backing up summaries for label: {label}...")
                
                skip = 0
                while True:
                    formatted_query = config["query"].format(label=label)
                    results = neo4j_mgr.execute_read_query(formatted_query, {"skip": skip, "limit": batch_size})
                    
                    if not results:
                        break
                    
                    for record in results:
                        identifier = record.get('identifier')
                        if not identifier: continue

                        entry = {k: v for k, v in record.items() if k != 'identifier' and v is not None}
                        final_cache_data[label][identifier] = entry
                    
                    logger.info(f"  Fetched {len(final_cache_data[label])} records for {label} so far...")
                    skip += batch_size

        self.cache_data = final_cache_data
        self._save_summary_cache_file(cache_file_path)
        logger.info("Backup summary from neo4j to cache file complete.")

    def restore_cache_file_to_neo4j(self, neo4j_mgr: Neo4jManager, cache_file_path: str):
        """
        Restores summaries from the cache file (in the new nested format) to the Neo4j graph.
        """
        logger.info("Restoring summaries from cache file to Neo4j...")
        self._load_summary_cache_file(cache_file_path)
        if not self.cache_data:
            logger.info("Cache is empty. Nothing to restore.")
            return

        for label, entries in self.cache_data.items():
            if not entries:
                continue
            
            logger.info(f"Restoring {len(entries)} summaries for label: {label}...")
            
            data_list = []
            identifier_key = "path" if label in ["FILE", "FOLDER", "PROJECT"] else "id"

            for identifier, properties in entries.items():
                item = {identifier_key: identifier, **properties}
                data_list.append(item)

            if not data_list: continue

            sample_props = data_list[0]
            set_clauses = [f"n.{prop} = d.{prop}" for prop in sample_props if prop != identifier_key]
            
            if not set_clauses:
                logger.warning(f"No properties to restore for label {label}. Skipping.")
                continue

            query = f"""
            UNWIND $data AS d
            MATCH (n:{label} {{{identifier_key}: d.{identifier_key}}})
            SET {', '.join(set_clauses)}
            """
            
            batch_size = 2000
            for i in range(0, len(data_list), batch_size):
                batch = data_list[i:i + batch_size]
                counters = neo4j_mgr.execute_autocommit_query(query, params={"data": batch})
                logger.info(f"  Processed batch for {label}. Properties set: {counters.properties_set}")

        logger.info("Restore summary from cache to neo4j graph complete.")

def main():
    import input_params 

    """Main entry point for the command-line tool."""
    parser = argparse.ArgumentParser(description="A tool for managing RAG summary caches.")
    input_params.add_rag_args(parser)
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available commands")
    path_arg = argparse.ArgumentParser(add_help=False)
    path_arg.add_argument("--path", help="Optional path to the summary cache file.")
    parser_backup = subparsers.add_parser("backup", help="Backup summaries from graph to file.", parents=[path_arg])
    parser_backup.add_argument("--batch-size", type=int, default=10000, help="Number of records per query.")
    subparsers.add_parser("restore", help="Restore summaries from file to graph.", parents=[path_arg])
    args = parser.parse_args()

    try:
        with Neo4jManager() as neo4j_mgr:
            if not neo4j_mgr.check_connection():
                logger.critical("Could not connect to Neo4j.")
                sys.exit(1)
            
            manager = SummaryManager(
                project_path="",
                llm_api=args.llm_api,
                token_encoding=args.token_encoding,
                max_context_token_size=args.max_context_size
            )
            manager.configure_from_graph(neo4j_mgr)
            cache_file_path = args.path or manager.cache_path

            if args.command == "backup":
                manager.backup_cache_file_from_neo4j(neo4j_mgr, cache_file_path, batch_size=args.batch_size)
            elif args.command == "restore":
                manager.restore_cache_file_to_neo4j(neo4j_mgr, cache_file_path)
    except Exception as e:
        logger.critical(f"An error occurred: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
