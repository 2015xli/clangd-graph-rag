#!/usr/bin/env python3
"""
This module consumes parsed clangd symbol data and function span data
to produce a function-level call graph.
"""

import yaml
import re
from typing import Dict, List, Tuple, Optional, Any, Set
import logging
import gc
import os
import argparse
import json
import math
from tqdm import tqdm
from collections import defaultdict

import input_params
from compilation_parser import SourceSpan
from compilation_manager import CompilationManager
from clangd_index_yaml_parser import (
    SymbolParser, Symbol, Location, Reference, RelativeLocation, CallRelation
)
from neo4j_manager import Neo4jManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Base Extractor Class ---
class BaseClangdCallGraphExtractor:
    def __init__(self, symbol_parser: SymbolParser, log_batch_size: int = 1000, ingest_batch_size: int = 1000):
        self.symbol_parser = symbol_parser
        self.log_batch_size = log_batch_size
        self.ingest_batch_size = ingest_batch_size

    def generate_statistics(self, caller_to_callees_map: Dict[str, Set[str]]) -> str:
        """Generate statistics about the extracted call graph."""
        all_call_relations = []
        for caller_id, callees in caller_to_callees_map.items():
            for callee_id in callees:
                all_call_relations.append((caller_id, callee_id))

        callers = set(caller_to_callees_map.keys())
        callees = set(callee for callee_set in caller_to_callees_map.values() for callee in callee_set)
        
        functions_in_graph = callers.union(callees)
        recursive_calls = sum(1 for caller, callee_set in caller_to_callees_map.items() if caller in callee_set)
        
        functions_with_bodies = len([f for f in self.symbol_parser.functions.values() if f.body_location])
        
        stats = f"""
Call Graph Statistics:
=====================
Total functions in clangd index: {len(self.symbol_parser.functions)}
Functions with body spans: {functions_with_bodies}
Total unique functions in call graph: {len(functions_in_graph)}
Functions that call others: {len(callers)}
Functions that are called: {len(callees)}
Total call relationships: {len(all_call_relations)}
Recursive calls: {recursive_calls}
Functions that only call (entry points): {len(callers - callees)}
Functions that are only called (leaf functions): {len(callees - callers)}
"""
        return stats

    def ingest_call_relations(self, caller_to_callees_map: Dict[str, Set[str]], neo4j_mgr: Optional[Neo4jManager] = None) -> None:
        """
        Ingests call relations from a map into Neo4j in batches.
        """
        if not caller_to_callees_map:
            logger.info("No call relations to ingest.")
            return

        # Flatten the map into a list of pairs for ingestion
        all_relations = []
        for caller_id, callee_set in caller_to_callees_map.items():
            for callee_id in callee_set:
                all_relations.append({"caller_id": caller_id, "callee_id": callee_id})

        total_relations = len(all_relations)
        logger.info(f"Preparing {total_relations} call relationships for batched ingestion (1 batch = {self.ingest_batch_size} relationships)...")

        query = """
        UNWIND $relations as relation
        MATCH (caller) WHERE (caller:FUNCTION OR caller:METHOD) AND caller.id = relation.caller_id
        MATCH (callee) WHERE (callee:FUNCTION OR callee:METHOD) AND callee.id = relation.callee_id
        MERGE (caller)-[:CALLS]->(callee)
        """

        if neo4j_mgr:
            total_rels_created = 0
            for i in tqdm(range(0, total_relations, self.ingest_batch_size), desc="Ingesting CALLS relations"):
                batch = all_relations[i:i + self.ingest_batch_size]
                all_counters = neo4j_mgr.process_batch([(query, {"relations": batch})])
                for counters in all_counters:
                    total_rels_created += counters.relationships_created
            logger.info(f"Finished processing {total_relations} call relationships in batches.")
            logger.info(f"  Total CALLS relationships created: {total_rels_created}")
        else:
            # Fallback to writing to a file if no manager is provided
            output_file_path = "generated_call_graph_cypher_queries.cql"
            with open(output_file_path, 'w') as f:
                f.write(f"// Total relations: {total_relations}\n")
                f.write(f"{query.strip()};\n")
                f.write(f"// PARAMS: Use the full list of relations\n")
            logger.info(f"Batched Cypher queries written to {output_file_path}")

