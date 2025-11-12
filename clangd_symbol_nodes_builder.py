#!/usr/bin/env python3
"""
This module processes an in-memory collection of clangd symbols to create
the file, folder, and symbol nodes in a Neo4j graph.
"""
import os
import sys
import argparse
import math
import re # Import re for scope normalization
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import List, Dict, Any, Tuple, Optional
from collections import defaultdict
import logging
import gc
from tqdm import tqdm

import input_params
# New imports from the common parser module
from clangd_index_yaml_parser import SymbolParser, Symbol, Location
from compilation_manager import CompilationManager
from neo4j_manager import Neo4jManager

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class PathManager:
    """Manages file paths and their relationships within the project."""
    def __init__(self, project_path: str) -> None:
        self.project_path = str(Path(project_path).resolve())
        
    def uri_to_relative_path(self, uri: str) -> str:
        parsed = urlparse(uri)
        if parsed.scheme != 'file': return uri
        path = unquote(parsed.path)
        try:
            return str(Path(path).relative_to(self.project_path))
        except ValueError:
            return path

    def is_within_project(self, path: str) -> bool:
        try:
            Path(path).relative_to(self.project_path)
            return True
        except ValueError:
            return False

class SymbolProcessor:
    """Processes Symbol objects and prepares data for Neo4j operations."""
    def __init__(self, path_manager: PathManager, log_batch_size: int = 1000, ingest_batch_size: int = 1000, cypher_tx_size: int = 500):
        self.path_manager = path_manager
        self.ingest_batch_size = ingest_batch_size
        self.log_batch_size = log_batch_size
        self.cypher_tx_size = cypher_tx_size

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

    def process_symbol(self, sym: Symbol, all_symbols: Dict[str, Symbol], qualified_namespace_to_id: Dict[str, str]) -> Optional[Dict]:
        if not sym.id or not sym.kind:
            return None

        symbol_data = {
            "id": sym.id,
            "name": sym.name,
            "kind": sym.kind,
            "scope": sym.scope,
            "language": sym.language,
            "has_definition": sym.definition is not None,
        }
        
        # Only process symbols that within the project path
        primary_location = sym.definition or sym.declaration
        if primary_location:
            abs_file_path = unquote(urlparse(primary_location.file_uri).path)
            if self.path_manager.is_within_project(abs_file_path):
                symbol_data["path"] = self.path_manager.uri_to_relative_path(primary_location.file_uri)
            else:
                if sym.kind != 'Namespace':
                    return None
            symbol_data["name_location"] = [primary_location.start_line, primary_location.start_column]

        # --- Parent ID Check ---
        # If the new provider added a parent_id, copy it over.
        if hasattr(sym, 'parent_id') and sym.parent_id:
            symbol_data["parent_id"] = sym.parent_id

        if sym.kind in ("Method", "Field") and not sym.parent_id:
            logger.debug(f"{sym.kind}: {sym.scope}{sym.name} ID: {sym.id} at {primary_location.file_uri}:{primary_location.start_line}:{primary_location.start_column} has no parent_id.")
            
        # --- Namespace Parent Check ---
        namespace_id = qualified_namespace_to_id.get(sym.scope)
        if namespace_id:
            symbol_data["namespace_id"] = namespace_id

        # --- Symbol Kind Processing ---
        if sym.kind == "Namespace":
            symbol_data["node_label"] = "NAMESPACE"
            symbol_data["qualified_name"] = sym.scope + sym.name + '::'

        elif sym.kind == "Function":
            symbol_data["node_label"] = "FUNCTION"
            symbol_data.update({
                "signature": sym.signature,
                "return_type": sym.return_type,
                "type": sym.type,
            })
            if hasattr(sym, 'body_location') and sym.body_location:
                symbol_data["body_location"] = [
                    sym.body_location.start_line,
                    sym.body_location.start_column,
                    sym.body_location.end_line,
                    sym.body_location.end_column
                ]

        elif sym.kind in ("InstanceMethod", "StaticMethod", "Constructor", "Destructor", "ConversionFunction"):
            symbol_data["node_label"] = "METHOD"
            symbol_data.update({
                "signature": sym.signature,
                "return_type": sym.return_type,
                "type": sym.type,
            })
            if hasattr(sym, 'body_location') and sym.body_location:
                symbol_data["body_location"] = [
                    sym.body_location.start_line,
                    sym.body_location.start_column,
                    sym.body_location.end_line,
                    sym.body_location.end_column
                ]

        elif sym.kind == "Class":
            symbol_data["node_label"] = "CLASS_STRUCTURE"
            if hasattr(sym, 'body_location') and sym.body_location:
                symbol_data["body_location"] = [
                    sym.body_location.start_line,
                    sym.body_location.start_column,
                    sym.body_location.end_line,
                    sym.body_location.end_column
                ]
        
        elif sym.kind == "Struct":
            if sym.language and sym.language.lower() == "cpp":
                symbol_data["node_label"] = "CLASS_STRUCTURE"
            else:
                symbol_data["node_label"] = "DATA_STRUCTURE"
            if hasattr(sym, 'body_location') and sym.body_location:
                symbol_data["body_location"] = [
                    sym.body_location.start_line,
                    sym.body_location.start_column,
                    sym.body_location.end_line,
                    sym.body_location.end_column
                ]

        elif sym.kind in ("Union", "Enum"):
            symbol_data["node_label"] = "DATA_STRUCTURE"
            if hasattr(sym, 'body_location') and sym.body_location:
                symbol_data["body_location"] = [
                    sym.body_location.start_line,
                    sym.body_location.start_column,
                    sym.body_location.end_line,
                    sym.body_location.end_column
                ]
        
        elif sym.kind == "Field":
            symbol_data["node_label"] = "FIELD"
            symbol_data.update({"type": sym.type, "is_static": False})

        elif sym.kind == "Variable":            
            # Check if the parent is a Class/Struct, making this a static field
            parent_id = symbol_data.get("parent_id")
            if parent_id:
                parent_sym = all_symbols.get(parent_id)
                if parent_sym and parent_sym.kind in ("Class", "Struct"):
                    symbol_data["node_label"] = "FIELD"
                    symbol_data.update({"type": sym.type, "is_static": True})
                else: # parent is not a class/struct, can be namespace
                    symbol_data["node_label"] = "VARIABLE"
                    symbol_data.update({"type": sym.type})

            else:
                # No parent, so it's a global variable
                symbol_data["node_label"] = "VARIABLE"
                symbol_data.update({"type": sym.type})
        else:
            return None

        if sym.definition:
            abs_file_path = unquote(urlparse(sym.definition.file_uri).path)
            if self.path_manager.is_within_project(abs_file_path):
                symbol_data["file_path"] = self.path_manager.uri_to_relative_path(sym.definition.file_uri)
            else: # all symbols should be within project except namespace symbols
                symbol_data["file_path"] = abs_file_path

        return symbol_data

    def _process_and_filter_symbols(self, symbols: Dict[str, Symbol], qualified_namespace_to_id: Dict[str, str]) -> Dict[str, List[Dict]]:
        processed_symbols = defaultdict(list)
        logger.info("Processing symbols for ingestion...")
        for sym in tqdm(symbols.values(), desc="Processing and grouping symbols by kind"):
            data = self.process_symbol(sym, symbols, qualified_namespace_to_id)
            if data and 'node_label' in data:
                processed_symbols[data['node_label']].append(data)
        
        return processed_symbols

    def ingest_symbols_and_relationships(self, symbol_parser: SymbolParser, neo4j_mgr: Neo4jManager, defines_generation_strategy: str = "batched-parallel"):
        # --- Phase 1: Discovery and Map Building ---
        logger.info("Phase 1: Building scope maps and processing symbols...")
        qualified_namespace_to_id = self._build_scope_maps(symbol_parser.symbols)
        processed_symbols = self._process_and_filter_symbols(symbol_parser.symbols, qualified_namespace_to_id)

        # --- Phase 2: Node Ingestion ---
        logger.info("Phase 2: Ingesting all nodes...")
        self._ingest_namespace_nodes(processed_symbols.get('NAMESPACE', []), neo4j_mgr)
        self._ingest_data_structure_nodes(processed_symbols.get('DATA_STRUCTURE', []), neo4j_mgr)
        self._ingest_class_nodes(processed_symbols.get('CLASS_STRUCTURE', []), neo4j_mgr)
        self._ingest_function_nodes(processed_symbols.get('FUNCTION', []), neo4j_mgr)
        self._ingest_method_nodes(processed_symbols.get('METHOD', []), neo4j_mgr)
        self._ingest_field_nodes([f for f in processed_symbols.get('FIELD', []) if 'parent_id' in f], neo4j_mgr)
        self._ingest_variable_nodes(processed_symbols.get('VARIABLE', []), neo4j_mgr)

        # --- Phase 3: Relationship Ingestion ---
        logger.info("Phase 3: Ingesting all relationships...")

        # Consolidate and ingest all relationships derived from parent IDs

        # Firstly, collect all relationships derived from parent IDs for SCOPE_CONTAINS and HAS_NESTED
        # Temporarily create a quick lookup for node labels by ID for efficient filtering
        id_to_label_map = {
            data['id']: data['node_label']
            for symbol_list in processed_symbols.values()
            for data in symbol_list
        }

        scope_contains_relations = []
        has_nested_relations = []
        for symbol_list in processed_symbols.values():
            for symbol_data in symbol_list:
                # Collect data for SCOPE_CONTAINS (parent is a Namespace)
                if "namespace_id" in symbol_data:
                    scope_contains_relations.append({
                        "parent_id": symbol_data["namespace_id"],
                        "child_id": symbol_data["id"]
                    })

                # Collect and pre-filter data for HAS_NESTED
                if "parent_id" in symbol_data:
                    parent_id = symbol_data["parent_id"]
                    parent_label = id_to_label_map.get(parent_id)
                    child_label = symbol_data["node_label"]
                    child_id = symbol_data["id"]
                            
                    is_valid_parent = parent_label in ('CLASS_STRUCTURE', 'DATA_STRUCTURE','FUNCTION', 'METHOD')
                    is_valid_child = child_label in ('CLASS_STRUCTURE', 'DATA_STRUCTURE', 'FUNCTION')

                    if parent_label and is_valid_parent and is_valid_child:
                        has_nested_relations.append({
                            "parent_id": parent_id,
                            "child_id": child_id
                        })

        self._ingest_scope_contains_relationships(scope_contains_relations, neo4j_mgr)
        self._ingest_nesting_relationships(has_nested_relations, neo4j_mgr)
        del id_to_label_map

        # Ingest other relationships
        self._ingest_file_declarations(processed_symbols.get('NAMESPACE', []), neo4j_mgr)

        defines_function_list = [d for d in processed_symbols.get('FUNCTION', []) if 'file_path' in d]
        defines_variable_list = [d for d in processed_symbols.get('VARIABLE', []) if 'file_path' in d]
        defines_data_structure_list = [d for d in processed_symbols.get('DATA_STRUCTURE', []) if 'file_path' in d]
        defines_class_list = [d for d in processed_symbols.get('CLASS_STRUCTURE', []) if 'file_path' in d]

        if defines_generation_strategy == "unwind-sequential":
            self._ingest_defines_relationships_unwind_sequential(defines_function_list, defines_variable_list, defines_data_structure_list, defines_class_list, neo4j_mgr)
        elif defines_generation_strategy == "isolated-parallel":
            self._ingest_defines_relationships_isolated_parallel(defines_function_list, defines_variable_list, defines_data_structure_list, defines_class_list, neo4j_mgr)
        else: # batched-parallel
            self._ingest_defines_relationships_batched_parallel(defines_function_list, defines_variable_list, defines_data_structure_list, defines_class_list, neo4j_mgr)

        self._ingest_has_field_relationships([f for f in processed_symbols.get('FIELD', []) if 'parent_id' in f], neo4j_mgr)
        self._ingest_has_method_relationships([m for m in processed_symbols.get('METHOD', []) if 'parent_id' in m], neo4j_mgr)

        self._ingest_inheritance_relationships(symbol_parser.inheritance_relations, neo4j_mgr)
        self._ingest_override_relationships(symbol_parser.override_relations, neo4j_mgr)

        del processed_symbols
        gc.collect()

    def _process_and_filter_symbols(self, symbols: Dict[str, Symbol], qualified_namespace_to_id: Dict[str, str]) -> Dict[str, List[Dict]]:
        processed_symbols = defaultdict(list)
        logger.info("Processing symbols for ingestion...")
        for sym in tqdm(symbols.values(), desc="Processing and grouping symbols by kind"):
            data = self.process_symbol(sym, symbols, qualified_namespace_to_id)
            if data and 'node_label' in data:
                processed_symbols[data['node_label']].append(data)
        
        return processed_symbols

    def ingest_symbols_and_relationships(self, symbol_parser: SymbolParser, neo4j_mgr: Neo4jManager, defines_generation_strategy: str = "batched-parallel"):
        # --- Phase 1: Discovery and Map Building ---
        logger.info("Phase 1: Building scope maps and processing symbols...")
        qualified_namespace_to_id = self._build_scope_maps(symbol_parser.symbols)
        processed_symbols = self._process_and_filter_symbols(symbol_parser.symbols, qualified_namespace_to_id)

        # --- Phase 2: Node Ingestion ---
        logger.info("Phase 2: Ingesting all nodes...")
        self._ingest_namespace_nodes(processed_symbols.get('NAMESPACE', []), neo4j_mgr)
        self._ingest_data_structure_nodes(processed_symbols.get('DATA_STRUCTURE', []), neo4j_mgr)
        self._ingest_class_nodes(processed_symbols.get('CLASS_STRUCTURE', []), neo4j_mgr)
        self._ingest_function_nodes(processed_symbols.get('FUNCTION', []), neo4j_mgr)
        self._ingest_method_nodes(processed_symbols.get('METHOD', []), neo4j_mgr)
        self._ingest_field_nodes([f for f in processed_symbols.get('FIELD', []) if 'parent_id' in f], neo4j_mgr)
        self._ingest_variable_nodes(processed_symbols.get('VARIABLE', []), neo4j_mgr)

        # --- Phase 3: Relationship Ingestion ---
        logger.info("Phase 3: Ingesting all relationships...")
        
        # Consolidate and ingest all SCOPE_CONTAINS relationships
        scope_contains_relations = []
        for symbol_list in processed_symbols.values():
            for symbol_data in symbol_list:
                if "namespace_id" in symbol_data:
                    scope_contains_relations.append({
                        "parent_id": symbol_data["namespace_id"],
                        "child_id": symbol_data["id"]
                    })
        self._ingest_scope_contains_relationships(scope_contains_relations, neo4j_mgr)

        # Ingest other relationships
        self._ingest_file_declarations(processed_symbols.get('NAMESPACE', []), neo4j_mgr)

        defines_function_list = [d for d in processed_symbols.get('FUNCTION', []) if 'file_path' in d]
        defines_variable_list = [d for d in processed_symbols.get('VARIABLE', []) if 'file_path' in d]
        defines_data_structure_list = [d for d in processed_symbols.get('DATA_STRUCTURE', []) if 'file_path' in d]
        defines_class_list = [d for d in processed_symbols.get('CLASS_STRUCTURE', []) if 'file_path' in d]

        if defines_generation_strategy == "unwind-sequential":
            self._ingest_defines_relationships_unwind_sequential(defines_function_list, defines_variable_list, defines_data_structure_list, defines_class_list, neo4j_mgr)
        elif defines_generation_strategy == "isolated-parallel":
            self._ingest_defines_relationships_isolated_parallel(defines_function_list, defines_variable_list, defines_data_structure_list, defines_class_list, neo4j_mgr)
        else: # batched-parallel
            self._ingest_defines_relationships_batched_parallel(defines_function_list, defines_variable_list, defines_data_structure_list, defines_class_list, neo4j_mgr)

        self._ingest_has_field_relationships([f for f in processed_symbols.get('FIELD', []) if 'parent_id' in f], neo4j_mgr)
        self._ingest_has_method_relationships([m for m in processed_symbols.get('METHOD', []) if 'parent_id' in m], neo4j_mgr)

        self._ingest_inheritance_relationships(symbol_parser.inheritance_relations, neo4j_mgr)
        self._ingest_override_relationships(symbol_parser.override_relations, neo4j_mgr)

        del processed_symbols
        gc.collect()

    def _ingest_function_nodes(self, function_data_list: List[Dict], neo4j_mgr: Neo4jManager):
        if not function_data_list:
            return
        logger.info(f"Creating {len(function_data_list)} FUNCTION nodes in batches (1 batch = {self.ingest_batch_size} nodes)...")
        total_nodes_created = 0
        total_properties_set = 0
        for i in tqdm(range(0, len(function_data_list), self.ingest_batch_size), desc="Ingesting FUNCTION nodes"):
            batch = function_data_list[i:i + self.ingest_batch_size]
            function_merge_query = """
            UNWIND $function_data AS data
            MERGE (n:FUNCTION {id: data.id})
            ON CREATE SET n += data
            ON MATCH SET n += data
            """
            all_counters = neo4j_mgr.process_batch([(function_merge_query, {"function_data": batch})])
            for counters in all_counters:
                total_nodes_created += counters.nodes_created
                total_properties_set += counters.properties_set
        logger.info(f"  Total FUNCTION nodes created: {total_nodes_created}, properties set: {total_properties_set}")

    def _ingest_data_structure_nodes(self, data_structure_data_list: List[Dict], neo4j_mgr: Neo4jManager):
        if not data_structure_data_list:
            return
        logger.info(f"Creating {len(data_structure_data_list)} DATA_STRUCTURE nodes in batches (1 batch = {self.ingest_batch_size} nodes)...")
        total_nodes_created = 0
        total_properties_set = 0
        for i in tqdm(range(0, len(data_structure_data_list), self.ingest_batch_size), desc="Ingesting DATA_STRUCTURE nodes"):
            batch = data_structure_data_list[i:i + self.ingest_batch_size]
            data_structure_merge_query = """
            UNWIND $data_structure_data AS data
            MERGE (n:DATA_STRUCTURE {id: data.id})
            ON CREATE SET n += data
            ON MATCH SET n += data
            """
            all_counters = neo4j_mgr.process_batch([(data_structure_merge_query, {"data_structure_data": batch})])
            for counters in all_counters:
                total_nodes_created += counters.nodes_created
                total_properties_set += counters.properties_set
        logger.info(f"  Total DATA_STRUCTURE nodes created: {total_nodes_created}, properties set: {total_properties_set}")

    def _ingest_field_nodes(self, field_data_list: List[Dict], neo4j_mgr: Neo4jManager):
        if not field_data_list:
            return
        logger.info(f"Creating {len(field_data_list)} FIELD nodes in batches (1 batch = {self.ingest_batch_size} nodes)...")
        total_nodes_created = 0
        total_properties_set = 0
        for i in tqdm(range(0, len(field_data_list), self.ingest_batch_size), desc="Ingesting FIELD nodes"):
            batch = field_data_list[i:i + self.ingest_batch_size]
            field_merge_query = """
            UNWIND $field_data AS data
            MERGE (n:FIELD {id: data.id})
            SET n += apoc.map.removeKey(data, 'parent_id')
            """
            all_counters = neo4j_mgr.process_batch([(field_merge_query, {"field_data": batch})])
            for counters in all_counters:
                total_nodes_created += counters.nodes_created
                total_properties_set += counters.properties_set
        logger.info(f"  Total FIELD nodes created: {total_nodes_created}, properties set: {total_properties_set}")

    def _ingest_has_field_relationships(self, field_data_list: List[Dict], neo4j_mgr: Neo4jManager):
        if not field_data_list:
            return
        logger.info(f"Creating {len(field_data_list)} HAS_FIELD relationships in batches...")
        query = """
        UNWIND $field_data AS data
        MATCH (parent) WHERE (parent:DATA_STRUCTURE OR parent:CLASS_STRUCTURE) AND parent.id = data.parent_id
        MATCH (child:FIELD {id: data.id})
        MERGE (parent)-[:HAS_FIELD]->(child)
        """
        total_rels_created = 0
        for i in tqdm(range(0, len(field_data_list), self.ingest_batch_size), desc="Ingesting HAS_FIELD relationships"):
            batch = field_data_list[i:i + self.ingest_batch_size]
            counters = neo4j_mgr.execute_autocommit_query(query, {"field_data": batch})
            total_rels_created += counters.relationships_created
        logger.info(f"  Total HAS_FIELD relationships created: {total_rels_created}")

    def _ingest_class_nodes(self, class_data_list: List[Dict], neo4j_mgr: Neo4jManager):
        if not class_data_list:
            return
        logger.info(f"Creating {len(class_data_list)} CLASS_STRUCTURE nodes in batches (1 batch = {self.ingest_batch_size} nodes)...")
        total_nodes_created = 0
        total_properties_set = 0
        for i in tqdm(range(0, len(class_data_list), self.ingest_batch_size), desc="Ingesting CLASS_STRUCTURE nodes"):
            batch = class_data_list[i:i + self.ingest_batch_size]
            class_merge_query = """
            UNWIND $class_data AS data
            MERGE (n:CLASS_STRUCTURE {id: data.id})
            ON CREATE SET n += data
            ON MATCH SET n += data
            """
            all_counters = neo4j_mgr.process_batch([(class_merge_query, {"class_data": batch})])
            for counters in all_counters:
                total_nodes_created += counters.nodes_created
                total_properties_set += counters.properties_set
        logger.info(f"  Total CLASS_STRUCTURE nodes created: {total_nodes_created}, properties set: {total_properties_set}")

    def _ingest_method_nodes(self, method_data_list: List[Dict], neo4j_mgr: Neo4jManager):
        if not method_data_list:
            return
        logger.info(f"Creating {len(method_data_list)} METHOD nodes in batches (1 batch = {self.ingest_batch_size} nodes)...")
        total_nodes_created = 0
        total_properties_set = 0
        for i in tqdm(range(0, len(method_data_list), self.ingest_batch_size), desc="Ingesting METHOD nodes"):
            batch = method_data_list[i:i + self.ingest_batch_size]
            method_merge_query = """
            UNWIND $method_data AS data
            MERGE (n:METHOD {id: data.id})
            SET n += apoc.map.removeKey(data, 'parent_id')
            """
            all_counters = neo4j_mgr.process_batch([(method_merge_query, {"method_data": batch})])
            for counters in all_counters:
                total_nodes_created += counters.nodes_created
                total_properties_set += counters.properties_set
        logger.info(f"  Total METHOD nodes created: {total_nodes_created}, properties set: {total_properties_set}")

    def _ingest_variable_nodes(self, variable_data_list: List[Dict], neo4j_mgr: Neo4jManager):
        if not variable_data_list:
            return
        logger.info(f"Creating {len(variable_data_list)} VARIABLE nodes in batches (1 batch = {self.ingest_batch_size} nodes)...")
        total_nodes_created = 0
        total_properties_set = 0
        for i in tqdm(range(0, len(variable_data_list), self.ingest_batch_size), desc="Ingesting VARIABLE nodes"):
            batch = variable_data_list[i:i + self.ingest_batch_size]
            variable_merge_query = """
            UNWIND $variable_data AS data
            MERGE (n:VARIABLE {id: data.id})
            ON CREATE SET n += data
            ON MATCH SET n += data
            """
            all_counters = neo4j_mgr.process_batch([(variable_merge_query, {"variable_data": batch})])
            for counters in all_counters:
                total_nodes_created += counters.nodes_created
                total_properties_set += counters.properties_set
        logger.info(f"  Total VARIABLE nodes created: {total_nodes_created}, properties set: {total_properties_set}")

    def _ingest_has_method_relationships(self, method_data_list: List[Dict], neo4j_mgr: Neo4jManager):
        if not method_data_list:
            return
        logger.info(f"Creating {len(method_data_list)} HAS_METHOD relationships in batches...")
        query = """
        UNWIND $method_data AS data
        MATCH (parent:CLASS_STRUCTURE {id: data.parent_id})
        MATCH (child:METHOD {id: data.id})
        MERGE (parent)-[:HAS_METHOD]->(child)
        """
        total_rels_created = 0
        for i in tqdm(range(0, len(method_data_list), self.ingest_batch_size), desc="Ingesting HAS_METHOD relationships"):
            batch = method_data_list[i:i + self.ingest_batch_size]
            counters = neo4j_mgr.execute_autocommit_query(query, {"method_data": batch})
            total_rels_created += counters.relationships_created
        logger.info(f"  Total HAS_METHOD relationships created: {total_rels_created}")

    def _get_defines_stats(self, defines_list: List[Dict]) -> str:
        from collections import Counter
        kind_counts = Counter(d.get('kind', 'Unknown') for d in defines_list)
        return ", ".join(f"{kind}: {count}" for kind, count in sorted(kind_counts.items()))

    def _ingest_defines_relationships_batched_parallel(self, defines_function_list: List[Dict], defines_variable_list: List[Dict], defines_data_structure_list: List[Dict], defines_class_list: List[Dict], neo4j_mgr: Neo4jManager):
        # Methods and Fields are defined by classes, not files
        all_defines_list = defines_function_list + defines_variable_list + defines_data_structure_list + defines_class_list
        if not all_defines_list:
            return

        logger.info(
            f"Found {len(all_defines_list)} potential DEFINES relationships. "
            f"Breakdown by kind: {self._get_defines_stats(all_defines_list)}"
        )
        logger.info("Creating relationships using batched parallel MERGE...")

        # Ingest FUNCTION DEFINES relationships
        total_rels_created = 0
        total_rels_merged = 0
        if defines_function_list:
            logger.info(f"  Ingesting {len(defines_function_list)} FUNCTION DEFINES relationships in batches (1 batch = {self.ingest_batch_size} relationships)...")
            for i in tqdm(range(0, len(defines_function_list), self.ingest_batch_size), desc="DEFINES (Functions)"):
                batch = defines_function_list[i:i + self.ingest_batch_size]
                defines_rel_query = """
                CALL apoc.periodic.iterate(
                    "UNWIND $defines_data AS data RETURN data",
                    "MATCH (f:FILE {path: data.file_path}) MATCH (n:FUNCTION {id: data.id}) MERGE (f)-[:DEFINES]->(n)",
                    {batchSize: $cypher_tx_size, parallel: true, params: {defines_data: $defines_data}}
                )
                YIELD updateStatistics
                RETURN
                    sum(updateStatistics.relationshipsCreated) AS totalRelsCreated,
                    sum(updateStatistics.relationshipsUpdated) AS totalRelsMerged
                """
                results = neo4j_mgr.execute_query_and_return_records(
                    defines_rel_query,
                    {"defines_data": batch, "cypher_tx_size": self.cypher_tx_size}
                )
                if results and len(results) > 0:
                    total_rels_created += results[0].get("totalRelsCreated", 0)
                    total_rels_merged += results[0].get("totalRelsMerged", 0)
            logger.info(f"  Total DEFINES FUNCTIONS relationships created: {total_rels_created}, merged: {total_rels_merged}")

        # Ingest DATA_STRUCTURE DEFINES relationships
        total_rels_created = 0
        total_rels_merged = 0
        if defines_data_structure_list:
            logger.info(f"  Ingesting {len(defines_data_structure_list)} DATA_STRUCTURE DEFINES relationships in batches (1 batch = {self.ingest_batch_size} relationships)...")
            for i in tqdm(range(0, len(defines_data_structure_list), self.ingest_batch_size), desc="DEFINES (Data Structures)"):
                batch = defines_data_structure_list[i:i + self.ingest_batch_size]
                defines_rel_query = """
                CALL apoc.periodic.iterate(
                    "UNWIND $defines_data AS data RETURN data",
                    "MATCH (f:FILE {path: data.file_path}) MATCH (n:DATA_STRUCTURE {id: data.id}) MERGE (f)-[:DEFINES]->(n)",
                    {batchSize: $cypher_tx_size, parallel: true, params: {defines_data: $defines_data}}
                )
                YIELD updateStatistics
                RETURN
                    sum(updateStatistics.relationshipsCreated) AS totalRelsCreated,
                    sum(updateStatistics.relationshipsUpdated) AS totalRelsMerged
                """
                results = neo4j_mgr.execute_query_and_return_records(
                    defines_rel_query,
                    {"defines_data": batch, "cypher_tx_size": self.cypher_tx_size}
                )
                if results and len(results) > 0:
                    total_rels_created += results[0].get("totalRelsCreated", 0)
                    total_rels_merged += results[0].get("totalRelsMerged", 0)
            logger.info(f"  Total DEFINES DATA_STRUCTURE relationships created: {total_rels_created}, merged: {total_rels_merged}")

        # Ingest CLASS_STRUCTURE DEFINES relationships
        total_rels_created = 0
        total_rels_merged = 0
        if defines_class_list:
            logger.info(f"  Ingesting {len(defines_class_list)} CLASS_STRUCTURE DEFINES relationships in batches (1 batch = {self.ingest_batch_size} relationships)...")
            for i in tqdm(range(0, len(defines_class_list), self.ingest_batch_size), desc="DEFINES (Class Structures)"):
                batch = defines_class_list[i:i + self.ingest_batch_size]
                defines_rel_query = """
                CALL apoc.periodic.iterate(
                    "UNWIND $defines_data AS data RETURN data",
                    "MATCH (f:FILE {path: data.file_path}) MATCH (n:CLASS_STRUCTURE {id: data.id}) MERGE (f)-[:DEFINES]->(n)",
                    {batchSize: $cypher_tx_size, parallel: true, params: {defines_data: $defines_data}}
                )
                YIELD updateStatistics
                RETURN
                    sum(updateStatistics.relationshipsCreated) AS totalRelsCreated,
                    sum(updateStatistics.relationshipsUpdated) AS totalRelsMerged
                """
                results = neo4j_mgr.execute_query_and_return_records(
                    defines_rel_query,
                    {"defines_data": batch, "cypher_tx_size": self.cypher_tx_size}
                )
                if results and len(results) > 0:
                    total_rels_created += results[0].get("totalRelsCreated", 0)
                    total_rels_merged += results[0].get("totalRelsMerged", 0)
            logger.info(f"  Total DEFINES CLASS_STRUCTURE relationships created: {total_rels_created}, merged: {total_rels_merged}")

        # Ingest VARIABLE DEFINES relationships
        total_rels_created = 0
        total_rels_merged = 0
        if defines_variable_list:
            logger.info(f"  Ingesting {len(defines_variable_list)} VARIABLE DEFINES relationships in batches (1 batch = {self.ingest_batch_size} relationships)...")
            for i in tqdm(range(0, len(defines_variable_list), self.ingest_batch_size), desc="DEFINES (Variables)"):
                batch = defines_variable_list[i:i + self.ingest_batch_size]
                defines_rel_query = """
                CALL apoc.periodic.iterate(
                    "UNWIND $defines_data AS data RETURN data",
                    "MATCH (f:FILE {path: data.file_path}) MATCH (n:VARIABLE {id: data.id}) MERGE (f)-[:DEFINES]->(n)",
                    {batchSize: $cypher_tx_size, parallel: true, params: {defines_data: $defines_data}}
                )
                YIELD updateStatistics
                RETURN
                    sum(updateStatistics.relationshipsCreated) AS totalRelsCreated,
                    sum(updateStatistics.relationshipsUpdated) AS totalRelsMerged
                """
                results = neo4j_mgr.execute_query_and_return_records(
                    defines_rel_query,
                    {"defines_data": batch, "cypher_tx_size": self.cypher_tx_size}
                )
                if results and len(results) > 0:
                    total_rels_created += results[0].get("totalRelsCreated", 0)
                    total_rels_merged += results[0].get("totalRelsMerged", 0)
            logger.info(f"  Total DEFINES VARIABLE relationships created: {total_rels_created}, merged: {total_rels_merged}")

        logger.info("Finished DEFINES relationship ingestion.")

    def _ingest_defines_relationships_isolated_parallel(self, defines_function_list: List[Dict], defines_variable_list: List[Dict], defines_data_structure_list: List[Dict], defines_class_list: List[Dict], neo4j_mgr: Neo4jManager):
        # Methods and Fields are defined by classes, not files
        all_defines_list = defines_function_list + defines_variable_list + defines_data_structure_list + defines_class_list
        if not all_defines_list:
            return

        logger.info(
            f"Found {len(all_defines_list)} potential DEFINES relationships. "
            f"Breakdown by kind: {self._get_defines_stats(all_defines_list)}"
        )
        
        logger.info("Grouping relationships by file for deadlock-safe parallel ingestion...")

        # Process FUNCTION DEFINES relationships
        if defines_function_list:
            logger.info(f"  Ingesting {len(defines_function_list)} FUNCTION DEFINES relationships...")
            grouped_by_file_functions = defaultdict(list)
            for item in defines_function_list:
                if 'file_path' in item:
                    grouped_by_file_functions[item['file_path']].append(item)
            self._process_grouped_defines_isolated_parallel(grouped_by_file_functions, neo4j_mgr, ":FUNCTION")

        # Process DATA_STRUCTURE DEFINES relationships
        if defines_data_structure_list:
            logger.info(f"  Ingesting {len(defines_data_structure_list)} DATA_STRUCTURE DEFINES relationships...")
            grouped_by_file_datastructures = defaultdict(list)
            for item in defines_data_structure_list:
                if 'file_path' in item:
                    grouped_by_file_datastructures[item['file_path']].append(item)
            self._process_grouped_defines_isolated_parallel(grouped_by_file_datastructures, neo4j_mgr, ":DATA_STRUCTURE")

        # Process CLASS_STRUCTURE DEFINES relationships
        if defines_class_list:
            logger.info(f"  Ingesting {len(defines_class_list)} CLASS_STRUCTURE DEFINES relationships...")
            grouped_by_file_classes = defaultdict(list)
            for item in defines_class_list:
                if 'file_path' in item:
                    grouped_by_file_classes[item['file_path']].append(item)
            self._process_grouped_defines_isolated_parallel(grouped_by_file_classes, neo4j_mgr, ":CLASS_STRUCTURE")

        # Process VARIABLE DEFINES relationships
        if defines_variable_list:
            logger.info(f"  Ingesting {len(defines_variable_list)} VARIABLE DEFINES relationships...")
            grouped_by_file_variables = defaultdict(list)
            for item in defines_variable_list:
                if 'file_path' in item:
                    grouped_by_file_variables[item['file_path']].append(item)
            self._process_grouped_defines_isolated_parallel(grouped_by_file_variables, neo4j_mgr, ":VARIABLE")

        logger.info("Finished DEFINES relationship ingestion.")

    def _process_grouped_defines_isolated_parallel(self, grouped_by_file: Dict[str, List[Dict]], neo4j_mgr: Neo4jManager, node_label_filter: str):
        list_of_groups = list(grouped_by_file.values())
        if not list_of_groups:
            return

        total_rels = sum(len(group) for group in list_of_groups)
        num_groups = len(list_of_groups)
        avg_group_size = total_rels / num_groups if num_groups > 0 else 1
        safe_avg_group_size = max(1, avg_group_size)

        num_groups_per_tx = math.ceil(self.cypher_tx_size / safe_avg_group_size)
        num_groups_per_query = math.ceil(self.ingest_batch_size / safe_avg_group_size)
        
        final_groups_per_tx = max(1, num_groups_per_tx)
        final_groups_per_query = max(1, num_groups_per_query)

        logger.info(f"  Avg rels/file: {avg_group_size:.2f}. Targeting ~{self.ingest_batch_size} rels/submission and ~{self.cypher_tx_size} rels/tx.")
        logger.info(f"  Submitting {final_groups_per_query} file-groups per query, with {final_groups_per_tx} file-groups per server tx.")
        total_rels_created = 0
        total_rels_merged = 0

        for i in tqdm(range(0, len(list_of_groups), final_groups_per_query), desc=f"DEFINES ({node_label_filter.strip(':')})"):
            query_batch = list_of_groups[i:i + final_groups_per_query]

            defines_rel_query = f"""
            CALL apoc.periodic.iterate(
                "UNWIND $groups AS group RETURN group",
                "UNWIND group AS data MATCH (f:FILE {{path: data.file_path}}) MATCH (n{node_label_filter} {{id: data.id}}) MERGE (f)-[:DEFINES]->(n)",
                {{ batchSize: $batch_size, parallel: true, params: {{ groups: $groups }} }}
            ) 
            YIELD updateStatistics
            RETURN
                sum(updateStatistics.relationshipsCreated) AS totalRelsCreated,
                sum(updateStatistics.relationshipsUpdated) AS totalRelsMerged
            """
            results = neo4j_mgr.execute_query_and_return_records(
                defines_rel_query,
                {"groups": query_batch, "batch_size": final_groups_per_tx}
            )
            if results and len(results) > 0:
                total_rels_created += results[0].get("totalRelsCreated", 0)
                total_rels_merged += results[0].get("totalRelsMerged", 0)

        logger.info(f"  Total DEFINES {node_label_filter} relationships created: {total_rels_created}, merged: {total_rels_merged}")

    def _ingest_defines_relationships_unwind_sequential(self, defines_function_list: List[Dict], defines_variable_list: List[Dict], defines_data_structure_list: List[Dict], defines_class_list: List[Dict], neo4j_mgr: Neo4jManager):
        # Methods and Fields are defined by classes, not files
        all_defines_list = defines_function_list + defines_variable_list + defines_data_structure_list + defines_class_list
        if not all_defines_list:
            return

        logger.info(
            f"Found {len(all_defines_list)} potential DEFINES relationships. "
            f"Breakdown by kind: {self._get_defines_stats(all_defines_list)}"
        )
        logger.info("Creating relationships in batches using sequential UNWIND MERGE...")

        # Ingest FUNCTION DEFINES relationships
        total_rels_created_func = 0
        if defines_function_list:
            logger.info(f"  Ingesting {len(defines_function_list)} FUNCTION DEFINES relationships in batches (1 batch = {self.ingest_batch_size} relationships)...")
            for i in tqdm(range(0, len(defines_function_list), self.ingest_batch_size), desc="DEFINES (Functions, sequential)"):
                batch = defines_function_list[i:i + self.ingest_batch_size]
                defines_rel_query = """
                UNWIND $defines_data AS data
                MATCH (f:FILE {path: data.file_path})
                MATCH (n:FUNCTION {id: data.id})
                MERGE (f)-[:DEFINES]->(n)
                """
                counters = neo4j_mgr.execute_autocommit_query(
                    defines_rel_query,
                    {"defines_data": batch}
                )
                total_rels_created_func += counters.relationships_created
            logger.info(f"  Total FUNCTION DEFINES relationships created: {total_rels_created_func}")

        # Ingest DATA_STRUCTURE DEFINES relationships
        total_rels_created_ds = 0
        if defines_data_structure_list:
            logger.info(f"  Ingesting {len(defines_data_structure_list)} DATA_STRUCTURE DEFINES relationships in batches (1 batch = {self.ingest_batch_size} relationships)...")
            for i in tqdm(range(0, len(defines_data_structure_list), self.ingest_batch_size), desc="DEFINES (Data Structures, sequential)"):
                batch = defines_data_structure_list[i:i + self.ingest_batch_size]
                defines_rel_query = """
                UNWIND $defines_data AS data
                MATCH (f:FILE {path: data.file_path})
                MATCH (n:DATA_STRUCTURE {id: data.id})
                MERGE (f)-[:DEFINES]->(n)
                """
                counters = neo4j_mgr.execute_autocommit_query(
                    defines_rel_query,
                    {"defines_data": batch}
                )
                total_rels_created_ds += counters.relationships_created
            logger.info(f"  Total DATA_STRUCTURE DEFINES relationships created: {total_rels_created_ds}")

        # Ingest CLASS_STRUCTURE DEFINES relationships
        total_rels_created_class = 0
        if defines_class_list:
            logger.info(f"  Ingesting {len(defines_class_list)} CLASS_STRUCTURE DEFINES relationships in batches (1 batch = {self.ingest_batch_size} relationships)...")
            for i in tqdm(range(0, len(defines_class_list), self.ingest_batch_size), desc="DEFINES (Class Structures, sequential)"):
                batch = defines_class_list[i:i + self.ingest_batch_size]
                defines_rel_query = """
                UNWIND $defines_data AS data
                MATCH (f:FILE {path: data.file_path})
                MATCH (n:CLASS_STRUCTURE {id: data.id})
                MERGE (f)-[:DEFINES]->(n)
                """
                counters = neo4j_mgr.execute_autocommit_query(
                    defines_rel_query,
                    {"defines_data": batch}
                )
                total_rels_created_class += counters.relationships_created
            logger.info(f"  Total CLASS_STRUCTURE DEFINES relationships created: {total_rels_created_class}")

        # Ingest VARIABLE DEFINES relationships
        total_rels_created_var = 0
        if defines_variable_list:
            logger.info(f"  Ingesting {len(defines_variable_list)} VARIABLE DEFINES relationships in batches (1 batch = {self.ingest_batch_size} relationships)...")
            for i in tqdm(range(0, len(defines_variable_list), self.ingest_batch_size), desc="DEFINES (Variables, sequential)"):
                batch = defines_variable_list[i:i + self.ingest_batch_size]
                defines_rel_query = """
                UNWIND $defines_data AS data
                MATCH (f:FILE {path: data.file_path})
                MATCH (n:VARIABLE {id: data.id})
                MERGE (f)-[:DEFINES]->(n)
                """
                counters = neo4j_mgr.execute_autocommit_query(
                    defines_rel_query,
                    {"defines_data": batch}
                )
                total_rels_created_var += counters.relationships_created
            logger.info(f"  Total VARIABLE DEFINES relationships created: {total_rels_created_var}")

        logger.info("Finished DEFINES relationship ingestion (sequential UNWIND MERGE).")

    def _ingest_inheritance_relationships(self, inheritance_relations: List[Tuple[str, str]], neo4j_mgr: Neo4jManager):
        if not inheritance_relations:
            return

        logger.info(f"Creating {len(inheritance_relations)} INHERITS relationships in batches...")
        query = """
        UNWIND $relations AS rel
        MATCH (child:CLASS_STRUCTURE {id: rel.object_id})
        MATCH (parent:CLASS_STRUCTURE {id: rel.subject_id})
        MERGE (child)-[:INHERITS]->(parent)
        """
        
        relations_data = [{"subject_id": subj, "object_id": obj} for subj, obj in inheritance_relations]
        total_rels_created = 0
        for i in tqdm(range(0, len(relations_data), self.ingest_batch_size), desc="Ingesting INHERITS relationships"):
            batch = relations_data[i:i + self.ingest_batch_size]
            counters = neo4j_mgr.execute_autocommit_query(query, {"relations": batch})
            total_rels_created += counters.relationships_created
        logger.info(f"  Total INHERITS relationships created: {total_rels_created}")

    def _ingest_override_relationships(self, override_relations: List[Tuple[str, str]], neo4j_mgr: Neo4jManager):
        if not override_relations:
            return

        logger.info(f"Creating {len(override_relations)} OVERRIDDEN_BY relationships in batches...")
        query = """
        UNWIND $relations AS rel
        MATCH (base_method:METHOD {id: rel.subject_id})
        MATCH (derived_method:METHOD {id: rel.object_id})
        MERGE (base_method)-[:OVERRIDDEN_BY]->(derived_method)
        """
        
        relations_data = [{"subject_id": subj, "object_id": obj} for subj, obj in override_relations]
        total_rels_created = 0
        for i in tqdm(range(0, len(relations_data), self.ingest_batch_size), desc="Ingesting OVERRIDDEN_BY relationships"):
            batch = relations_data[i:i + self.ingest_batch_size]
            counters = neo4j_mgr.execute_autocommit_query(query, {"relations": batch})
            total_rels_created += counters.relationships_created
        logger.info(f"  Total OVERRIDDEN_BY relationships created: {total_rels_created}")

    def _ingest_namespace_nodes(self, namespace_data_list: List[Dict], neo4j_mgr: Neo4jManager):
        if not namespace_data_list:
            return
        logger.info(f"Creating {len(namespace_data_list)} NAMESPACE nodes...")
        
        query = """
        UNWIND $ns_data AS data
        MERGE (n:NAMESPACE {id: data.id})
        SET n.qualified_name = data.qualified_name,
            n.name = data.name,
            n.path = data.path,
            n.name_location = data.name_location
        """
        counters = neo4j_mgr.execute_autocommit_query(query, {"ns_data": namespace_data_list})
        logger.info(f"  Total NAMESPACE nodes created/updated: {counters.nodes_created}")

    def _ingest_scope_contains_relationships(self, relations: List[Dict], neo4j_mgr: Neo4jManager):
        if not relations:
            return
        
        logger.info(f"Creating {len(relations)} SCOPE_CONTAINS relationships...")
        query = """
        UNWIND $relations AS rel
        MATCH (parent:NAMESPACE {id: rel.parent_id})
        MATCH (child) WHERE child.id = rel.child_id
        MERGE (parent)-[:SCOPE_CONTAINS]->(child)
        """
        total_rels_created = 0
        for i in tqdm(range(0, len(relations), self.ingest_batch_size), desc="Ingesting SCOPE_CONTAINS relationships"):
            batch = relations[i:i + self.ingest_batch_size]
            counters = neo4j_mgr.execute_autocommit_query(query, {"relations": batch})
            total_rels_created += counters.relationships_created
        logger.info(f"  Total SCOPE_CONTAINS relationships created: {total_rels_created}")

    def _ingest_file_declarations(self, namespace_data_list: List[Dict], neo4j_mgr: Neo4jManager):
        relations_data = [ns for ns in namespace_data_list if ns.get('path')]
        if not relations_data:
            return
        logger.info(f"Creating {len(relations_data)} FILE-[:DECLARES]->NAMESPACE relationships...")
        
        query = """
        UNWIND $relations AS rel
        MATCH (f:FILE {path: rel.path})
        MATCH (ns:NAMESPACE {id: rel.id})
        MERGE (f)-[:DECLARES]->(ns)
        """
        counters = neo4j_mgr.execute_autocommit_query(query, {"relations": relations_data})
        logger.info(f"  Total FILE-[:DECLARES]->NAMESPACE relationships created: {counters.relationships_created}")

    def _ingest_nesting_relationships(self, relations: List[Dict], neo4j_mgr: Neo4jManager):
        """
        Creates HAS_NESTED relationships for lexically nested structures.
        The list of relations is pre-filtered to ensure it only contains
        valid parent/child node types.
        """
        if not relations:
            return

        logger.info(f"Creating {len(relations)} HAS_NESTED relationships...")
        query = """
        UNWIND $relations AS rel
        MATCH (parent) WHERE parent.id = rel.parent_id
        MATCH (child) WHERE child.id = rel.child_id
        MERGE (parent)-[:HAS_NESTED]->(child)
        """
        total_rels_created = 0
        for i in tqdm(range(0, len(relations), self.ingest_batch_size), desc="Ingesting HAS_NESTED relationships"):
            batch = relations[i:i + self.ingest_batch_size]
            counters = neo4j_mgr.execute_autocommit_query(query, {"relations": batch})
            total_rels_created += counters.relationships_created

        logger.info(f"  Total HAS_NESTED relationships created: {total_rels_created}")

