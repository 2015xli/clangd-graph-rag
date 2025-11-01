#!/usr/bin/env python3
"""
This module processes an in-memory collection of clangd symbols to create
the file, folder, and symbol nodes in a Neo4j graph.
"""
import os
import sys
import argparse
import math
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
    
    def process_symbol(self, sym: Symbol, scope_name_to_id: Dict[str, str]) -> Optional[Dict]:
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

        primary_location = sym.definition or sym.declaration
        if primary_location:
            abs_file_path = unquote(urlparse(primary_location.file_uri).path)
            if self.path_manager.is_within_project(abs_file_path):
                symbol_data["path"] = self.path_manager.uri_to_relative_path(primary_location.file_uri)
            else:
                return None
            symbol_data["name_location"] = [primary_location.start_line, primary_location.start_column]

        if sym.kind == "Function":
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
        
        if sym.kind in ("Struct", "Union", "Enum", "Class"):
            if hasattr(sym, 'body_location') and sym.body_location:
                symbol_data["body_location"] = [
                    sym.body_location.start_line,
                    sym.body_location.start_column,
                    sym.body_location.end_line,
                    sym.body_location.end_column
                ]

        if sym.kind == "Field":
            parent_id = scope_name_to_id.get(sym.scope)
            if parent_id:
                symbol_data.update({
                    "type": sym.type,
                    "parent_id": parent_id
                })
            else:
                logger.debug(f"Could not find parent ID for field '{sym.name}' with scope '{sym.scope}'")

        if sym.definition:
            abs_file_path = unquote(urlparse(sym.definition.file_uri).path)
            if self.path_manager.is_within_project(abs_file_path):
                symbol_data["file_path"] = self.path_manager.uri_to_relative_path(sym.definition.file_uri)
        
        return symbol_data

    def _process_and_filter_symbols(self, symbols: Dict[str, Symbol], scope_name_to_id: Dict[str, str]) -> Dict[str, List[Dict]]:
        processed_symbols = defaultdict(list)
        logger.info("Processing symbols for ingestion...")
        for sym in tqdm(symbols.values(), desc="Processing and grouping symbols by kind"):
            data = self.process_symbol(sym, scope_name_to_id)
            if data and 'kind' in data:
                processed_symbols[data['kind']].append(data)
        
        processed_symbols['DATA_STRUCTURE'] = (
            processed_symbols.get('Struct', []) +
            processed_symbols.get('Class', []) +
            processed_symbols.get('Union', []) +
            processed_symbols.get('Enum', [])
        )
        return processed_symbols

    def ingest_symbols_and_relationships(self, symbols: Dict[str, Symbol], neo4j_mgr: Neo4jManager, defines_generation_strategy: str = "batched-parallel"):
        logger.info("Building lookup map from scope names to parent IDs...")
        scope_name_to_id = {}
        for sym in symbols.values():
            if sym.kind in ('Struct', 'Class', 'Union'):
                key = sym.scope + sym.name + '::'
                scope_name_to_id[key] = sym.id

        processed_symbols = self._process_and_filter_symbols(symbols, scope_name_to_id)

        function_data_list = processed_symbols.get('Function', [])
        data_structure_data_list = processed_symbols.get('DATA_STRUCTURE', [])
        field_data_list = [f for f in processed_symbols.get('Field', []) if 'parent_id' in f]

        self._ingest_function_nodes(function_data_list, neo4j_mgr)
        self._ingest_data_structure_nodes(data_structure_data_list, neo4j_mgr)
        self._ingest_field_nodes(field_data_list, neo4j_mgr)

        defines_function_list = [d for d in function_data_list if 'file_path' in d]
        defines_data_structure_list = [d for d in data_structure_data_list if 'file_path' in d]

        if defines_generation_strategy == "unwind-sequential":
            self._ingest_defines_relationships_unwind_sequential(defines_function_list, defines_data_structure_list, neo4j_mgr)
        elif defines_generation_strategy == "isolated-parallel":
            self._ingest_defines_relationships_isolated_parallel(defines_function_list, defines_data_structure_list, neo4j_mgr)
        elif defines_generation_strategy == "batched-parallel":
            self._ingest_defines_relationships_batched_parallel(defines_function_list, defines_data_structure_list, neo4j_mgr)
        else:
            logger.error(f"Unknown defines generation strategy: {defines_generation_strategy}. Defaulting to batched-parallel.")
            self._ingest_defines_relationships_batched_parallel(defines_function_list, defines_data_structure_list, neo4j_mgr)

        self._ingest_has_field_relationships(field_data_list, neo4j_mgr)

        del processed_symbols, function_data_list, data_structure_data_list, field_data_list
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
        MATCH (parent:DATA_STRUCTURE {id: data.parent_id})
        MATCH (child:FIELD {id: data.id})
        MERGE (parent)-[:HAS_FIELD]->(child)
        """
        total_rels_created = 0
        for i in tqdm(range(0, len(field_data_list), self.ingest_batch_size), desc="Ingesting HAS_FIELD relationships"):
            batch = field_data_list[i:i + self.ingest_batch_size]
            counters = neo4j_mgr.execute_autocommit_query(query, {"field_data": batch})
            total_rels_created += counters.relationships_created
        logger.info(f"  Total HAS_FIELD relationships created: {total_rels_created}")

    def _get_defines_stats(self, defines_list: List[Dict]) -> str:
        from collections import Counter
        kind_counts = Counter(d.get('kind', 'Unknown') for d in defines_list)
        return ", ".join(f"{kind}: {count}" for kind, count in sorted(kind_counts.items()))

    def _ingest_defines_relationships_batched_parallel(self, defines_function_list: List[Dict], defines_data_structure_list: List[Dict], neo4j_mgr: Neo4jManager):
        if not defines_function_list and not defines_data_structure_list:
            return

        logger.info(
            f"Found {len(defines_function_list) + len(defines_data_structure_list)} potential DEFINES relationships. "
            f"Breakdown by kind: {self._get_defines_stats(defines_function_list + defines_data_structure_list)}"
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

        logger.info("Finished DEFINES relationship ingestion.")

    def _ingest_defines_relationships_isolated_parallel(self, defines_function_list: List[Dict], defines_data_structure_list: List[Dict], neo4j_mgr: Neo4jManager):
        if not defines_function_list and not defines_data_structure_list:
            return

        logger.info(
            f"Found {len(defines_function_list) + len(defines_data_structure_list)} potential DEFINES relationships. "
            f"Breakdown by kind: {self._get_defines_stats(defines_function_list + defines_data_structure_list)}"
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

    def _ingest_defines_relationships_unwind_sequential(self, defines_function_list: List[Dict], defines_data_structure_list: List[Dict], neo4j_mgr: Neo4jManager):
        if not defines_function_list and not defines_data_structure_list:
            return

        logger.info(
            f"Found {len(defines_function_list) + len(defines_data_structure_list)} potential DEFINES relationships. "
            f"Breakdown by kind: {self._get_defines_stats(defines_function_list + defines_data_structure_list)}"
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
        logger.info("Finished DEFINES relationship ingestion (sequential UNWIND MERGE).")

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
    logger = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(description='Import Clangd index symbols and file structure into Neo4j.')

    # Add argument groups from the centralized module
    input_params.add_core_input_args(parser)
    input_params.add_worker_args(parser)
    input_params.add_batching_args(parser)
    input_params.add_ingestion_strategy_args(parser)

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
    
    path_manager = PathManager(args.project_path)
    with Neo4jManager() as neo4j_mgr:
        if not neo4j_mgr.check_connection(): return 1
        neo4j_mgr.reset_database()
        neo4j_mgr.update_project_node(path_manager.project_path, {})
        neo4j_mgr.create_constraints()
        
        logger.info("\n--- Starting Phase 1: Ingesting File & Folder Structure ---")
        path_processor = PathProcessor(path_manager, neo4j_mgr, args.log_batch_size, args.ingest_batch_size)
        path_processor.ingest_paths(symbol_parser.symbols)
        del path_processor
        gc.collect()
        logger.info("--- Finished Phase 1 ---")

        logger.info("\n--- Starting Phase 2: Ingesting Symbol Definitions ---")
        symbol_processor = SymbolProcessor(
            path_manager,
            log_batch_size=args.log_batch_size,
            ingest_batch_size=args.ingest_batch_size,
            cypher_tx_size=args.cypher_tx_size
        )
        symbol_processor.ingest_symbols_and_relationships(symbol_parser.symbols, neo4j_mgr, args.defines_generation)
        
        del symbol_processor
        gc.collect()
        
        logger.info(f"\n✅ Done. Processed {len(symbol_parser.symbols)} symbols.")
        return 0

if __name__ == "__main__":
    sys.exit(main())
