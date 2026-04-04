#!/usr/bin/env python3
"""
This module consumes parsed clangd symbol data and function span data
to produce a function-level call graph.
"""

from typing import Dict, List, Tuple, Optional, Any, Set
import logging
import gc
import argparse
from tqdm import tqdm
from collections import defaultdict

import input_params
from source_parser import CompilationManager
from symbol_parser import (
    SymbolParser, Symbol, Location, Reference, RelativeLocation, CallRelation
)
from neo4j_manager import Neo4jManager
from utils import align_string

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class ClangdCallGraphExtractor:
    """
    Unified class for extracting call relationships from clangd index data.
    Automatically switches between metadata-based (Container field) and 
    spatial-based extraction strategies.
    """
    def __init__(self, symbol_parser: SymbolParser, log_batch_size: int = 1000, ingest_batch_size: int = 1000):
        self.symbol_parser = symbol_parser
        self.log_batch_size = log_batch_size
        self.ingest_batch_size = ingest_batch_size

    def extract_call_relationships(self, generate_bidirectional: bool = False):
        """Dispatches to the best available extraction strategy."""
        if self.symbol_parser.has_container_field:
            return self._extract_with_container(generate_bidirectional)
        else:
            return self._extract_without_container(generate_bidirectional)

    def _extract_with_container(self, generate_bidirectional: bool = False):
        """Strategy 1: Using the explicit 'Container' field (Modern Clangd)."""
        logger.info("Extracting call relationships using Container field...")
        caller_to_callees = defaultdict(set)
        callee_to_callers = defaultdict(set) if generate_bidirectional else None

        for callee_symbol in self.symbol_parser.symbols.values():
            if not callee_symbol.references or not callee_symbol.is_function():
                continue
            
            for reference in callee_symbol.references:
                # Kinds 20/28 are function calls in modern Clangd
                if reference.container_id and reference.container_id != '0000000000000000' and reference.kind in [20, 28]:
                    caller_id = reference.container_id
                    caller_symbol = self.symbol_parser.symbols.get(caller_id)
                    
                    if caller_symbol and caller_symbol.is_function():
                        caller_to_callees[caller_id].add(callee_symbol.id)
                        if generate_bidirectional:
                            callee_to_callers[callee_symbol.id].add(caller_id)
        
        total_relations = sum(len(v) for v in caller_to_callees.values())
        logger.info(f"Extracted {total_relations} call relationships using Container field.")
        return (caller_to_callees, callee_to_callers) if generate_bidirectional else caller_to_callees

    def _extract_without_container(self, generate_bidirectional: bool = False):
        """Strategy 2: Using spatial indexing (Older Clangd fallback)."""
        logger.info("Extracting call relationships using spatial indexing fallback...")
        caller_to_callees = defaultdict(set)
        callee_to_callers = defaultdict(set) if generate_bidirectional else None

        functions_with_bodies = {fid: f for fid, f in self.symbol_parser.functions.items() if f.body_location}
        
        if not functions_with_bodies:
            logger.warning("No functions have body locations. Call graph will be empty.")
            return (caller_to_callees, callee_to_callers) if generate_bidirectional else caller_to_callees
        
        # Build spatial index: file_uri -> list of (body_span, symbol)
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

        valid_call_kinds = [20, 28] if self.symbol_parser.has_call_kind else [4, 12]
        logger.info(f"Using call kinds for detection: {valid_call_kinds}")

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
        logger.info(f"Extracted {total_relations} call relationships using spatial indexing.")
        del file_to_function_bodies_index
        gc.collect()

        return (caller_to_callees, callee_to_callers) if generate_bidirectional else caller_to_callees

    def _is_location_within_function_body(self, call_loc: Location, body_loc: RelativeLocation, body_file_uri: str) -> bool:
        if call_loc.file_uri != body_file_uri:
            return False
        
        start_ok = (call_loc.start_line > body_loc.start_line) or \
                   (call_loc.start_line == body_loc.start_line and call_loc.start_column >= body_loc.start_column)
        
        end_ok = (call_loc.end_line < body_loc.end_line) or \
                 (call_loc.end_line == body_loc.end_line and call_loc.end_column <= body_loc.end_column)
        
        return start_ok and end_ok

    def ingest_call_relations(self, caller_to_callees_map: Dict[str, Set[str]], neo4j_mgr: Optional[Neo4jManager] = None) -> None:
        """Ingests call relations from a map into Neo4j in batches."""
        if not caller_to_callees_map:
            logger.info("No call relationships to ingest.")
            return

        all_relations = []
        for caller_id, callee_set in caller_to_callees_map.items():
            for callee_id in callee_set:
                all_relations.append({"caller_id": caller_id, "callee_id": callee_id})

        total_relations = len(all_relations)
        logger.info(f"Preparing {total_relations} call relationships for ingestion...")

        query = """
        UNWIND $relations as relation
        MATCH (caller) WHERE (caller:FUNCTION OR caller:METHOD) AND caller.id = relation.caller_id
        MATCH (callee) WHERE (callee:FUNCTION OR callee:METHOD) AND callee.id = relation.callee_id
        MERGE (caller)-[:CALLS]->(callee)
        """

        if neo4j_mgr:
            total_rels_created = 0
            for i in tqdm(range(0, total_relations, self.ingest_batch_size), desc=align_string("Ingesting CALLS relations")):
                batch = all_relations[i:i + self.ingest_batch_size]
                all_counters = neo4j_mgr.process_batch([(query, {"relations": batch})])
                for counters in all_counters:
                    total_rels_created += counters.relationships_created
            logger.info(f"  Total CALLS relationships created: {total_rels_created}")
        else:
            # Fallback to writing to a file for debugging
            output_file_path = "generated_call_graph_cypher_queries.cql"
            with open(output_file_path, 'w') as f:
                f.write(f"// Total relations: {total_relations}\n")
                f.write(f"{query.strip()};\n")
            logger.info(f"Batched Cypher queries written to {output_file_path}")

    def generate_statistics(self, caller_to_callees_map: Dict[str, Set[str]]) -> str:
        """Generate summary statistics about the call graph."""
        all_call_relations = [(c, e) for c, e_set in caller_to_callees_map.items() for e in e_set]
        callers = set(caller_to_callees_map.keys())
        callees = {e for e_set in caller_to_callees_map.values() for e in e_set}
        functions_in_graph = callers.union(callees)
        recursive_calls = sum(1 for c, e_set in caller_to_callees_map.items() if c in e_set)
        
        return f"""
Call Graph Statistics:
=====================
Total functions in index: {len(self.symbol_parser.functions)}
Functions in call graph:  {len(functions_in_graph)}
Functions calling:        {len(callers)}
Functions called:         {len(callees)}
Total call relationships: {len(all_call_relations)}
Recursive calls:          {recursive_calls}
Entry points (only call): {len(callers - callees)}
Leaf functions (only called): {len(callees - callers)}
"""

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
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
    
    logger.info("\n--- Phase 0: Parsing Clangd Index ---")
    symbol_parser = SymbolParser(index_file_path=args.index_file, log_batch_size=args.log_batch_size)
    symbol_parser.parse(num_workers=args.num_parse_workers)

    logger.info("\n--- Phase 1: Parsing Source Code for Spans ---")
    compilation_manager = CompilationManager(project_path=args.project_path, compile_commands_path=args.compile_commands)
    compilation_manager.parse_folder(args.project_path)

    from symbol_enricher import SymbolEnricher
    logger.info("\n--- Phase 2: Enriching Symbols with Spans ---")
    symbol_enricher = SymbolEnricher(symbol_parser=symbol_parser, compilation_manager=compilation_manager)
    symbol_enricher.enrich_symbols()

    logger.info("\n--- Phase 3: Extracting Call Relationships ---")
    extractor = ClangdCallGraphExtractor(symbol_parser, args.log_batch_size, args.ingest_batch_size)
    caller_to_callees_map = extractor.extract_call_relationships(generate_bidirectional=False)
    
    logger.info("\n--- Phase 4: Ingesting Call Relations ---")
    if args.ingest:
        with Neo4jManager() as neo4j_mgr:
            if neo4j_mgr.check_connection():
                if not neo4j_mgr.check_connection(): return
                extractor.ingest_call_relations(caller_to_callees_map, neo4j_mgr=neo4j_mgr)
    else:
        extractor.ingest_call_relations(caller_to_callees_map, neo4j_mgr=None)
    
    if args.stats:
        print(extractor.generate_statistics(caller_to_callees_map))

if __name__ == "__main__": 
    main()