# --- Extractor Without Container ---
class ClangdCallGraphExtractorWithoutContainer(BaseClangdCallGraphExtractor):
    def __init__(self, symbol_parser: SymbolParser, log_batch_size: int = 1000, ingest_batch_size: int = 1000):
        super().__init__(symbol_parser, log_batch_size, ingest_batch_size)

    def _is_location_within_function_body(self, call_loc: Location, body_loc: RelativeLocation, body_file_uri: str) -> bool:
        if call_loc.file_uri != body_file_uri:
            return False
        
        start_ok = (call_loc.start_line > body_loc.start_line) or \
                   (call_loc.start_line == body_loc.start_line and call_loc.start_column >= body_loc.start_column)
        
        end_ok = (call_loc.end_line < body_loc.end_line) or \
                 (call_loc.end_line == body_loc.end_line and call_loc.end_column <= body_loc.end_column)
        
        return start_ok and end_ok

    def extract_call_relationships(self, generate_bidirectional: bool = False):
        """Extract function call relationships from the parsed data using spatial indexing."""
        caller_to_callees = defaultdict(set)
        callee_to_callers = defaultdict(set) if generate_bidirectional else None

        functions_with_bodies = {fid: f for fid, f in self.symbol_parser.functions.items() if f.body_location}
        
        if not functions_with_bodies:
            logger.warning("No functions have body locations. Did you load function spans?")
            return (caller_to_callees, callee_to_callers) if generate_bidirectional else caller_to_callees
        
        logger.info(f"Analyzing calls for {len(functions_with_bodies)} functions with body spans using optimized lookup")

        file_to_function_bodies_index: Dict[str, List[Tuple[RelativeLocation, Symbol]]] = {}
        for caller_symbol in functions_with_bodies.values():
            if caller_symbol.body_location and caller_symbol.definition:
                file_uri = caller_symbol.definition.file_uri
                file_to_function_bodies_index.setdefault(file_uri, []).append((caller_symbol.body_location, caller_symbol))

        for file_uri in file_to_function_bodies_index:
            file_to_function_bodies_index[file_uri].sort(key=lambda item: item[0].start_line)
        logger.info(f"Built spatial index for {len(file_to_function_bodies_index)} files.")
        del functions_with_bodies
        gc.collect()

        if self.symbol_parser.has_call_kind:
            valid_call_kinds = [20, 28]
        else:
            valid_call_kinds = [4, 12]
        logger.info(f"Using call kinds for detection: {valid_call_kinds}")

        logger.info("Processing call relationships for callees...")
        for callee_symbol in self.symbol_parser.symbols.values():
            if not callee_symbol.references or not callee_symbol.is_function():
                continue
            
            for reference in callee_symbol.references:
                if reference.kind not in valid_call_kinds:
                    continue
                
                call_location = reference.location
                if call_location.file_uri in file_to_function_bodies_index:
                    for body_loc, caller_symbol in file_to_function_bodies_index[call_location.file_uri]:
                        if self._is_location_within_function_body(call_location, body_loc, call_location.file_uri):
                            caller_to_callees[caller_symbol.id].add(callee_symbol.id)
                            if generate_bidirectional:
                                callee_to_callers[callee_symbol.id].add(caller_symbol.id)
                            break

        total_relations = sum(len(v) for v in caller_to_callees.values())
        logger.info(f"Extracted {total_relations} call relationships")
        del file_to_function_bodies_index
        gc.collect()

        return (caller_to_callees, callee_to_callers) if generate_bidirectional else caller_to_callees
    
class ClangdCallGraphExtractorWithContainer(BaseClangdCallGraphExtractor):
    def extract_call_relationships(self, generate_bidirectional: bool = False):
        caller_to_callees = defaultdict(set)
        callee_to_callers = defaultdict(set) if generate_bidirectional else None
        
        logger.info("Extracting call relationships using Container field...")

        for callee_symbol in self.symbol_parser.symbols.values():
            if not callee_symbol.references or not callee_symbol.is_function():
                continue
            
            for reference in callee_symbol.references:
                if reference.container_id and reference.container_id != '0000000000000000' and reference.kind in [20, 28]:
                    caller_id = reference.container_id
                    caller_symbol = self.symbol_parser.symbols.get(caller_id)
                    
                    if caller_symbol and caller_symbol.is_function():
                        caller_to_callees[caller_id].add(callee_symbol.id)
                        if generate_bidirectional:
                            callee_to_callers[callee_symbol.id].add(caller_id)
        
        total_relations = sum(len(v) for v in caller_to_callees.values())
        logger.info(f"Extracted {total_relations} call relationships")
        return (caller_to_callees, callee_to_callers) if generate_bidirectional else caller_to_callees

