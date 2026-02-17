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
from clangd_index_yaml_parser import SymbolParser, Symbol
from compilation_manager import CompilationManager
from neo4j_manager import Neo4jManager, align_string
from path_processor import PathProcessor, PathManager

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

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

        for sym in tqdm(symbols.values(), desc=align_string("Building scope maps")):
            if sym.kind == 'Namespace':
                qualified_name = sym.scope + sym.name + '::'
                qualified_namespace_to_id[qualified_name] = sym.id

        logger.info(f"Built map for {len(qualified_namespace_to_id)} namespaces.")
        return qualified_namespace_to_id

    def process_symbol(self, sym: Symbol, qualified_namespace_to_id: Dict[str, str]) -> Optional[Dict]:
        """
        Processes a single Symbol object, enriching and converting it into a
        dictionary suitable for Neo4j ingestion.
        """
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
                if sym.kind != 'Namespace':
                    return None
            symbol_data["name_location"] = [primary_location.start_line, primary_location.start_column]

        if hasattr(sym, 'parent_id') and sym.parent_id:
            symbol_data["parent_id"] = sym.parent_id

        if namespace_id := qualified_namespace_to_id.get(sym.scope):
            symbol_data["namespace_id"] = namespace_id

        # --- Symbol Kind to Node Label Mapping ---
        if sym.kind == "Namespace":
            symbol_data["node_label"] = "NAMESPACE"
            symbol_data["qualified_name"] = sym.scope + sym.name + '::'
        elif sym.kind == "Function":
            symbol_data["node_label"] = "FUNCTION"
            symbol_data.update({"signature": sym.signature, "return_type": sym.return_type, "type": sym.type})
            if hasattr(sym, 'body_location') and sym.body_location:
                symbol_data["body_location"] = [sym.body_location.start_line, sym.body_location.start_column, sym.body_location.end_line, sym.body_location.end_column]
        elif sym.kind in ("InstanceMethod", "StaticMethod", "Constructor", "Destructor", "ConversionFunction"):
            symbol_data["node_label"] = "METHOD"
            symbol_data.update({"signature": sym.signature, "return_type": sym.return_type, "type": sym.type})
            if hasattr(sym, 'body_location') and sym.body_location:
                symbol_data["body_location"] = [sym.body_location.start_line, sym.body_location.start_column, sym.body_location.end_line, sym.body_location.end_column]
        elif sym.kind == "Class":
            symbol_data["node_label"] = "CLASS_STRUCTURE"
            if hasattr(sym, 'body_location') and sym.body_location:
                symbol_data["body_location"] = [sym.body_location.start_line, sym.body_location.start_column, sym.body_location.end_line, sym.body_location.end_column]
        elif sym.kind == "Struct":
            symbol_data["node_label"] = "CLASS_STRUCTURE" if sym.language and sym.language.lower() == "cpp" else "DATA_STRUCTURE"
            if hasattr(sym, 'body_location') and sym.body_location:
                symbol_data["body_location"] = [sym.body_location.start_line, sym.body_location.start_column, sym.body_location.end_line, sym.body_location.end_column]
        elif sym.kind in ("Union", "Enum"):
            symbol_data["node_label"] = "DATA_STRUCTURE"
            if hasattr(sym, 'body_location') and sym.body_location:
                symbol_data["body_location"] = [sym.body_location.start_line, sym.body_location.start_column, sym.body_location.end_line, sym.body_location.end_column]
        elif sym.kind == "Field":
            symbol_data["node_label"] = "FIELD"
            symbol_data.update({"type": sym.type, "is_static": False})
        elif sym.kind in ("StaticProperty", "EnumConstant"):
            symbol_data["node_label"] = "FIELD"
            symbol_data.update({"type": sym.type, "is_static": True})
        elif sym.kind == "Variable":
            symbol_data["node_label"] = "VARIABLE"
            symbol_data.update({"type": sym.type})
        elif sym.kind == "TypeAlias":
            symbol_data["node_label"] = "TYPE_ALIAS"
            symbol_data["aliased_canonical_spelling"] = sym.aliased_canonical_spelling
            symbol_data["aliased_type_id"] = sym.aliased_type_id
            symbol_data["aliased_type_kind"] = sym.aliased_type_kind
            symbol_data["parent_id"] = sym.parent_id
            symbol_data["scope"] = sym.scope
            symbol_data["qualified_name"] = sym.scope + sym.name
            if sym.body_location:
                symbol_data["body_location"] = [sym.body_location.start_line, sym.body_location.start_column, sym.body_location.end_line, sym.body_location.end_column]
        else:
            return None

        if sym.definition:
            abs_file_path = unquote(urlparse(sym.definition.file_uri).path)
            if self.path_manager.is_within_project(abs_file_path):
                symbol_data["file_path"] = self.path_manager.uri_to_relative_path(sym.definition.file_uri)
            else:
                symbol_data["file_path"] = abs_file_path
        return symbol_data

    def _process_and_group_symbols(self, symbols: Dict[str, Symbol], qualified_namespace_to_id: Dict[str, str]) -> Dict[str, List[Dict]]:
        """Groups processed symbols by their target node label."""
        processed_symbols = defaultdict(list)
        logger.info("Processing and grouping symbols by kind...")
        for sym in tqdm(symbols.values(), desc=align_string("Grouping symbols")):
            if data := self.process_symbol(sym, qualified_namespace_to_id):
                if 'node_label' in data:
                    processed_symbols[data['node_label']].append(data)
        return processed_symbols

    def ingest_symbols_and_relationships(self, symbol_parser: SymbolParser, neo4j_mgr: Neo4jManager, defines_generation_strategy: str):
        """Orchestrates the ingestion of all symbols and their relationships."""
        logger.info("Pass 1: Building scope maps and processing symbols...")
        qualified_namespace_to_id = self._build_scope_maps(symbol_parser.symbols)
        processed_symbols = self._process_and_group_symbols(symbol_parser.symbols, qualified_namespace_to_id)

        logger.info("Pass 2: Ingesting all nodes...")
        self._ingest_nodes_by_label(processed_symbols.get('NAMESPACE', []), "NAMESPACE", neo4j_mgr)
        self._ingest_nodes_by_label(processed_symbols.get('DATA_STRUCTURE', []), "DATA_STRUCTURE", neo4j_mgr)
        self._ingest_nodes_by_label(processed_symbols.get('CLASS_STRUCTURE', []), "CLASS_STRUCTURE", neo4j_mgr)
        self._ingest_nodes_by_label(processed_symbols.get('TYPE_ALIAS', []), "TYPE_ALIAS", neo4j_mgr)
        self._dedup_nodes(neo4j_mgr)
        self._ingest_nodes_by_label(processed_symbols.get('FUNCTION', []), "FUNCTION", neo4j_mgr)
        self._ingest_nodes_by_label(processed_symbols.get('METHOD', []), "METHOD", neo4j_mgr)
        self._ingest_nodes_by_label([f for f in processed_symbols.get('FIELD', []) if 'parent_id' in f], "FIELD", neo4j_mgr)
        self._ingest_nodes_by_label(processed_symbols.get('VARIABLE', []), "VARIABLE", neo4j_mgr)

        logger.info("Pass 3: Ingesting all relationships...")
       
        self._ingest_parental_relationships(processed_symbols, neo4j_mgr)
        self._ingest_file_namespace_declarations(processed_symbols.get('NAMESPACE', []), neo4j_mgr)
        self._ingest_other_declares_relationships(processed_symbols, neo4j_mgr)
        self._ingest_defines_relationships(processed_symbols, neo4j_mgr, defines_generation_strategy)

        self._ingest_has_member_relationships([f for f in processed_symbols.get('FIELD', []) if 'parent_id' in f], "FIELD", "HAS_FIELD", neo4j_mgr)
        self._ingest_has_member_relationships([m for m in processed_symbols.get('METHOD', []) if 'parent_id' in m], "METHOD", "HAS_METHOD", neo4j_mgr)
        
        self._ingest_inheritance_relationships(symbol_parser.inheritance_relations, neo4j_mgr)
        self._ingest_override_relationships(symbol_parser.override_relations, neo4j_mgr)
        self._ingest_alias_of_relationships(processed_symbols.get('TYPE_ALIAS', []), neo4j_mgr)

        del processed_symbols
        gc.collect()

    def _ingest_parental_relationships(self, processed_symbols: Dict[str, List[Dict]], neo4j_mgr: Neo4jManager):
        """Groups and ingests SCOPE_CONTAINS and HAS_NESTED relationships."""
        grouped_scope_relations = defaultdict(list)
        grouped_nested_relations = defaultdict(list)
       
        # Build a map of symbol ID to its node label for efficient lookups
        id_to_label_map = {}
        for label, symbol_list in processed_symbols.items():
            for data in symbol_list:
                id_to_label_map[data['id']] = label
 
        for symbol_list in processed_symbols.values():
            for symbol_data in symbol_list:
                # Group relationships for (NAMESPACE)-[:SCOPE_CONTAINS]->(...)
                if "namespace_id" in symbol_data:
                    parent_id = symbol_data["namespace_id"]
                    child_label = symbol_data["node_label"]
                    if child_label in ('NAMESPACE', 'CLASS_STRUCTURE', 'DATA_STRUCTURE', 'FUNCTION', 'VARIABLE'):
                        grouped_scope_relations[('NAMESPACE', child_label)].append({"parent_id": parent_id, "child_id": symbol_data["id"]})
                
                # Group relationships for (...)-[:HAS_NESTED]->(...)
                if "parent_id" in symbol_data:
                    parent_id = symbol_data["parent_id"]
                    if parent_label := id_to_label_map.get(parent_id):
                        child_label = symbol_data["node_label"]
                        if parent_label in ('CLASS_STRUCTURE', 'DATA_STRUCTURE', 'FUNCTION', 'METHOD') and child_label in ('CLASS_STRUCTURE', 'DATA_STRUCTURE', 'FUNCTION'):
                            grouped_nested_relations[(parent_label, child_label)].append({"parent_id": parent_id, "child_id": symbol_data["id"]})
        
        self._ingest_grouped_parental_relationships(grouped_scope_relations, "SCOPE_CONTAINS", neo4j_mgr)
        self._ingest_grouped_parental_relationships(grouped_nested_relations, "HAS_NESTED", neo4j_mgr)
        
        # ingest relationships for (SCOPE)-[:DEFINES_TYPE_ALIAS]->(:TYPE_ALIAS)
        grouped_defines_type_alias_relations = defaultdict(list)
        type_alias_symbols = processed_symbols.get('TYPE_ALIAS', [])
        for symbol_data in type_alias_symbols:
            parent_id = symbol_data["parent_id"]
            if parent_label := id_to_label_map.get(parent_id):
                grouped_defines_type_alias_relations[(parent_label, "TYPE_ALIAS")].append({"parent_id": parent_id, "child_id": symbol_data["id"]})
        self._ingest_grouped_parental_relationships(grouped_defines_type_alias_relations, "DEFINES_TYPE_ALIAS", neo4j_mgr)
        del id_to_label_map

    def _ingest_nodes_by_label(self, data_list: List[Dict], label: str, neo4j_mgr: Neo4jManager):
        """A generic function to ingest nodes of a specific label."""
        if not data_list: return
        
        logger.info(f"Creating {len(data_list)} {label} nodes in batches of {self.ingest_batch_size}...")
        keys_to_remove = ['parent_id', 'node_label']
        if label == "TYPE_ALIAS":
            keys_to_remove += ['aliased_type_id'] #'aliased_type_kind' and 'aliased_canonical_spelling' are kept for quick reference
        else:
            keys_to_remove += ['namespace_id']        

        keys_to_remove = str(keys_to_remove)
        query = f"""
        UNWIND $data AS d
        MERGE (n:{label} {{id: d.id}})
        SET n += apoc.map.removeKeys(d, {keys_to_remove})
        """
        
        total_nodes_created, total_properties_set = 0, 0
        for i in tqdm(range(0, len(data_list), self.ingest_batch_size), desc=align_string(f"Ingesting {label} nodes")):
            batch = data_list[i:i+self.ingest_batch_size]
            all_counters = neo4j_mgr.process_batch([(query, {"data": batch})])
            for counters in all_counters:
                total_nodes_created += counters.nodes_created
                total_properties_set += counters.properties_set
        logger.info(f"  Total {label} nodes created: {total_nodes_created}, properties set: {total_properties_set}")

    def _ingest_grouped_parental_relationships(self, grouped_relations: Dict[Tuple[str, str], List[Dict]], relationship_type: str, neo4j_mgr: Neo4jManager):
        if not grouped_relations: return
        total_rels = sum(len(v) for v in grouped_relations.values())
        logger.info(f"Creating {total_rels} {relationship_type} relationships...")
        total_rels_created = 0
        for (parent_label, child_label), relations in grouped_relations.items():
            logger.info(f"  Ingesting {len(relations)} {relationship_type} for ({parent_label})->({child_label})")
            query = f"UNWIND $data AS d MATCH (p:{parent_label} {{id: d.parent_id}}) MATCH (c:{child_label} {{id: d.child_id}}) MERGE (p)-[:{relationship_type}]->(c)"
            for i in tqdm(range(0, len(relations), self.ingest_batch_size), desc=align_string(f"Ingesting {relationship_type} ({child_label})")):
                counters = neo4j_mgr.execute_autocommit_query(query, {"data": relations[i:i+self.ingest_batch_size]})
                total_rels_created += counters.relationships_created
        logger.info(f"  Total {relationship_type} relationships created: {total_rels_created}")

    def _dedup_nodes(self, neo4j_mgr: Neo4jManager):
        """
        In some cases, a struct can be seen as both a DATA_STRUCTURE (in C contexts)
        and a CLASS_STRUCTURE (in C++ contexts). This removes the DATA_STRUCTURE
        if a CLASS_STRUCTURE with the same ID exists, preferring the C++ view.
        
        We use Clang.cindex parser to parse files, where on header file may be included by C++ file and C file.
        This will cause duplicate nodes (same id) but different node_label in the graph. The graph ingestor does not know they are duplicates.
        We cannot dedup simply by removing symbols of same id in memory without querying neo4j graph, 
        because there are cases where the data_structure nodes may be in the existing graph, while the class_structure may be generated by updater, or vice versa.
        NOTE: This should be only needed in graph updater path, 
        but we return the node/symbol in CompilationParser as a SourceSpan object that has "lang" property.
        That makes the same symbol parsed as C or Cpp to generate different SourceSpan objects, and finally enriched to Symbols as different synthetic symbols.
        If it is not a synthetic symbol, but an clangd indexed symbol, the synthetic symbols will be removed if they have a matched indexed symbol (in SourceSpanProvider) 
        """
        logger.info("Deduping DATA_STRUCTURE nodes if CLASS_STRUCTURE with same ID exists")
        query = """ 
            MATCH (ds:DATA_STRUCTURE)
            MATCH (cs:CLASS_STRUCTURE {id: ds.id})
            DETACH DELETE ds;
        """
        counters = neo4j_mgr.execute_autocommit_query(query)
        if counters.nodes_deleted > 0:
            logger.info(f"Total duplicate DATA_STRUCTURE nodes deleted: {counters.nodes_deleted}")

    def _ingest_has_member_relationships(self, data_list: List[Dict], child_label: str, relationship_type: str, neo4j_mgr: Neo4jManager):
        """A generic function to create relationships between a parent class/struct and its members (fields or methods)."""
        if not data_list: return
        
        logger.info(f"Creating {len(data_list)} {relationship_type} relationships in batches...")
        query = f"""
        UNWIND $data AS d
        MATCH (parent:DATA_STRUCTURE | CLASS_STRUCTURE {{id: d.parent_id}})
        MATCH (child:{child_label} {{id: d.id}})
        MERGE (parent)-[:{relationship_type}]->(child)
        """
        total_rels_created = 0
        for i in tqdm(range(0, len(data_list), self.ingest_batch_size), desc=align_string(f"Ingesting {relationship_type}")):
            counters = neo4j_mgr.execute_autocommit_query(query, {"data": data_list[i:i+self.ingest_batch_size]})
            total_rels_created += counters.relationships_created
        logger.info(f"  Total {relationship_type} relationships created: {total_rels_created}")

    def _get_defines_stats(self, defines_dict: Dict[str, List[Dict]]) -> str:
        """Generates a string summary of counts per label."""
        kind_counts = {label: len(data_list) for label, data_list in defines_dict.items()}
        return ", ".join(f"{kind}: {count}" for kind, count in sorted(kind_counts.items()))


    def _ingest_defines_relationships(self, processed_symbols: Dict[str, List[Dict]], neo4j_mgr: Neo4jManager, defines_generation_strategy: str='unwind-sequential'):
        # Group symbols that are defined in a file
        grouped_defines = defaultdict(list)
        for label in ['FUNCTION', 'VARIABLE', 'DATA_STRUCTURE', 'CLASS_STRUCTURE', 'TYPE_ALIAS']:
            for symbol_data in processed_symbols.get(label, []):
                if 'file_path' in symbol_data:
                    grouped_defines[label].append(symbol_data)
        
        # Ingest DEFINES relationships using the chosen strategy
        if defines_generation_strategy == "isolated-parallel":
            self._ingest_defines_relationships_isolated_parallel(grouped_defines, neo4j_mgr)
        else: # Default to unwind-sequential
            self._ingest_defines_relationships_unwind_sequential(grouped_defines, neo4j_mgr)

    def _ingest_defines_relationships_isolated_parallel(self, grouped_defines: Dict[str, List[Dict]], neo4j_mgr: Neo4jManager):
        """Ingests DEFINES relationships by grouping by file first to allow for parallelization."""
        if not grouped_defines: return
        total_defines = sum(len(v) for v in grouped_defines.values())
        logger.info(f"Found {total_defines} potential DEFINES relationships. Breakdown: {self._get_defines_stats(grouped_defines)}")
        logger.info("Grouping relationships by file for deadlock-safe parallel ingestion...")

        for label, data_list in grouped_defines.items():
            if not data_list: continue

            logger.info(f"  Ingesting {len(data_list)} (FILE)-[:DEFINES]->({label}) relationships...")
            grouped_by_file = defaultdict(list)
            for item in data_list:
                if 'file_path' in item:
                    grouped_by_file[item['file_path']].append(item)
            self._process_grouped_defines_isolated_parallel(grouped_by_file, neo4j_mgr, f":{label}")
        logger.info("Finished DEFINES relationship ingestion.")

    def _process_grouped_defines_isolated_parallel(self, grouped_by_file: Dict[str, List[Dict]], neo4j_mgr: Neo4jManager, node_label_filter: str):
        """Helper function to process batches for the isolated parallel strategy."""
        list_of_groups = list(grouped_by_file.values())
        if not list_of_groups: return

        total_rels = sum(len(group) for group in list_of_groups)
        num_groups = len(list_of_groups)
        avg_group_size = total_rels / num_groups if num_groups > 0 else 1
        safe_avg_group_size = max(1, avg_group_size)

        num_groups_per_tx = math.ceil(self.cypher_tx_size / safe_avg_group_size)
        num_groups_per_query = math.ceil(self.ingest_batch_size / safe_avg_group_size)
        
        final_groups_per_tx = max(1, num_groups_per_tx)
        final_groups_per_query = max(1, num_groups_per_query)

        logger.info(f"  Avg rels/file: {avg_group_size:.2f}. Submitting {final_groups_per_query} file-groups/query, with {final_groups_per_tx} file-groups/tx.")
        total_rels_created, total_rels_merged = 0, 0

        for i in tqdm(range(0, len(list_of_groups), final_groups_per_query), desc=align_string(f"DEFINES ({node_label_filter.strip(':')})")):
            query_batch = list_of_groups[i:i + final_groups_per_query]
            defines_rel_query = f"""
            CALL apoc.periodic.iterate(
                "UNWIND $groups AS group RETURN group",
                "UNWIND group AS data MATCH (f:FILE {{path: data.file_path}}) MATCH (n{node_label_filter} {{id: data.id}}) MERGE (f)-[:DEFINES]->(n)",
                {{ batchSize: $batch_size, parallel: true, params: {{ groups: $groups }} }}
            ) YIELD updateStatistics
            RETURN sum(updateStatistics.relationshipsCreated) AS totalRelsCreated, sum(updateStatistics.relationshipsUpdated) AS totalRelsMerged
            """
            results = neo4j_mgr.execute_query_and_return_records(defines_rel_query, {"groups": query_batch, "batch_size": final_groups_per_tx})
            if results:
                total_rels_created += results[0].get("totalRelsCreated", 0)
                total_rels_merged += results[0].get("totalRelsMerged", 0)
        logger.info(f"  Total DEFINES {node_label_filter} relationships created: {total_rels_created}, merged: {total_rels_merged}")

    def _ingest_defines_relationships_unwind_sequential(self, grouped_defines: Dict[str, List[Dict]], neo4j_mgr: Neo4jManager):
        """Ingests DEFINES relationships using a simple batched UNWIND."""
        if not grouped_defines: return
        total_defines = sum(len(v) for v in grouped_defines.values())
        logger.info(f"Found {total_defines} potential DEFINES relationships. Breakdown: {self._get_defines_stats(grouped_defines)}")
        logger.info("Creating relationships in batches using sequential UNWIND MERGE...")

        for label, data_list in grouped_defines.items():
            if not data_list: continue
            
            logger.info(f"  Ingesting {len(data_list)} (FILE)-[:DEFINES]->({label}) relationships...")
            query = f"""
            UNWIND $data AS d
            MATCH (f:FILE {{path: d.file_path}})
            MATCH (n:{label} {{id: d.id}})
            MERGE (f)-[:DEFINES]->(n)
            """
            total_rels_created = 0
            for i in tqdm(range(0, len(data_list), self.ingest_batch_size), desc=align_string(f"DEFINES ({label})")):
                counters = neo4j_mgr.execute_autocommit_query(query, {"data": data_list[i:i+self.ingest_batch_size]})
                total_rels_created += counters.relationships_created
        logger.info(f"  Total (FILE)-[:DEFINES]->({label}) relationships created: {total_rels_created}")

    def _ingest_inheritance_relationships(self, inheritance_relations: List[Tuple[str, str]], neo4j_mgr: Neo4jManager):
        if not inheritance_relations: return
        logger.info(f"Creating {len(inheritance_relations)} INHERITS relationships...")
        query = """
        UNWIND $relations AS rel
        MATCH (child:CLASS_STRUCTURE {id: rel.object_id})
        MATCH (parent:CLASS_STRUCTURE {id: rel.subject_id})
        MERGE (child)-[:INHERITS]->(parent)
        """
        relations_data = [{"subject_id": subj, "object_id": obj} for subj, obj in inheritance_relations]
        total_rels_created = 0
        for i in tqdm(range(0, len(relations_data), self.ingest_batch_size), desc=align_string("Ingesting INHERITS")):
            counters = neo4j_mgr.execute_autocommit_query(query, {"relations": relations_data[i:i+self.ingest_batch_size]})
            total_rels_created += counters.relationships_created
        logger.info(f"  Total INHERITS relationships created: {total_rels_created}")

    def _ingest_override_relationships(self, override_relations: List[Tuple[str, str]], neo4j_mgr: Neo4jManager):
        if not override_relations: return
        logger.info(f"Creating {len(override_relations)} OVERRIDDEN_BY relationships...")
        query = """
        UNWIND $relations AS rel
        MATCH (base_method:METHOD {id: rel.subject_id})
        MATCH (derived_method:METHOD {id: rel.object_id})
        MERGE (base_method)-[:OVERRIDDEN_BY]->(derived_method)
        """
        relations_data = [{"subject_id": subj, "object_id": obj} for subj, obj in override_relations]
        total_rels_created = 0
        for i in tqdm(range(0, len(relations_data), self.ingest_batch_size), desc=align_string("Ingesting OVERRIDDEN_BY")):
            counters = neo4j_mgr.execute_autocommit_query(query, {"relations": relations_data[i:i+self.ingest_batch_size]})
            total_rels_created += counters.relationships_created
        logger.info(f"  Total OVERRIDDEN_BY relationships created: {total_rels_created}")

    def _ingest_alias_of_relationships(self, type_alias_data_list: List[Dict], neo4j_mgr: Neo4jManager):
        """
        Ingests ALIAS_OF relationships from TYPE_ALIAS nodes to their aliased types.
        """
        if not type_alias_data_list: return
        
        relations_to_ingest = []
        for data in type_alias_data_list:
            # Automatically fillter out those aliased target nodes (aliased_type_id is None)
            if data.get('aliased_type_id'):
                relations_to_ingest.append({
                    "alias_id": data['id'],
                    "aliased_type_id": data['aliased_type_id'],
                    "aliased_type_kind": data['aliased_type_kind']
                })

        if not relations_to_ingest: return

        logger.info(f"Creating {len(relations_to_ingest)} ALIAS_OF relationships in batches of {self.ingest_batch_size}...")
        query = """
        UNWIND $data AS d
        MATCH (alias:TYPE_ALIAS {id: d.alias_id})
        MATCH (aliasee) WHERE aliasee.id = d.aliased_type_id AND (aliasee:CLASS_STRUCTURE|DATA_STRUCTURE|TYPE_ALIAS)
        MERGE (alias)-[:ALIAS_OF]->(aliasee)
        """
        
        total_rels_created = 0
        for i in tqdm(range(0, len(relations_to_ingest), self.ingest_batch_size), desc=align_string(f"Ingesting ALIAS_OF relationships")):
            batch = relations_to_ingest[i:i+self.ingest_batch_size]
            counters = neo4j_mgr.execute_autocommit_query(query, {"data": batch})
            total_rels_created += counters.relationships_created
        logger.info(f"  Total ALIAS_OF relationships created: {total_rels_created}")

    def _ingest_file_namespace_declarations(self, namespace_data_list: List[Dict], neo4j_mgr: Neo4jManager):
        relations_data = [ns for ns in namespace_data_list if ns.get('path')]
        if not relations_data: return
        logger.info(f"Creating {len(relations_data)} FILE-[:DECLARES]->NAMESPACE relationships...")
        query = f"UNWIND $data AS d MATCH (f:FILE {{path: d.path}}) MATCH (ns:NAMESPACE {{id: d.id}}) MERGE (f)-[:DECLARES]->(ns)"
        total_rels_created = 0
        for i in tqdm(range(0, len(relations_data), self.ingest_batch_size), desc=align_string("DECLARES (NAMESPACE)")):
            counters = neo4j_mgr.execute_autocommit_query(query, {"data": relations_data[i:i+self.ingest_batch_size]})
            total_rels_created += counters.relationships_created
        logger.info(f"  Total FILE-[:DECLARES]->NAMESPACE relationships created: {total_rels_created}")

    def _ingest_other_declares_relationships(self, processed_symbols: Dict[str, List[Dict]], neo4j_mgr: Neo4jManager):
        """ Ingests FILE-[:DECLARES] relationships for symbols that only have a declaration, not a definition. """
        logger.info("Creating FILE-[:DECLARES]->(Symbols with declaration only) relationships...")
        total_rels_created = 0

        # Iterate through the relevant labels that can be declaration-only
        for label in ('FUNCTION', 'VARIABLE', 'DATA_STRUCTURE', 'CLASS_STRUCTURE'):
            # Filter for declaration-only symbols within this label group
            data_list = [d for d in processed_symbols.get(label, []) if 'file_path' not in d and 'path' in d]            
            if not data_list:
                continue
            logger.info(f"   Ingesting {len(data_list)} (FILE)-[:DECLARES]->({label}) relationships...")
            query = f"""
            UNWIND $data AS d
            MATCH (f:FILE {{path: d.path}})
            MATCH (n:{label} {{id: d.id}})
            MERGE (f)-[:DECLARES]->(n)
            """
            for i in tqdm(range(0, len(data_list), self.ingest_batch_size), desc=align_string(f"DECLARES ({label})")):
                counters = neo4j_mgr.execute_autocommit_query(query, {"data": data_list[i:i+self.ingest_batch_size]})
                total_rels_created += counters.relationships_created
            logger.info(f"   Total FILE-[:DECLARES]->({label}) relationships created: {counters.relationships_created}")

        logger.info(f"  Total FILE-[:DECLARES]->(Other) relationships created: {total_rels_created}")

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    parser = argparse.ArgumentParser(description='Import Clangd index symbols and file structure into Neo4j.')
    input_params.add_core_input_args(parser)
    input_params.add_worker_args(parser)
    input_params.add_batching_args(parser)
    input_params.add_ingestion_strategy_args(parser)
    input_params.add_source_parser_args(parser)
    args = parser.parse_args()
    
    args.index_file = str(args.index_file.resolve())
    args.project_path = str(args.project_path.resolve())

    if args.ingest_batch_size is None:
        try:
            default_workers = math.ceil(os.cpu_count() / 2)
        except (NotImplementedError, TypeError):
            default_workers = 2
        args.ingest_batch_size = args.cypher_tx_size * (args.num_parse_workers or default_workers)

    logger.info("\n--- Starting Phase 0: Loading, Parsing, and Linking Symbols ---")
    symbol_parser = SymbolParser(index_file_path=args.index_file, log_batch_size=args.log_batch_size)
    symbol_parser.parse(num_workers=args.num_parse_workers)
    logger.info("--- Finished Phase 0 ---")

    logger.info("\n--- Starting Phase 1: Parsing Source Code for Spans ---")
    compilation_manager = CompilationManager(parser_type=args.source_parser, project_path=args.project_path, compile_commands_path=args.compile_commands)
    compilation_manager.parse_folder(args.project_path, args.num_parse_workers)
    logger.info("--- Finished Phase 1 ---")

    from source_span_provider import SourceSpanProvider
    logger.info("\n--- Starting Phase 2: Enriching Symbols with Spans ---")
    span_provider = SourceSpanProvider(symbol_parser=symbol_parser, compilation_manager=compilation_manager)
    span_provider.enrich_symbols_with_span()
    logger.info("--- Finished Phase 2 ---")
    
    path_manager = PathManager(args.project_path)
    with Neo4jManager() as neo4j_mgr:
        if not neo4j_mgr.check_connection(): return 1
        neo4j_mgr.setup_database(path_manager.project_path, {})
        
        logger.info("\n--- Starting Phase 3: Ingesting File & Folder Structure ---")
        path_processor = PathProcessor(path_manager, neo4j_mgr, args.log_batch_size, args.ingest_batch_size)
        path_processor.ingest_paths(symbol_parser.symbols, compilation_manager)
        del path_processor
        gc.collect()
        logger.info("--- Finished Phase 3 ---")

        logger.info("\n--- Starting Phase 4: Ingesting Symbol Definitions ---")
        symbol_processor = SymbolProcessor(path_manager, log_batch_size=args.log_batch_size, ingest_batch_size=args.ingest_batch_size, cypher_tx_size=args.cypher_tx_size)
        symbol_processor.ingest_symbols_and_relationships(symbol_parser, neo4j_mgr, args.defines_generation)
        del symbol_processor
        gc.collect()
        
        logger.info(f"\n✅ Done. Processed {len(symbol_parser.symbols)} symbols.")
        return 0

if __name__ == "__main__":
    sys.exit(main())