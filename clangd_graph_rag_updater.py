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
from clangd_symbol_nodes_builder import PathManager, PathProcessor, SymbolProcessor
from clangd_call_graph_builder import ClangdCallGraphExtractorWithContainer, ClangdCallGraphExtractorWithoutContainer
from source_span_provider import SourceSpanProvider
from code_graph_rag_generator import RagGenerator
from llm_client import get_llm_client, get_embedding_client
from compilation_manager import CompilationManager
from include_relation_provider import IncludeRelationProvider
from compilation_parser import CompilationParser # Import CompilationParser

logger = logging.getLogger(__name__)

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

            git_changes = self._identify_git_changes(old_commit, new_commit)
            impacted_from_graph = self._analyze_impact_from_graph(git_changes)
            
            dirty_files = set(git_changes['added'] + git_changes['modified']) | impacted_from_graph
            if not dirty_files and not git_changes['deleted']:
                logger.info("No relevant source file changes detected. Updating commit hash and exiting.")
                self.neo4j_mgr.update_project_node(self.project_path, {'commit_hash': new_commit})
                return

            logger.info(f"Found {len(dirty_files)} files to re-ingest and {len(git_changes['deleted'])} files to delete.")

            # Convert paths to relative for purge operations
            dirty_files_rel = {os.path.relpath(f, self.project_path) for f in dirty_files}
            deleted_files_rel = [os.path.relpath(f, self.project_path) for f in git_changes['deleted']]
            self._purge_stale_graph_data(dirty_files_rel, deleted_files_rel)

            # The rebuild scope method still works with absolute paths
            self._rebuild_dirty_scope(git_changes, impacted_from_graph)

            self.neo4j_mgr.update_project_node(self.project_path, {'commit_hash': new_commit})
            logger.info(f"Successfully updated PROJECT node to commit: {new_commit}")

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
        logger.info("\n--- Phase 3: Purging Stale Graph Data ---")
        files_to_purge_symbols_from = list(dirty_files_rel | set(deleted_files_rel))

        if files_to_purge_symbols_from:
            logger.info(f"Purging symbols and includes from {len(files_to_purge_symbols_from)} files.")
            self.neo4j_mgr.purge_symbols_defined_in_files(files_to_purge_symbols_from)
            self.neo4j_mgr.purge_include_relations_from_files(files_to_purge_symbols_from)

        if deleted_files_rel:
            logger.info(f"Deleting {len(deleted_files_rel)} FILE nodes.")
            self.neo4j_mgr.purge_files(deleted_files_rel)
        
        # NEW: Cleanup orphaned NAMESPACE nodes after files and symbols are purged
        logger.info("Cleaning up orphaned NAMESPACE nodes...")
        deleted_namespaces_count = self.neo4j_mgr.cleanup_orphaned_namespaces()
        if deleted_namespaces_count > 0:
            logger.info(f"Removed {deleted_namespaces_count} orphaned NAMESPACE nodes.")

    def _rebuild_dirty_scope(self, git_changes: Dict[str, List[str]], impacted_from_graph: Set[str]):
        dirty_files = set(git_changes['added'] + git_changes['modified']) | impacted_from_graph
        logger.info(f"\n--- Phase 4: Rebuilding scope for {len(dirty_files)} Dirty Files ---")
        if not dirty_files:
            logger.info("No dirty files to rebuild. Skipping.")
            return

        # 1. Parse full new symbol index
        full_symbol_parser = SymbolParser(self.args.index_file)
        full_symbol_parser.parse(self.args.num_parse_workers)

        # 2. Parse only dirty source files
        comp_manager = CompilationManager(
            parser_type=self.args.source_parser,
            project_path=self.project_path,
            compile_commands_path=self.args.compile_commands
        )
        comp_manager.parse_files(list(dirty_files), self.args.num_parse_workers)

        # 3. Create "mini" symbol parser for the dirty scope
        dirty_file_uris = {f"file://{os.path.abspath(f)}" for f in dirty_files}
        dirty_symbol_ids = {
            s.id for s in full_symbol_parser.symbols.values() 
            if s.definition and s.definition.file_uri in dirty_file_uris
        }
        mini_symbol_parser = full_symbol_parser.create_sufficient_subset(dirty_symbol_ids)
        logger.info(f"Created mini-parser with {len(mini_symbol_parser.symbols)} symbols from dirty files.")
        del full_symbol_parser

        # 4. Enrich the "mini" symbols with spans
        span_provider = SourceSpanProvider(mini_symbol_parser, comp_manager)
        span_provider.enrich_symbols_with_span()

        # 5. Re-run ingestion pipeline on the mini-scope
        path_manager = PathManager(self.project_path)
        
        path_processor = PathProcessor(path_manager, self.neo4j_mgr, self.args.log_batch_size, self.args.ingest_batch_size)
        path_processor.ingest_paths(mini_symbol_parser.symbols, comp_manager)

        symbol_processor = SymbolProcessor(path_manager, self.args.log_batch_size, self.args.ingest_batch_size, self.args.cypher_tx_size)
        symbol_processor.ingest_symbols_and_relationships(mini_symbol_parser.symbols, self.neo4j_mgr, self.args.defines_generation)

        include_provider = IncludeRelationProvider(self.neo4j_mgr, self.project_path)
        include_provider.ingest_include_relations(comp_manager, self.args.ingest_batch_size)

        if mini_symbol_parser.has_container_field:
            extractor = ClangdCallGraphExtractorWithContainer(mini_symbol_parser, self.args.log_batch_size, self.args.ingest_batch_size)
        else:
            extractor = ClangdCallGraphExtractorWithoutContainer(mini_symbol_parser, self.args.log_batch_size, self.args.ingest_batch_size)
        call_relations = extractor.extract_call_relationships()
        extractor.ingest_call_relations(call_relations, neo4j_mgr=self.neo4j_mgr)

        logger.info("--- Re-ingestion complete ---")

        # 6. Run targeted RAG update
        self._regenerate_summary(mini_symbol_parser, git_changes, impacted_from_graph)

    def _regenerate_summary(self, mini_symbol_parser: SymbolParser, git_changes: Dict[str, List[str]], impacted_from_graph: Set[str]):
        if not self.args.generate_summary:
            return

        logger.info("\n--- Phase 5: Running targeted RAG update ---")

        llm_client = get_llm_client(self.args.llm_api)
        embedding_client = get_embedding_client(self.args.llm_api)
        rag_generator = RagGenerator(
            neo4j_mgr=self.neo4j_mgr,
            project_path=self.project_path,
            llm_client=llm_client,
            embedding_client=embedding_client,
            num_local_workers=self.args.num_local_workers,
            num_remote_workers=self.args.num_remote_workers,
            max_context_size=self.args.max_context_size,
        )
        
        rag_seed_ids = {s.id for s in mini_symbol_parser.symbols.values()
                        if s.is_function() or s.kind == 'Class'}
        
        structurally_changed_files_for_rag = {
            'added': git_changes['added'],
            'modified': list(set(git_changes['modified']) | impacted_from_graph),
            'deleted': git_changes['deleted']
         }
         
        # for graph operations, we need relative paths
        structurally_changed_files_for_rag = {
            'added': [os.path.relpath(f, self.project_path) for f in git_changes['added']],
            'modified': [os.path.relpath(f, self.project_path) for f in list(set(git_changes['modified']) | impacted_from_graph)],
            'deleted': [os.path.relpath(f, self.project_path) for f in git_changes['deleted']]
        }
        
        rag_generator.summarize_targeted_update(rag_seed_ids, structurally_changed_files_for_rag)

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
    # Set a different default for defines_generation for safety in updates
    parser.set_defaults(defines_generation='batched-parallel')

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