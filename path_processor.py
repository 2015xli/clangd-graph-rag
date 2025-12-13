#!/usr/bin/env python3
"""
This module processes an in-memory collection of clangd symbols to extract and 
create the file, folder, and symbol nodes in a Neo4j graph.
"""
import os
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import List, Dict
import logging
import gc
from tqdm import tqdm

from neo4j_manager import Neo4jManager, align_string
from clangd_index_yaml_parser import Symbol
from compilation_manager import CompilationManager

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

class PathProcessor:
    """Discovers and ingests file/folder structure into Neo4j."""
    def __init__(self, path_manager: PathManager, neo4j_mgr: Neo4jManager, log_batch_size: int = 1000, ingest_batch_size: int = 1000):
        self.path_manager, self.neo4j_mgr, self.log_batch_size, self.ingest_batch_size = path_manager, neo4j_mgr, log_batch_size, ingest_batch_size

    def _discover_paths_from_symbols(self, symbols: Dict[str, Symbol]) -> set:
        project_files = set()
        logger.info("Discovering file paths from symbols...")
        for sym in tqdm(symbols.values(), desc=align_string("Discovering paths from symbols")):
            for loc in [sym.definition, sym.declaration]:
                if loc and urlparse(loc.file_uri).scheme == 'file':
                    abs_path = unquote(urlparse(loc.file_uri).path)
                    if self.path_manager.is_within_project(abs_path):
                        project_files.add(self.path_manager.uri_to_relative_path(loc.file_uri))
        logger.info(f"Discovered {len(project_files)} unique files from symbols.")
        return project_files

    def _discover_paths_from_includes(self, compilation_manager: CompilationManager) -> set:
        include_files = set()
        logger.info("Discovering file paths from include relations...")
        for including_abs, included_abs in tqdm(compilation_manager.get_include_relations(), desc=align_string("Discovering paths from includes")):
            for abs_path in [including_abs, included_abs]:
                if self.path_manager.is_within_project(abs_path):
                    include_files.add(os.path.relpath(abs_path, self.path_manager.project_path))
        logger.info(f"Discovered {len(include_files)} unique files from includes.")
        return include_files

    def _get_folders_from_files(self, project_files: set) -> set:
        project_folders = set()
        for file_path in project_files:
            parent = Path(file_path).parent
            while str(parent) != '.' and str(parent) != '/':
                project_folders.add(str(parent))
                parent = parent.parent
        return project_folders

    def ingest_paths(self, symbols: Dict[str, Symbol], compilation_manager: CompilationManager):
        logger.info("Consolidating all unique file and folder paths...")
        project_files = self._discover_paths_from_symbols(symbols).union(self._discover_paths_from_includes(compilation_manager))
        project_folders = self._get_folders_from_files(project_files)
        logger.info(f"Consolidated to {len(project_files)} unique project files and {len(project_folders)} unique project folders.")
        
        folder_data_list = [{
            "path": folder_path,
            "name": Path(folder_path).name,
            "parent_path": str(Path(folder_path).parent) if str(Path(folder_path).parent) != '.' else self.path_manager.project_path
        } for folder_path in sorted(list(project_folders), key=lambda p: len(Path(p).parts))]
        self._ingest_folder_nodes_and_relationships(folder_data_list)

        file_data_list = [{
            "path": file_path,
            "name": Path(file_path).name,
            "parent_path": str(Path(file_path).parent) if str(Path(file_path).parent) != '.' else self.path_manager.project_path
        } for file_path in project_files]
        self._ingest_file_nodes_and_relationships(file_data_list)
        gc.collect()

    def _ingest_folder_nodes_and_relationships(self, folder_data_list: List[Dict]):
        if not folder_data_list: return
        logger.info(f"Creating {len(folder_data_list)} FOLDER nodes and CONTAINS relationships...")
        
        node_query = "UNWIND $data AS d MERGE (f:FOLDER {path: d.path}) SET f.name = d.name"
        total_nodes_created, total_properties_set = 0, 0
        for i in tqdm(range(0, len(folder_data_list), self.ingest_batch_size), desc=align_string("Ingesting FOLDER nodes")):
            batch = folder_data_list[i:i+self.ingest_batch_size]
            all_counters = self.neo4j_mgr.process_batch([(node_query, {"data": batch})])
            for counters in all_counters:
                total_nodes_created += counters.nodes_created
                total_properties_set += counters.properties_set
        logger.info(f"  Total FOLDER nodes created: {total_nodes_created}, properties set: {total_properties_set}")

        rel_query = """
        UNWIND $data AS d
        MATCH (child:FOLDER {path: d.path})
        MATCH (parent) WHERE (parent:FOLDER OR parent:PROJECT) AND parent.path = d.parent_path
        MERGE (parent)-[:CONTAINS]->(child)
        """
        total_rels_created = 0
        for i in tqdm(range(0, len(folder_data_list), self.ingest_batch_size), desc=align_string("Ingesting FOLDER CONTAINS")):
            counters = self.neo4j_mgr.execute_autocommit_query(rel_query, {"data": folder_data_list[i:i+self.ingest_batch_size]})
            total_rels_created += counters.relationships_created
        logger.info(f"  Total FOLDER CONTAINS relationships created: {total_rels_created}")

    def _ingest_file_nodes_and_relationships(self, file_data_list: List[Dict]):
        if not file_data_list: return
        logger.info(f"Creating {len(file_data_list)} FILE nodes and CONTAINS relationships...")

        node_query = "UNWIND $data AS d MERGE (f:FILE {path: d.path}) SET f.name = d.name"
        total_nodes_created, total_properties_set = 0, 0
        for i in tqdm(range(0, len(file_data_list), self.ingest_batch_size), desc=align_string("Ingesting FILE nodes")):
            batch = file_data_list[i:i+self.ingest_batch_size]
            all_counters = self.neo4j_mgr.process_batch([(node_query, {"data": batch})])
            for counters in all_counters:
                total_nodes_created += counters.nodes_created
                total_properties_set += counters.properties_set
        logger.info(f"  Total FILE nodes created: {total_nodes_created}, properties set: {total_properties_set}")

        rel_query = """
        UNWIND $data AS d
        MATCH (child:FILE {path: d.path})
        MATCH (parent) WHERE (parent:FOLDER OR parent:PROJECT) AND parent.path = d.parent_path
        MERGE (parent)-[:CONTAINS]->(child)
        """
        total_rels_created = 0
        for i in tqdm(range(0, len(file_data_list), self.ingest_batch_size), desc=align_string("Ingesting FILE CONTAINS")):
            counters = self.neo4j_mgr.execute_autocommit_query(rel_query, {"data": file_data_list[i:i+self.ingest_batch_size]})
            total_rels_created += counters.relationships_created
        logger.info(f"  Total FILE CONTAINS relationships created: {total_rels_created}")
