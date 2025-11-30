#!/usr/bin/env python3
"""
This module encapsulates the logic for rebuilding a dirty scope in the graph.

It is responsible for creating a "sufficient subset" of symbols from a full
clangd index and then running a mini-ingestion pipeline on that subset.
"""

import os
import logging
from typing import Dict, List, Set, Optional
from collections import defaultdict
from tqdm import tqdm 

# Lower-level data structures and utilities
from clangd_index_yaml_parser import SymbolParser, Symbol
from compilation_manager import CompilationManager
from neo4j_manager import Neo4jManager

# Ingestion components
from clangd_symbol_nodes_builder import PathManager, PathProcessor, SymbolProcessor
from clangd_call_graph_builder import ClangdCallGraphExtractorWithContainer, ClangdCallGraphExtractorWithoutContainer
from source_span_provider import SourceSpanProvider
from include_relation_provider import IncludeRelationProvider

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class GraphUpdateScopeBuilder:
    """Orchestrates the rebuilding of a dirty scope within the graph."""

    def __init__(self, args, neo4j_mgr: Neo4jManager, project_path: str):
        self.args = args
        self.neo4j_mgr = neo4j_mgr
        self.project_path = project_path
        self.comp_manager = None
        self.mini_symbol_parser = None

    def build_miniparser_for_dirty_scope(self, dirty_files: Set[str], full_symbol_parser: SymbolParser, new_commit: Optional[str], old_commit: Optional[str]) -> SymbolParser:
        """Main entry point to run the mini-rebuild pipeline."""
        # dirty_files include all the added/modified files and recursively impacted files by modified header files.
        logger.info(f"\n--- Phase 4: Rebuilding scope for {len(dirty_files)} Dirty Files ---")
        if not dirty_files:
            logger.info("No dirty files to rebuild. Skipping.")
            return None # Return None to indicate no mini_parser was generated

        # 1. Parse only the dirty source files to get up-to-date span/include info
        self.comp_manager = CompilationManager(
            parser_type=self.args.source_parser,
            project_path=self.project_path,
            compile_commands_path=self.args.compile_commands
        )
        self.comp_manager.parse_files(
            list(dirty_files),
            self.args.num_parse_workers,
            new_commit=new_commit,
            old_commit=old_commit
        )

        # 2. Enrich the full symbol parser symbols with the fresh spans, and parent/child relationships
        span_provider = SourceSpanProvider(full_symbol_parser, self.comp_manager)
        span_provider.enrich_symbols_with_span()

        # 3. Find the seed symbols
        dirty_file_uris = {f"file://{os.path.abspath(f)}" for f in dirty_files}
        seed_symbol_ids = {
            s.id for s in full_symbol_parser.symbols.values()
            if s.definition and s.definition.file_uri in dirty_file_uris \
                or s.declaration and s.declaration.file_uri in dirty_file_uris
        }

        # 4. Create the "sufficient subset" of symbols        
        self.mini_symbol_parser = self._create_sufficient_subset(full_symbol_parser, seed_symbol_ids)

        return self.mini_symbol_parser

    def rebuild_mini_scope(self):
        mini_symbol_parser = self.mini_symbol_parser
        comp_manager = self.comp_manager
        neo4j_mgr = self.neo4j_mgr
        args = self.args
        project_path = self.project_path

        if not mini_symbol_parser:
            logger.info("No symbols to rebuild. Skipping.")
            return
        
        # 5. Re-run the ingestion pipeline on the mini-scope
        path_manager = PathManager(project_path)
        
        path_processor = PathProcessor(path_manager, neo4j_mgr, args.log_batch_size, args.ingest_batch_size)
        path_processor.ingest_paths(mini_symbol_parser.symbols, comp_manager)

        symbol_processor = SymbolProcessor(path_manager, args.log_batch_size, args.ingest_batch_size, args.cypher_tx_size)
        symbol_processor.ingest_symbols_and_relationships(mini_symbol_parser, neo4j_mgr, args.defines_generation)

        include_provider = IncludeRelationProvider(neo4j_mgr, project_path)
        include_provider.ingest_include_relations(comp_manager, args.ingest_batch_size)

        # 5.5 Re-ingest call graph for the mini-scope
        logger.info("Re-ingesting call graph for the dirty scope...")
        if mini_symbol_parser.has_container_field:
            extractor = ClangdCallGraphExtractorWithContainer(mini_symbol_parser, args.log_batch_size, args.ingest_batch_size)
        else:
            extractor = ClangdCallGraphExtractorWithoutContainer(mini_symbol_parser, args.log_batch_size, args.ingest_batch_size)
        
        caller_to_callees_map = extractor.extract_call_relationships(generate_bidirectional=False)
        extractor.ingest_call_relations(caller_to_callees_map, neo4j_mgr=neo4j_mgr)

        logger.info("--- Re-ingestion complete ---")
        return

    def _build_scope_maps(self, symbols: Dict[str, Symbol]) -> Dict[str, str]:
        """
        Performs a single pass over all symbols to build a lookup table for
        qualified namespace names to their symbol IDs.
        """
        logger.info("Building scope-to-ID lookup maps for namespaces...")
        qualified_namespace_to_id = {}

        for sym in tqdm(symbols.values(), desc="Building scope maps"):
            if sym.kind == 'Namespace':
                qualified_name = sym.scope + sym.name + '::'
                qualified_namespace_to_id[qualified_name] = sym.id

        logger.info(f"Built map for {len(qualified_namespace_to_id)} namespaces.")
        return qualified_namespace_to_id

    def _create_sufficient_subset(self, full_symbol_parser: SymbolParser, seed_symbol_ids: set) -> 'SymbolParser':
        """
        Creates a new SymbolParser instance containing the seed symbols and their
        direct dependencies, without recursive traversal.
        """
        logger.info(f"Starting sufficient subset creation from {len(seed_symbol_ids)} seed symbols.")

        # --- 1. Pre-computation for efficient lookups ---
        logger.info("Building temporary in-memory relationship graphs for expansion...")

        # Build containment graph for parent->child traversal (e.g., CLASS -> METHOD)
        containment_graph = defaultdict(lambda: {'children': set()})
        for sym in full_symbol_parser.symbols.values():
            if sym.parent_id:
                containment_graph[sym.parent_id]['children'].add(sym.id)

        # Build a map from fully qualified namespace names to their symbol IDs
        qualified_namespace_to_id = self._build_scope_maps(full_symbol_parser.symbols)

        inheritance_graph = defaultdict(lambda: {'parents': set(), 'children': set()})
        for base_id, derived_id in full_symbol_parser.inheritance_relations:
            inheritance_graph[base_id]['children'].add(derived_id)
            inheritance_graph[derived_id]['parents'].add(base_id)

        override_graph = defaultdict(lambda: {'overridden': set(), 'overriding': set()})
        for base_id, derived_id in full_symbol_parser.override_relations:
            override_graph[base_id]['overridden'].add(derived_id)
            override_graph[derived_id]['overriding'].add(base_id)

        if full_symbol_parser.has_container_field:
            extractor = ClangdCallGraphExtractorWithContainer(full_symbol_parser)
        else:
            logger.warning("Cannot extract call graph for subset without container field. Call graph expansion will be incomplete.")
            extractor = None

        if extractor:
            caller_to_callees, callee_to_callers = extractor.extract_call_relationships(generate_bidirectional=True)
        else:
            caller_to_callees, callee_to_callers = defaultdict(set), defaultdict(set)

        # --- 2. Single-Level Expansion ---
        logger.info("Expanding seed set to include direct dependencies...")
        final_symbol_ids = set(seed_symbol_ids)
        
        # Create a temporary set to hold the direct dependencies we find
        direct_dependencies = set()

        for symbol_id in seed_symbol_ids:
            symbol = full_symbol_parser.symbols.get(symbol_id)
            if not symbol: 
                logger.error(f"Could not find symbol {symbol_id} of seed set in full symbol parser.")
                continue

            def add_symbol(direct_dependencies, symbol_id):
                if symbol_id not in full_symbol_parser.symbols:
                    # full_symbol_parser may include some IDs (e.g., in !Relations) that have no corresponding symbols.
                    # TODO: to check if this is a bug in clangd.
                    logger.debug(f"Could not find symbol {symbol_id} of seed set in full symbol parser.")
                    return
                if symbol_id not in final_symbol_ids:
                    final_symbol_ids.add(symbol_id)
                    direct_dependencies.add(symbol_id)

            # --- Find all direct dependencies for the current seed symbol ---

            # 1. Calls (Up and Down)
            for callee_id in caller_to_callees.get(symbol_id, set()): add_symbol(direct_dependencies, callee_id)
            for caller_id in callee_to_callers.get(symbol_id, set()): add_symbol(direct_dependencies, caller_id)

            # 2. Lexical Parent (Up)
            if symbol.parent_id:
                add_symbol(direct_dependencies, symbol.parent_id)

            # 3. Semantic Namespace Parent (Up)
            if symbol.scope:
                ns_id = qualified_namespace_to_id.get(symbol.scope)
                if ns_id:
                    add_symbol(direct_dependencies, ns_id)

            # 4. Inheritance (Up and Down)
            if symbol_id in inheritance_graph:
                for parent_id in inheritance_graph[symbol_id]['parents']: add_symbol(direct_dependencies, parent_id)
                for child_id in inheritance_graph[symbol_id]['children']: add_symbol(direct_dependencies, child_id)

            # 5. Overrides (Up and Down)
            if symbol_id in override_graph:
                for overridden_id in override_graph[symbol_id]['overridden']: add_symbol(direct_dependencies, overridden_id)
                for overriding_id in override_graph[symbol_id]['overriding']: add_symbol(direct_dependencies, overriding_id)
            
            # 6. Containment (Down to members/children)
            if symbol_id in containment_graph:
                for child_id in containment_graph[symbol_id]['children']:
                    add_symbol(direct_dependencies, child_id)

        # Add the collected direct dependencies to the final set
        final_symbol_ids.update(direct_dependencies)

        logger.info(f"Expanded set to {len(final_symbol_ids)} total symbols (seeds + direct dependencies).")

        # --- 3. Build the new SymbolParser instance ---
        subset_parser = SymbolParser(full_symbol_parser.index_file_path)
        for symbol_id in final_symbol_ids:
            # This check is redundant, since all the symbols are extracted from the full symbol parser.
            if symbol_id not in full_symbol_parser.symbols:
                logger.debug(f"Could not find symbol {symbol_id} of subset_parser in full symbol parser.")
                continue
            subset_parser.symbols[symbol_id] = full_symbol_parser.symbols[symbol_id]
        
        for symbol in subset_parser.symbols.values():
            if symbol.is_function():
                subset_parser.functions[symbol.id] = symbol
        
        subset_parser.has_container_field = full_symbol_parser.has_container_field
        subset_parser.has_call_kind = full_symbol_parser.has_call_kind
        
        # Filter relations to only include those relevant to the subset
        subset_parser.inheritance_relations = [
            (subj, obj) for subj, obj in full_symbol_parser.inheritance_relations
            if subj in final_symbol_ids and obj in final_symbol_ids
        ]
        subset_parser.override_relations = [
            (subj, obj) for subj, obj in full_symbol_parser.override_relations
            if subj in final_symbol_ids and obj in final_symbol_ids
        ]
        
        logger.info(f"Created mini-parser with {len(subset_parser.symbols)} symbols ({len(subset_parser.functions)} functions).")
        return subset_parser
