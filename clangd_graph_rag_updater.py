#!/usr/bin/env python3
"""
Orchestrates the incremental update of the code graph based on Git commits.
"""

import argparse
import sys, math
import logging
import os
import gc
from typing import Dict, List, Set

import input_params
from git_manager import GitManager
from git.exc import InvalidGitRepositoryError
from neo4j_manager import Neo4jManager
from clangd_index_yaml_parser import SymbolParser
from rag_updater import RagUpdater
from include_relation_provider import IncludeRelationProvider
from compilation_parser import CompilationParser # Import CompilationParser
from graph_update_scope_builder import GraphUpdateScopeBuilder

from log_manager import init_logging
init_logging()
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class GraphUpdater:
    """Manages the incremental update process using dependency analysis."""

    def __init__(self, args):
        self.args = args
        self.project_path = args.project_path
        self.neo4j_mgr = None

        logger.info(f"Initializing graph update for project: {self.project_path}")
        try:
            self.git_manager = GitManager(self.project_path)
        except InvalidGitRepositoryError:
            logger.error("Project path is not a valid Git repository. Aborting.")
            sys.exit(1)

    def update(self):
        """Runs the entire incremental update pipeline."""
        with Neo4jManager() as neo4j_mgr:
            if not neo4j_mgr.check_connection():
                return 1
            self.neo4j_mgr = neo4j_mgr

            if not self.neo4j_mgr.verify_project_path(self.project_path):
                sys.exit(1)

            old_commit, new_commit = self._resolve_commit_range()
            if old_commit == new_commit:
                logger.info("Database is already up-to-date. No update needed.")
                return

            logger.info(f"Processing changes from {old_commit} to {new_commit}")

            # Phase 1: Identify git changed files 
            git_changes = self._identify_git_changes(old_commit, new_commit)

            # Phase 2: Analyze files that are impacted by (including) git changed header files.
            impacted_from_graph = self._analyze_impact_from_graph(git_changes)
            dirty_files = set(git_changes['added'] + git_changes['modified']) | impacted_from_graph
            
            if not dirty_files and not git_changes['deleted']:
                logger.info("No relevant source file changes detected. Updating commit hash and exiting.")
                self.neo4j_mgr.update_project_node(self.project_path, {'commit_hash': new_commit})
                return

            logger.info(f"Found {len(dirty_files)} files to re-ingest and {len(git_changes['deleted'])} files to delete.")

            # Phase 3: Rebuild the dirty scope using the dedicated builder
            # 3.1. We build the full symbol parser to get all the symbols info.
            full_symbol_parser = SymbolParser(self.args.index_file)
            full_symbol_parser.parse(self.args.num_parse_workers)

            # 3.2. We build the mini symbol parser by extracting the sufficient subset of symbols from the full symbol parser.
            # The sufficient subset includes the seed symbols (directly defined by the dirty files) 
            # and their direct dependent symbols (e.g., parent-child, inheritance, override, caller-callee, scope, nesting).
            scope_builder = GraphUpdateScopeBuilder(self.args, self.neo4j_mgr, self.project_path)
            mini_symbol_parser = scope_builder.build_miniparser_for_dirty_scope(
                dirty_files, full_symbol_parser, new_commit, old_commit
            )

            # Phase 4: Purge all stale data from the graph. 
            # We purge after building the mini_symbol_parser solely for easy debugging. 
            # If we purge before the mini_symbol_parser building, we lose lots of nodes in the graph that can be useful for the mini_symbol_parser debugging.
            dirty_files_rel = {os.path.relpath(f, self.project_path) for f in dirty_files}
            deleted_files_rel = [os.path.relpath(f, self.project_path) for f in git_changes['deleted']]
            self._purge_stale_graph_data(dirty_files_rel, deleted_files_rel)

            # Phase 5: Rebuild the dirty scope. 
            scope_builder.rebuild_mini_scope()

            # Phase 6: Clean up orphan nodes
            self._cleanup_graph()
            
            # Update the commit hash in the graph to the new state
            self.neo4j_mgr.update_project_node(self.project_path, {'commit_hash': new_commit})
            logger.info(f"Successfully updated PROJECT node to commit: {new_commit}")

            # Phase 7: Run targeted RAG update if any symbols were re-ingested  
            if mini_symbol_parser:
                self._regenerate_summary(mini_symbol_parser, git_changes, impacted_from_graph)

        logger.info("\n✅ Incremental update complete.")

    def _resolve_commit_range(self) -> (str, str):
        new_commit = self.args.new_commit or self.git_manager.get_head_commit_hash()
        old_commit = self.args.old_commit or self.neo4j_mgr.get_graph_commit_hash(self.project_path)
        
        if not old_commit:
            logger.error("No old-commit specified and no commit hash found in the database. Cannot determine update range.")
            sys.exit(1)
            
        logger.info(f"Update range resolved: {old_commit} -> {new_commit}")
        return old_commit, new_commit

    def _identify_git_changes(self, old_commit: str, new_commit: str) -> Dict[str, List[str]]:
        logger.info("\n--- Phase 1: Identifying Changed Files via Git ---")
        changed_files = self.git_manager.get_changed_files_abs_path(old_commit, new_commit)
        logger.info(f"Found: {len(changed_files['added'])} added, {len(changed_files['modified'])} modified, {len(changed_files['deleted'])} deleted.")
        return changed_files

    def _analyze_impact_from_graph(self, git_changes: Dict[str, List[str]]) -> Set[str]:
        logger.info("\n--- Phase 2: Analyzing Header Impact via Graph Query ---")
        headers_to_check = [h for h in git_changes['modified'] if h.lower().endswith(CompilationParser.ALL_HEADER_EXTENSIONS)] + \
                           [h for h in git_changes['deleted'] if h.lower().endswith(CompilationParser.ALL_HEADER_EXTENSIONS)]

        if not headers_to_check:
            logger.info("No modified or deleted headers to analyze. Skipping graph query.")
            return set()

        include_provider = IncludeRelationProvider(self.neo4j_mgr, self.project_path)
        impacted_files = include_provider.get_impacted_files_from_graph(headers_to_check)
        return impacted_files

    def _purge_stale_graph_data(self, dirty_files_rel: Set[str], deleted_files_rel: List[str]):
        """ Purge all the nodes of deleted files, and 
        all the nodes defined/declared by either deleted or dirty files (and relationships to/from the nodes).
        NOTE: We don't prune empty NAMESPACE and PATH nodes recursively after files and symbols are purged
        because if the parent's parent namespace node is deleted, 
        the seed symbol nodes will not be able to find a namespace node to attach to.
        This may lead to some nodes becoming orphans and getting deleted finally.
        So we only prune the empty NAMESPACE and PATH nodes in the final cleanup phase.
        """
        logger.info("\n--- Phase 3: Purging Stale Graph Data ---")
        files_to_purge_symbols_from = list(dirty_files_rel | set(deleted_files_rel))

        if files_to_purge_symbols_from:
            logger.info(f"Purging symbols and includes from {len(files_to_purge_symbols_from)} files.")
            self.neo4j_mgr.purge_symbols_defined_in_files(files_to_purge_symbols_from)
            self.neo4j_mgr.purge_symbols_declared_in_files(files_to_purge_symbols_from)
            self.neo4j_mgr.purge_include_relations_from_files(files_to_purge_symbols_from)

        if deleted_files_rel:
            logger.info(f"Deleting {len(deleted_files_rel)} FILE nodes.")
            self.neo4j_mgr.purge_files(deleted_files_rel)
        
    def _cleanup_graph(self):
        neo4j_mgr = self.neo4j_mgr
        if not self.args.keep_orphans:
            logger.info("\n--- Phase 6: Cleaning up Orphan Nodes ---")
            deleted_nodes_count = neo4j_mgr.cleanup_orphan_nodes()
            logger.info(f"Removed {deleted_nodes_count} orphan nodes.")
        else:
            logger.info("\n--- Skipping Phase 6: Keeping orphan nodes as requested ---")
        
        # NOTE: It is fine to prune the empty NAMESPACE nodes. 
        # We just keep them here since they can be regarded as kinds of defines.

        #deleted_ns = neo4j_mgr.cleanup_empty_namespaces_recursively()
        #logger.info(f"Removed {deleted_ns} empty NAMESPACE nodes.")
        
        # NOTE: Some files although don't define/declare any symbols, are still needed to be included in the graph
        # because they are source files anyway, such as a file has only "#PRAGMA ONCE"
        # or they have function declarations while those functions have been defined in other files.
        # For the latter case, we only keep the function definition relationships in the graph currently.
        # In future, we may include more nodes in the graph such as macro definitions, etc. 
        # Then those files will have define/declare relationships to other nodes.
        
        #deleted_files, deleted_folders = neo4j_mgr.cleanup_empty_paths_recursively()
        #logger.info(f"Removed {deleted_files} empty FILE nodes and {deleted_folders} empty FOLDER nodes.")
        
        logger.info(f"Total nodes in graph: {neo4j_mgr.total_nodes_in_graph()}")
        logger.info(f"Total relationships in graph: {neo4j_mgr.total_relationships_in_graph()}")

    def _regenerate_summary(self, mini_symbol_parser: SymbolParser, git_changes: Dict[str, List[str]], impacted_from_graph: Set[str]):
        if not self.args.generate_summary:
            return

        logger.info("\n--- Phase 7: Running targeted RAG update ---")

        rag_updater = RagUpdater(
            neo4j_mgr=self.neo4j_mgr,
            project_path=self.project_path,
            args=self.args
        )
        
        # For Rag seeds we need the both function/methods and other core symbols that we need summarize
        # The other symbols include Class and Struct in C++. 
        # Here we provide both of them. If it is C project, they will be filtered out when summarizing CLASS_STRUCTURE nodes
        rag_seed_ids = {s.id for s in mini_symbol_parser.symbols.values()
                        if s.is_function() or s.kind == 'Class' or s.kind == 'Struct'}

        #rag_seed_ids = {s.id for s in mini_symbol_parser.functions.values()}
         
        # for graph operations, we need relative paths
        structurally_changed_files_for_rag = {
            'added': [os.path.relpath(f, self.project_path) for f in git_changes['added']],
            'modified': [os.path.relpath(f, self.project_path) for f in list(set(git_changes['modified']) | impacted_from_graph)],
            'deleted': [os.path.relpath(f, self.project_path) for f in git_changes['deleted']]
        }
        
        rag_updater.summarize_targeted_update(rag_seed_ids, structurally_changed_files_for_rag)

        logger.info("--- Summary regeneration complete ---")
        

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    parser = argparse.ArgumentParser(description='Incrementally update the code graph based on Git commits.')

    # Add argument groups from the centralized module
    input_params.add_core_input_args(parser)
    input_params.add_git_update_args(parser)
    input_params.add_worker_args(parser)
    input_params.add_batching_args(parser)
    input_params.add_rag_args(parser)
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

    updater = GraphUpdater(args)
    updater.update()

    return 0

if __name__ == "__main__":
    sys.exit(main())