import input_params
from pathlib import Path

def main():
    """Main function to demonstrate usage."""
    parser = argparse.ArgumentParser(description='Extract call graph from clangd index YAML')

    input_params.add_core_input_args(parser)
    input_params.add_worker_args(parser)
    input_params.add_batching_args(parser)
    input_params.add_logistic_args(parser)
    input_params.add_source_parser_args(parser)

    args = parser.parse_args()

    args.index_file = str(args.index_file.resolve())
    args.project_path = str(args.project_path.resolve())

    if args.ingest_batch_size is None:
        args.ingest_batch_size = args.cypher_tx_size
    
    # --- Phase 0: Load, Parse, and Link Symbols ---
    logger.info("\n--- Starting Phase 0: Loading, Parsing, and Linking Symbols ---")
    symbol_parser = SymbolParser(
        index_file_path=args.index_file,
        log_batch_size=args.log_batch_size
    )
    symbol_parser.parse(num_workers=args.num_parse_workers)
    logger.info("--- Finished Phase 0 ---")

    # --- NEW: Phase 1: Parse Source Code (for spans) ---
    logger.info("\n--- Starting Phase 1: Parsing Source Code for Spans ---")
    compilation_manager = CompilationManager(
        parser_type=args.source_parser,
        project_path=args.project_path,
        compile_commands_path=args.compile_commands
    )
    compilation_manager.parse_folder(args.project_path)
    logger.info("--- Finished Phase 1 ---")

    # --- NEW: Phase 2: Create SourceSpanProvider adapter ---
    from source_span_provider import SourceSpanProvider
    logger.info("\n--- Starting Phase 2: Enriching Symbols with Spans ---")
    span_provider = SourceSpanProvider(symbol_parser=symbol_parser, compilation_manager=compilation_manager)
    span_provider.enrich_symbols_with_span()
    logger.info("--- Finished Phase 2 ---")

    # --- Phase 3: Create extractor based on available features ---
    logger.info("\n--- Starting Phase 3: Creating Call Graph Extractor ---")
    if symbol_parser.has_container_field:
        extractor = ClangdCallGraphExtractorWithContainer(symbol_parser, args.log_batch_size, args.ingest_batch_size)
        logger.info("Using ClangdCallGraphExtractorWithContainer (new format detected).")
    else:
        extractor = ClangdCallGraphExtractorWithoutContainer(symbol_parser, args.log_batch_size, args.ingest_batch_size)
        logger.info("Using ClangdCallGraphExtractorWithoutContainer (old format detected).")
    logger.info("--- Finished Phase 3 ---")

    # --- Phase 4: Extract call relationships ---
    logger.info("\n--- Starting Phase 4: Extracting Call Relationships ---")
    # For standalone execution, we only need the unidirectional map for ingestion
    caller_to_callees_map = extractor.extract_call_relationships(generate_bidirectional=False)
    logger.info("--- Finished Phase 4 ---")
    
    # --- Phase 5: Ingest or write to file ---
    logger.info("\n--- Starting Phase 5: Ingesting/Writing Call Relations ---")
    if args.ingest:
        with Neo4jManager() as neo4j_mgr:
            if neo4j_mgr.check_connection():
                if not neo4j_mgr.verify_project_path(args.project_path):
                    return
                extractor.ingest_call_relations(caller_to_callees_map, neo4j_mgr=neo4j_mgr)
    else:
        extractor.ingest_call_relations(caller_to_callees_map, neo4j_mgr=None)
    logger.info("--- Finished Phase 5 ---")
    
    # --- Phase 6: Generate statistics ---
    if args.stats:
        logger.info("\n--- Starting Phase 6: Generating Statistics ---")
        stats = extractor.generate_statistics(caller_to_callees_map)
        logger.info(stats)
        logger.info("--- Finished Phase 6 ---")

if __name__ == "__main__": 
    main()
