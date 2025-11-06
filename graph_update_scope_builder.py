#!/usr/bin/env python3
"""
This module encapsulates the logic for rebuilding a dirty scope in the graph.

It is responsible for creating a "sufficient subset" of symbols from a full
clangd index and then running a mini-ingestion pipeline on that subset.
"""

import os, re
import logging
from typing import Dict, List, Set
from collections import defaultdict, deque

# Lower-level data structures and utilities
from clangd_index_yaml_parser import SymbolParser
from compilation_manager import CompilationManager
from neo4j_manager import Neo4jManager

# Ingestion components
from clangd_symbol_nodes_builder import PathManager, PathProcessor, SymbolProcessor
from clangd_call_graph_builder import ClangdCallGraphExtractorWithContainer, ClangdCallGraphExtractorWithoutContainer
from source_span_provider import SourceSpanProvider
from include_relation_provider import IncludeRelationProvider

logger = logging.getLogger(__name__)

class GraphUpdateScopeBuilder:
    """Orchestrates the rebuilding of a dirty scope within the graph."""

    def __init__(self, args, neo4j_mgr: Neo4jManager, project_path: str):
        self.args = args
        self.neo4j_mgr = neo4j_mgr
        self.project_path = project_path

    def rebuild_dirty_scope(self, dirty_files: Set[str], full_symbol_parser: SymbolParser):
        """Main entry point to run the mini-rebuild pipeline."""
        logger.info(f"\n--- Phase 4: Rebuilding scope for {len(dirty_files)} Dirty Files ---")
        if not dirty_files:
            logger.info("No dirty files to rebuild. Skipping.")
            return None # Return None to indicate no mini_parser was generated

        # 1. Parse only the dirty source files to get up-to-date span/include info
        comp_manager = CompilationManager(
            parser_type=self.args.source_parser,
            project_path=self.project_path,
            compile_commands_path=self.args.compile_commands
        )
        comp_manager.parse_files(list(dirty_files), self.args.num_parse_workers)

        # 2. Create the "sufficient subset" of symbols
        dirty_file_uris = {f"file://{os.path.abspath(f)}" for f in dirty_files}
        seed_symbol_ids = {
            s.id for s in full_symbol_parser.symbols.values()
            if s.definition and s.definition.file_uri in dirty_file_uris
        }
        
        mini_symbol_parser = self._create_sufficient_subset(full_symbol_parser, seed_symbol_ids)

        # 3. Enrich the new mini-parser symbols with the fresh spans
        span_provider = SourceSpanProvider(mini_symbol_parser, comp_manager)
        span_provider.enrich_symbols_with_span()

        # 4. Re-run the ingestion pipeline on the mini-scope
        path_manager = PathManager(self.project_path)
        
        path_processor = PathProcessor(path_manager, self.neo4j_mgr, self.args.log_batch_size, self.args.ingest_batch_size)
        path_processor.ingest_paths(mini_symbol_parser.symbols, comp_manager)

        symbol_processor = SymbolProcessor(path_manager, self.args.log_batch_size, self.args.ingest_batch_size, self.args.cypher_tx_size)
        symbol_processor.ingest_symbols_and_relationships(mini_symbol_parser, self.neo4j_mgr, self.args.defines_generation)

        include_provider = IncludeRelationProvider(self.neo4j_mgr, self.project_path)
        include_provider.ingest_include_relations(comp_manager, self.args.ingest_batch_size)

        # 4.5 Re-ingest call graph for the mini-scope
        logger.info("Re-ingesting call graph for the dirty scope...")
        if mini_symbol_parser.has_container_field:
            extractor = ClangdCallGraphExtractorWithContainer(mini_symbol_parser, self.args.log_batch_size, self.args.ingest_batch_size)
        else:
            extractor = ClangdCallGraphExtractorWithoutContainer(mini_symbol_parser, self.args.log_batch_size, self.args.ingest_batch_size)
        
        caller_to_callees_map = extractor.extract_call_relationships(generate_bidirectional=False)
        extractor.ingest_call_relations(caller_to_callees_map, neo4j_mgr=self.neo4j_mgr)

        logger.info("--- Re-ingestion complete ---")
        return mini_symbol_parser

    def _create_sufficient_subset(self, full_symbol_parser: SymbolParser, seed_symbol_ids: set) -> 'SymbolParser':
        """
        Creates a new SymbolParser instance containing only the specified symbols and all
        other symbols required to fully describe their relationships (parents, children, calls, etc.).
        """
        logger.info(f"Starting sufficient subset creation from {len(seed_symbol_ids)} seed symbols.")

        # --- 1. Pre-computation for efficient lookups ---
        logger.info("Building temporary relationship graphs for expansion...")
        # TODO: reuse the scope_name_to_id data structure in SymbolProcessor?
        scope_to_structure_id = {
            re.sub('<.*>', '', sym.scope) + sym.name + '::': sym.id
            for sym in full_symbol_parser.symbols.values()
            if sym.kind in ('Struct', 'Class', 'Union')
        }

        inheritance_graph = defaultdict(lambda: {'parents': set(), 'children': set()})
        for base_id, derived_id in full_symbol_parser.inheritance_relations:
            inheritance_graph[base_id]['children'].add(derived_id)
            inheritance_graph[derived_id]['parents'].add(base_id)

        override_graph = defaultdict(lambda: {'overridden': set(), 'overriding': set()})
        for base_id, derived_id in full_symbol_parser.override_relations:
            override_graph[base_id]['overriding'].add(derived_id)
            override_graph[derived_id]['overridden'].add(base_id)

        if full_symbol_parser.has_container_field:
            extractor = ClangdCallGraphExtractorWithContainer(full_symbol_parser)
        else:
            logger.warning("Cannot extract call graph for subset without container field. Call graph expansion will be incomplete.")
            extractor = None

        if extractor:
            caller_to_callees, callee_to_callers = extractor.extract_call_relationships(generate_bidirectional=True)
        else:
            caller_to_callees, callee_to_callers = defaultdict(set), defaultdict(set)

        # --- 2. Iterative Expansion ---
        logger.info("Expanding symbol set to include all dependencies...")
        final_symbol_ids = set(seed_symbol_ids)
        queue = deque(seed_symbol_ids)

        while queue:
            symbol_id = queue.popleft()
            symbol = full_symbol_parser.symbols.get(symbol_id)
            if not symbol: continue

            def add_to_set(new_id):
                if new_id and new_id not in final_symbol_ids:
                    final_symbol_ids.add(new_id)
                    queue.append(new_id)

            for callee_id in caller_to_callees.get(symbol_id, set()): add_to_set(callee_id)
            for caller_id in callee_to_callers.get(symbol_id, set()): add_to_set(caller_id)

            if symbol.scope in scope_to_structure_id:
                add_to_set(scope_to_structure_id[symbol.scope])

            if symbol_id in inheritance_graph:
                for parent_id in inheritance_graph[symbol_id]['parents']: add_to_set(parent_id)
                for child_id in inheritance_graph[symbol_id]['children']: add_to_set(child_id)

            if symbol_id in override_graph:
                for overridden_id in override_graph[symbol_id]['overridden']: add_to_set(overridden_id)
                for overriding_id in override_graph[symbol_id]['overriding']: add_to_set(overriding_id)

        logger.info(f"Expanded set to {len(final_symbol_ids)} total symbols.")

        # --- 3. Build the new SymbolParser instance ---
        subset_parser = SymbolParser(full_symbol_parser.index_file_path)
        for symbol_id in final_symbol_ids:
            if symbol_id in full_symbol_parser.symbols:
                subset_parser.symbols[symbol_id] = full_symbol_parser.symbols[symbol_id]
        
        for symbol in subset_parser.symbols.values():
            if symbol.is_function():
                subset_parser.functions[symbol.id] = symbol
        
        subset_parser.has_container_field = full_symbol_parser.has_container_field
        subset_parser.has_call_kind = full_symbol_parser.has_call_kind
        subset_parser.inheritance_relations = full_symbol_parser.inheritance_relations
        subset_parser.override_relations = full_symbol_parser.override_relations
        
        logger.info(f"Created mini-parser with {len(subset_parser.symbols)} symbols ({len(subset_parser.functions)} functions).")
        return subset_parser
