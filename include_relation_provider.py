#!/usr/bin/env python3
"""
This module provides the IncludeRelationProvider class, which is responsible for
handling the `:INCLUDES` relationship in the graph. This includes both ingesting
the relationships during a full build and querying them to find impacted files
during an incremental update.
"""

import logging
import os
from typing import List, Set, Dict, Tuple
from collections import defaultdict, deque

from neo4j_manager import Neo4jManager
from compilation_manager import CompilationManager
from compilation_parser import IncludeRelation

logger = logging.getLogger(__name__)

class IncludeRelationProvider:
    """Manages the `:INCLUDES` relationships in the Neo4j graph."""

    def __init__(self, neo4j_manager: Neo4jManager, project_path: str):
        """
        Initializes the provider with a Neo4jManager instance.

        Args:
            neo4j_manager: An active Neo4jManager instance.
            project_path: The absolute path to the root of the project.
        """
        self.neo4j_manager = neo4j_manager
        self.project_path = project_path

    def ingest_include_relations(self, compilation_manager: CompilationManager, batch_size: int = 1000):
        """
        Gets include relations from the compilation manager, converts paths to
        relative, and ingests them into Neo4j.
        """
        logger.info("Preparing to ingest :INCLUDES relationships into the graph...")
        
        include_relations_set: Set[IncludeRelation] = compilation_manager.get_include_relations()
        if not include_relations_set:
            logger.info("No include relations found to ingest.")
            return

        relations_list = []
        for including, included in include_relations_set:
            try:
                rel_including = os.path.relpath(including, self.project_path)
                rel_included = os.path.relpath(included, self.project_path)

                # Relative path creates ../ for external files. Filter them out.
                if '..' not in rel_including and '..' not in rel_included:
                    relations_list.append({
                        "including_path": rel_including,
                        "included_path": rel_included
                    })
                    #logger.info(f"    {rel_including} --> {rel_included}")

            except ValueError:
                continue # Ignore paths not in project (e.g., different drives on Windows)

        if not relations_list:
            logger.warning("No internal include relations found to ingest.")
            return

        logger.info(f"Ingesting {len(relations_list)} internal include relations.")
        self.neo4j_manager.ingest_include_relations(relations_list, batch_size=batch_size)

    def get_impacted_files_from_graph(self, headers: List[str]) -> Set[str]:
        """
        Queries the graph with relative paths to find all source files that
        transitively include the given headers, and returns them as absolute paths.
        """
        if not headers:
            return set()

        logger.info(f"Querying graph for files impacted by {len(headers)} changed header(s)...")
        impacted_files = set()

        query = """
        MATCH (f:FILE)-[:INCLUDES*]->(:FILE {path: $header_path})
        RETURN f.path AS path
        """

        for header_abs_path in headers:
            try:
                header_rel_path = os.path.relpath(header_abs_path, self.project_path)
                if '..' in header_rel_path:
                    continue
            except ValueError:
                continue

            params = {"header_path": header_rel_path}
            results = self.neo4j_manager.execute_read_query(query, params)
            for record in results:
                # Convert relative path from DB back to absolute for the caller
                impacted_abs_path = os.path.join(self.project_path, record['path'])
                impacted_files.add(impacted_abs_path)

        logger.info(f"Found {len(impacted_files)} impacted source files in the graph.")
        return impacted_files

    def analyze_impact_from_memory(self, all_relations: Set[IncludeRelation], headers_to_check: List[str]) -> Dict[str, List[str]]:
        """
        Analyzes the impact of header changes using an in-memory set of include relations.
        This method uses absolute paths as it operates on raw parser data.
        """
        logger.info(f"Building reverse include graph from {len(all_relations)} relations...")
        reverse_include_graph = defaultdict(set)
        for including, included in all_relations:
            reverse_include_graph[included].add(including)

        impact_results = {}
        for header_path in headers_to_check:
            impacted_for_header = set()
            queue = deque([header_path])
            visited = {header_path}

            while queue:
                current_file = queue.popleft()
                for dependent in reverse_include_graph.get(current_file, []):
                    if dependent not in visited:
                        visited.add(dependent)
                        impacted_for_header.add(dependent)
                        queue.append(dependent)
            
            source_files = sorted([
                f for f in impacted_for_header 
                if f.endswith(('.c', '.cpp', '.cc', '.cxx'))
            ])
            impact_results[header_path] = source_files
        
        return impact_results