class PathProcessor:
    """Discovers and ingests file/folder structure into Neo4j."""
    def __init__(self, path_manager: PathManager, neo4j_mgr: Neo4jManager, log_batch_size: int = 1000, ingest_batch_size: int = 1000):
        self.path_manager, self.neo4j_mgr, self.log_batch_size, self.ingest_batch_size = path_manager, neo4j_mgr, log_batch_size, ingest_batch_size

    def _discover_paths_from_symbols(self, symbols: Dict[str, Symbol]) -> set:
        project_files = set()
        logger.info("Discovering file paths from symbols...")
        for sym in tqdm(symbols.values(), desc="Discovering paths from symbols"):
            for loc in [sym.definition, sym.declaration]:
                if loc and urlparse(loc.file_uri).scheme == 'file':
                    abs_path = unquote(urlparse(loc.file_uri).path)
                    if self.path_manager.is_within_project(abs_path):
                        relative_path = self.path_manager.uri_to_relative_path(loc.file_uri)
                        project_files.add(relative_path)
        logger.info(f"Discovered {len(project_files)} unique files from symbols.")
        return project_files

    def _discover_paths_from_includes(self, compilation_manager: CompilationManager) -> set:
        """Discovers all unique file paths from include relations."""
        include_files = set()
        logger.info("Discovering file paths from include relations...")
        include_relations = compilation_manager.get_include_relations()
        for including_abs, included_abs in tqdm(include_relations, desc="Discovering paths from includes"):
            for abs_path in [including_abs, included_abs]:
                if self.path_manager.is_within_project(abs_path):
                    # Use os.path.relpath for consistency with include_relation_provider
                    relative_path = os.path.relpath(abs_path, self.path_manager.project_path)
                    include_files.add(relative_path)
        logger.info(f"Discovered {len(include_files)} unique files from includes.")
        return include_files

    def _get_folders_from_files(self, project_files: set) -> set:
        """Extracts all unique parent folder paths from a set of file paths."""
        project_folders = set()
        for file_path in project_files:
            parent = Path(file_path).parent
            while str(parent) != '.' and str(parent) != '/':
                project_folders.add(str(parent))
                parent = parent.parent
        return project_folders

    def ingest_paths(self, symbols: Dict[str, Symbol], compilation_manager: CompilationManager):
        logger.info("Consolidating all unique file and folder paths...")
        paths_from_symbols = self._discover_paths_from_symbols(symbols)
        paths_from_includes = self._discover_paths_from_includes(compilation_manager)
        
        project_files = paths_from_symbols.union(paths_from_includes)
        project_folders = self._get_folders_from_files(project_files)
        
        logger.info(f"Consolidated to {len(project_files)} unique project files and {len(project_folders)} unique project folders.")
        
        folder_data_list = []
        sorted_folders = sorted(list(project_folders), key=lambda p: len(Path(p).parts))
        for folder_path in sorted_folders:
            parent_path = str(Path(folder_path).parent)
            if parent_path == '.':
                folder_data_list.append({
                    "path": folder_path,
                    "name": Path(folder_path).name,
                    "parent_path": self.path_manager.project_path,
                    "is_root": True
                })
            else:
                folder_data_list.append({
                    "path": folder_path,
                    "name": Path(folder_path).name,
                    "parent_path": parent_path,
                    "is_root": False
                })
        
        self._ingest_folder_nodes_and_relationships(folder_data_list)
        del folder_data_list
        gc.collect()

        file_data_list = []
        for file_path in project_files:
            parent_path = str(Path(file_path).parent)
            if parent_path == '.':
                file_data_list.append({
                    "path": file_path,
                    "name": Path(file_path).name,
                    "parent_path": self.path_manager.project_path,
                    "is_root": True
                })
            else:
                file_data_list.append({
                    "path": file_path,
                    "name": Path(file_path).name,
                    "parent_path": parent_path,
                    "is_root": False
                })
        
        self._ingest_file_nodes_and_relationships(file_data_list)
        del file_data_list
        del project_files, project_folders, sorted_folders
        gc.collect()

    def _ingest_folder_nodes_and_relationships(self, folder_data_list: List[Dict]):
        if not folder_data_list:
            return
        total_nodes_created = 0
        total_properties_set = 0
        total_rels_created = 0
        logger.info(f"Creating {len(folder_data_list)} folder nodes and relationships in batches...")
        for i in tqdm(range(0, len(folder_data_list), self.ingest_batch_size), desc="Ingesting FOLDER nodes"):
            batch = folder_data_list[i:i + self.ingest_batch_size]
            folder_merge_query = """
            UNWIND $folder_data AS data
            MERGE (f:FOLDER {path: data.path})
            ON CREATE SET f.name = data.name
            ON MATCH SET f.name = data.name
            """
            node_counters = self.neo4j_mgr.process_batch([(folder_merge_query, {"folder_data": batch})])
            for counters in node_counters:
                total_nodes_created += counters.nodes_created
                total_properties_set += counters.properties_set

            folder_rel_query = """
            UNWIND $folder_data AS data
            MATCH (child:FOLDER {path: data.path})
            WITH child, data
            MATCH (parent:FOLDER {path: data.parent_path})
            MERGE (parent)-[:CONTAINS]->(child)
            """
            rel_counters = self.neo4j_mgr.process_batch([(folder_rel_query, {"folder_data": batch})])
            for counters in rel_counters:
                total_rels_created += counters.relationships_created

            folder_rel_query = """
            UNWIND $folder_data AS data
            MATCH (child:FOLDER {path: data.path})
            WITH child, data
            MATCH (parent:PROJECT {path: data.parent_path})
            MERGE (parent)-[:CONTAINS]->(child)
            """
            rel_counters = self.neo4j_mgr.process_batch([(folder_rel_query, {"folder_data": batch})])
            for counters in rel_counters:
                total_rels_created += counters.relationships_created

        logger.info(f"  Total FOLDER nodes created: {total_nodes_created}, properties set: {total_properties_set}")
        logger.info(f"  Total CONTAINS relationships created for FOLDERs: {total_rels_created}")

    def _ingest_file_nodes_and_relationships(self, file_data_list: List[Dict]):
        if not file_data_list:
            return

        logger.info(f"Creating {len(file_data_list)} file nodes and relationships in batches...")
        total_nodes_created = 0
        total_properties_set = 0
        total_rels_created = 0

        for i in tqdm(range(0, len(file_data_list), self.ingest_batch_size), desc="Ingesting FILE nodes"):
            batch = file_data_list[i:i + self.ingest_batch_size]
            file_merge_query = """
            UNWIND $file_data AS data
            MERGE (f:FILE {path: data.path})
            ON CREATE SET f.name = data.name
            ON MATCH SET f.name = data.name
            """
            node_counters = self.neo4j_mgr.process_batch([(file_merge_query, {"file_data": batch})])
            for counters in node_counters:
                total_nodes_created += counters.nodes_created
                total_properties_set += counters.properties_set

            file_rel_query = """
            UNWIND $file_data AS data
            MATCH (child:FILE {path: data.path})
            WITH child, data
            MATCH (parent:FOLDER {path: data.parent_path})
            MERGE (parent)-[:CONTAINS]->(child)
            """
            rel_counters = self.neo4j_mgr.process_batch([(file_rel_query, {"file_data": batch})])
            for counters in rel_counters:
                total_rels_created += counters.relationships_created

            file_rel_query = """
            UNWIND $file_data AS data
            MATCH (child:FILE {path: data.path})
            WITH child, data
            MATCH (parent:PROJECT {path: data.parent_path})
            MERGE (parent)-[:CONTAINS]->(child)
            """
            rel_counters = self.neo4j_mgr.process_batch([(file_rel_query, {"file_data": batch})])
            for counters in rel_counters:
                total_rels_created += counters.relationships_created

        logger.info(f"  Total FILE nodes created: {total_nodes_created}, properties set: {total_properties_set}")
        logger.info(f"  Total CONTAINS relationships created for FILEs: {total_rels_created}")

