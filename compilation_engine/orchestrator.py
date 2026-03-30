#!/usr/bin/env python3
"""
Orchestrator for parallel processing of translation units.
"""
import os
import logging
import sys
import gc
from itertools import islice
from typing import List, Dict, Any, Tuple, Set
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from multiprocessing import get_context
from tqdm import tqdm
from collections import defaultdict

from .worker import _ClangWorkerImpl
from .types import SourceSpan, MacroSpan, TypeAliasSpan, IncludeRelation

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# --- Process-local worker and initializer ---
_worker_impl_instance = None
_count_processed_tus = 0

def _worker_initializer(init_args: Dict[str, Any]):
    """Initializes a worker implementation object for each process."""
    global _worker_impl_instance
    # Increase recursion limit for this worker process
    sys.setrecursionlimit(3000)
    _worker_impl_instance = _ClangWorkerImpl(**init_args)

def _parallel_worker(data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generic top-level worker function that uses the process-local worker object."""
    global _worker_impl_instance
    global _count_processed_tus

    if _worker_impl_instance is None:
        raise RuntimeError("Worker implementation has not been initialized.")

    if len(data) == 1:
        entry = data[0]
        _count_processed_tus += 1
        if _count_processed_tus % 1000 == 0: gc.collect()
        try:
            return _worker_impl_instance.run(entry)
        except Exception:
            file_path = entry.get('file', 'unknown')
            logger.exception(f"Worker failed on {file_path}")
            return {
                "span_results": defaultdict(dict),
                "include_relations": set(),
                "static_call_relations": set(),
                "type_alias_spans": {},
                "macro_spans": {}
            }

    # Task batch merging
    merged_span_results = defaultdict(dict)
    merged_include_relations = set()
    merged_static_call_relations = set()
    merged_type_alias_spans = {}
    merged_macro_spans = {}

    for entry in data:
        _count_processed_tus += 1
        if _count_processed_tus % 1000 == 0: gc.collect()
        try:
            result_dict = _worker_impl_instance.run(entry)
            merged_span_results.update(result_dict["span_results"])
            merged_include_relations.update(result_dict["include_relations"])
            merged_static_call_relations.update(result_dict["static_call_relations"])
            for alias_id, new_span in result_dict["type_alias_spans"].items():
                existing = merged_type_alias_spans.get(alias_id)
                if not existing or (new_span.is_aliasee_definition and not existing.is_aliasee_definition):
                    merged_type_alias_spans[alias_id] = new_span
            merged_macro_spans.update(result_dict.get("macro_spans", {}))
        except Exception:
            continue

    return {
        "span_results": merged_span_results,
        "include_relations": merged_include_relations,
        "static_call_relations": merged_static_call_relations,
        "type_alias_spans": merged_type_alias_spans,
        "macro_spans": merged_macro_spans
    }

class ParallelOrchestrator:
    """Manages the parallel execution of workers across multiple processes."""

    def run_parallel_parse(self, items_to_process: List[Dict], num_workers: int, desc: str, worker_init_args: Dict[str, Any], batch_size: int = 1) -> Dict[str, Any]:
        """Runs the parallel parsing and returns consolidated results."""
        all_spans = defaultdict(dict)
        all_includes = set()
        all_static_calls = set()
        all_type_alias_spans = {}
        all_macro_spans = {}
        file_tu_hash_map = defaultdict(set)

        items_iterator = iter(items_to_process)
        max_pending = num_workers * 2
        futures = {}

        ctx = get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=num_workers,
            mp_context=ctx,
            initializer=_worker_initializer,
            initargs=(worker_init_args,)
        ) as executor:

            def _next_batch():
                return list(islice(items_iterator, batch_size))

            for _ in range(max_pending):
                batch = _next_batch()
                if not batch: break
                future = executor.submit(_parallel_worker, batch)
                futures[future] = len(batch)

            total_tus = len(items_to_process)
            with tqdm(total=total_tus, desc=desc) as pbar:
                while futures:
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        batch_count = futures.pop(future)
                        pbar.update(batch_count)
                        
                        next_batch = _next_batch()
                        if next_batch:
                            nf = executor.submit(_parallel_worker, next_batch)
                            futures[nf] = len(next_batch)

                        try:
                            result_dict = future.result()
                            all_includes.update(result_dict["include_relations"])
                            all_static_calls.update(result_dict["static_call_relations"])
                            all_macro_spans.update(result_dict.get("macro_spans", {}))
                            
                            for alias_id, new_span in result_dict["type_alias_spans"].items():
                                existing = all_type_alias_spans.get(alias_id)
                                if not existing or (new_span.is_aliasee_definition and not existing.is_aliasee_definition):
                                    all_type_alias_spans[alias_id] = new_span

                            for (file_uri, tu_hash), id_to_span_dict in result_dict["span_results"].items():
                                # PHONY PARENT SUPPORT: We don't deduplicate by TU hash here because 
                                # a phony parent node span might be created in one TU even if the header was already seen.
                                file_tu_hash_map[file_uri].add(tu_hash)
                                all_spans[file_uri].update(id_to_span_dict)

                        except Exception as e:
                            logger.error(f"A worker batch failed: {e}", exc_info=True)

        gc.collect()
        return {
            "source_spans": all_spans,
            "include_relations": all_includes,
            "static_call_relations": all_static_calls,
            "type_alias_spans": all_type_alias_spans,
            "macro_spans": all_macro_spans
        }
