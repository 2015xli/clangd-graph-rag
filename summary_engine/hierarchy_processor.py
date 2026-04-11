#!/usr/bin/env python3
"""
Mixin for file, folder, and project summarization.
Handles physical roll-ups from entities to files, files to folders, and folders to the project.
"""

import logging
import os
from typing import Set, List, Dict

# Set up logging for this module
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class HierarchyProcessorMixin:
    """
    Encapsulates logic for rolling up summaries through the physical hierarchy:
    FILES -> FOLDERS -> PROJECT.
    """

    def summarize_files_with_paths(self, file_paths: list[str]) -> set:
        """Generates summaries for the specified file paths using a manifest approach."""
        if not file_paths:
            return set()
        
        logger.info(f"Processing summaries for {len(file_paths)} candidate files.")
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers

        query = """
        MATCH (f:FILE {path: $key})
        OPTIONAL MATCH (f)-[:INCLUDES]->(inc:FILE)
        OPTIONAL MATCH (f)-[:DEFINES|DECLARES]->(sym)
        WHERE NOT sym:FIELD
        RETURN collect(DISTINCT inc.path) AS include_paths,
               collect(DISTINCT {
                   name: sym.name, 
                   kind: sym.kind, 
                   id: sym.id, 
                   labels: labels(sym), 
                   summary: sym.summary
               }) AS symbol_inventory
        """
        items_to_process = [
            (file_path, 'FILE', query, self.node_processor.get_file_summary)
            for file_path in file_paths
        ]

        updated_ids = self._parallel_process(
            items=items_to_process,
            process_func=self._process_one_hierarchical_node,
            max_workers=max_workers,
            desc="Pass 5: File Summaries"
        )
        logger.info(f"Pass 5: File Summaries - Updated {len(updated_ids)} nodes.")
        return updated_ids

    def summarize_folders_with_paths(self, folder_paths: list[str]) -> set:
        """Generates summaries for the specified folder paths using a manifest approach, bottom-up."""
        if not folder_paths:
            return set()

        paths_by_depth = defaultdict(list)
        for folder_path in folder_paths:
            paths_by_depth[folder_path.count(os.sep)].append(folder_path)

        all_updated_ids = set()
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers

        for depth in sorted(paths_by_depth.keys(), reverse=True):
            level_paths = paths_by_depth[depth]
            logger.info(f"Processing {len(level_paths)} folders at depth {depth}.")

            query = """
            MATCH (fold:FOLDER {path: $key})
            MATCH (fold)-[:CONTAINS]->(child)
            RETURN collect(DISTINCT {
                       name: child.name, 
                       path: child.path, 
                       labels: labels(child), 
                       summary: child.summary
                   }) AS children_inventory
            """
            items_to_process = [
                (path, 'FOLDER', query, self.node_processor.get_folder_summary)
                for path in level_paths
            ]

            updated_ids_at_level = self._parallel_process(
                items=items_to_process,
                process_func=self._process_one_hierarchical_node,
                max_workers=max_workers,
                desc=f"Pass 6: Folder Summaries (Depth {depth})"
            )
            logger.info(f"Pass 6 (Depth {depth}): Folder Summaries - Updated {len(updated_ids_at_level)} nodes.")
            all_updated_ids.update(updated_ids_at_level)
            
        logger.info(f"Pass 6 (all depths): Folder Summaries - Updated {len(all_updated_ids)} nodes.")
        return all_updated_ids

    def summarize_project(self) -> set:
        """Generates the final summary for the PROJECT node using a manifest approach."""
        project_path = self.project_path
        logger.info("Processing summary for PROJECT node.")
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers

        query = """
        MATCH (p:PROJECT {path: $key})
        MATCH (p)-[:CONTAINS]->(child)
        RETURN collect(DISTINCT {
                   name: child.name, 
                   path: child.path, 
                   labels: labels(child), 
                   summary: child.summary
               }) AS children_inventory
        """
        items_to_process = [(project_path, 'PROJECT', query, self.node_processor.get_project_summary)]

        return self._parallel_process(
            items=items_to_process,
            process_func=self._process_one_hierarchical_node,
            max_workers=max_workers,
            desc="Pass 7: Project Summary"
        )

    def _process_one_hierarchical_node(self, args) -> dict:
        """Generic worker for hierarchical nodes (File, Folder, Project)."""
        key, label, manifest_query, processor_func = args

        node_query = f"MATCH (n:{label} {{path: $key}}) RETURN n, labels(n) as n_labels"
        node_results = self.neo4j_mgr.execute_read_query(node_query, {"key": key})
        if not node_results or not node_results[0]['n']:
            logger.warning(f"Could not find node {label} with path {key} for summary.")
            return None

        node_data = dict(node_results[0]['n'])
        node_data['label'] = label

        # Execute the manifest query
        manifest_results = self.neo4j_mgr.execute_read_query(manifest_query, {"key": key})
        manifest_data = manifest_results[0] if manifest_results else {}

        status, data = processor_func(node_data, manifest_data)

        if status in ["summary_regenerated", "summary_restored"]:
            # Build the SET clause dynamically to handle optional code_hash
            set_clauses = ["n.summary = $summary"]
            params = {"key": key, "summary": data["summary"]}
            
            if data.get("code_hash"):
                set_clauses.append("n.code_hash = $code_hash")
                params["code_hash"] = data["code_hash"]
            
            update_query = f"MATCH (n:{label} {{path: $key}}) SET {', '.join(set_clauses)} REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(update_query, params)
        
        return {
            "key": key,
            "label": label,
            "status": status,
            "data": data
        }

from collections import defaultdict