import input_params

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    parser = argparse.ArgumentParser(description='Import Clangd index symbols and file structure into Neo4j.')

    # Add argument groups from the centralized module
    input_params.add_core_input_args(parser)
    input_params.add_worker_args(parser)
    input_params.add_batching_args(parser)
    input_params.add_ingestion_strategy_args(parser)
    input_params.add_source_parser_args(parser)

    args = parser.parse_args()
    
    # Resolve paths and convert back to strings
    args.index_file = str(args.index_file.resolve())
    args.project_path = str(args.project_path.resolve())

    # Set default for ingest_batch_size if not provided
    if args.ingest_batch_size is None:
        try:
            default_workers = math.ceil(os.cpu_count() / 2)
        except (NotImplementedError, TypeError):
            default_workers = 2
        args.ingest_batch_size = args.cypher_tx_size * (args.num_parse_workers or default_workers)

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
    compilation_manager.parse_folder(args.project_path, args.num_parse_workers)
    logger.info("--- Finished Phase 1 ---")

    # --- NEW: Phase 2: Create SourceSpanProvider adapter ---
    from source_span_provider import SourceSpanProvider
    logger.info("\n--- Starting Phase 2: Enriching Symbols with Spans ---")
    span_provider = SourceSpanProvider(symbol_parser=symbol_parser, compilation_manager=compilation_manager)
    span_provider.enrich_symbols_with_span()
    logger.info("--- Finished Phase 2 ---")
    
    path_manager = PathManager(args.project_path)
    with Neo4jManager() as neo4j_mgr:
        if not neo4j_mgr.check_connection(): return 1
        neo4j_mgr.reset_database()
        neo4j_mgr.update_project_node(path_manager.project_path, {})
        neo4j_mgr.create_constraints()
        
        logger.info("\n--- Starting Phase 3: Ingesting File & Folder Structure ---")
        path_processor = PathProcessor(path_manager, neo4j_mgr, args.log_batch_size, args.ingest_batch_size)
        path_processor.ingest_paths(symbol_parser.symbols, compilation_manager)
        del path_processor
        gc.collect()
        logger.info("--- Finished Phase 3 ---")

        logger.info("\n--- Starting Phase 4: Ingesting Symbol Definitions ---")
        symbol_processor = SymbolProcessor(
            path_manager,
            log_batch_size=args.log_batch_size,
            ingest_batch_size=args.ingest_batch_size,
            cypher_tx_size=args.cypher_tx_size
        )
        symbol_processor.ingest_symbols_and_relationships(symbol_parser, neo4j_mgr, args.defines_generation)
        
        del symbol_processor
        gc.collect()
        
        logger.info(f"\n✅ Done. Processed {len(symbol_parser.symbols)} symbols.")
        return 0

if __name__ == "__main__":
    sys.exit(main())
