#!/usr/bin/env python3
"""
This module provides the CompilationManager, a class responsible for orchestrating
the parsing of source code, managing different parsing strategies (e.g., clang vs.
treesitter), and handling the caching of parsing results.
"""

import os
import logging
import gc
import pickle
import hashlib
from typing import Optional, List, Tuple, Dict, Set, Any

# Optional Git import
try:
    import git
except ImportError:
    git = None

from compilation_parser import CompilationParser, ClangParser, TreesitterParser, SourceSpan, IncludeRelation
from git_manager import get_git_repo, resolve_commit_ref_to_hash

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# --- Caching Logic ---

class CacheManager:
    """Handles finding, validating, loading, and saving cache files."""
    def __init__(self, cache_directory: str, project_name: str):
        self.cache_directory = cache_directory
        self.project_name = project_name
        os.makedirs(self.cache_directory, exist_ok=True)

    def _construct_git_filename(self, new_commit: str, old_commit: str = None) -> str:
        """Constructs a cache filename based on commit hashes."""
        new_short = new_commit[:8]
        old_short = old_commit[:8] if old_commit else ''
        return f"parsing_{self.project_name}_hash_{new_short}_{old_short}.pkl"

    def _construct_mtime_filename(self, latest_mtime: float, oldest_mtime: float) -> str:
        """Constructs a cache filename based on modification times."""
        latest_hex = f"{int(latest_mtime):08x}"
        oldest_hex = f"{int(oldest_mtime):08x}"
        return f"parsing_{self.project_name}_time_{latest_hex}_{oldest_hex}.pkl"

    def find_and_load_git_cache(self, new_commit: str, old_commit: str = None) -> Optional[Tuple[List[Dict], Set[IncludeRelation]]]:
        """Finds and loads a cache based on Git commit hashes."""
        filename = self._construct_git_filename(new_commit, old_commit)
        cache_path = os.path.join(self.cache_directory, filename)

        if not os.path.exists(cache_path):
            return None

        try:
            with open(cache_path, "rb") as f:
                cached_data = pickle.load(f)
            
            # Deep validation
            if (cached_data.get("new_commit") == new_commit and
                cached_data.get("old_commit") == old_commit):
                logger.info(f"Found and validated Git-based cache: {filename}")
                return cached_data.get("source_spans", []), cached_data.get("include_relations", set())
            else:
                logger.warning(f"Cache file {filename} has mismatched full commit hashes. Ignoring.")
                return None
        except (pickle.UnpicklingError, EOFError, KeyError) as e:
            logger.warning(f"Cache file {cache_path} is corrupted: {e}. Ignoring.")
            return None

    def find_and_load_mtime_cache(self, file_list: List[str]) -> Optional[Tuple[List[Dict], Set[IncludeRelation]]]:
        """Finds and loads a cache based on file modification times and content hash."""
        if not file_list:
            return None

        try:
            mtimes = [os.path.getmtime(f) for f in file_list]
            latest_mtime = max(mtimes)
            oldest_mtime = min(mtimes)
        except FileNotFoundError:
            return None # One of the files doesn't exist

        filename = self._construct_mtime_filename(latest_mtime, oldest_mtime)
        cache_path = os.path.join(self.cache_directory, filename)

        if not os.path.exists(cache_path):
            return None

        try:
            with open(cache_path, "rb") as f:
                cached_data = pickle.load(f)

            # Deep validation: check if the list of files is identical
            current_file_hash = hashlib.sha256("".join(sorted(file_list)).encode()).hexdigest()
            if cached_data.get("file_list_hash") == current_file_hash:
                logger.info(f"Found and validated mtime-based cache: {filename}")
                return cached_data.get("source_spans", []), cached_data.get("include_relations", set())
            else:
                logger.warning(f"Cache file {filename} has mismatched file list hash. Ignoring.")
                return None
        except (pickle.UnpicklingError, EOFError, KeyError) as e:
            logger.warning(f"Cache file {cache_path} is corrupted: {e}. Ignoring.")
            return None

    def save_git_cache(self, data: Any, new_commit: str, old_commit: str = None):
        """Saves data to a Git-based cache file."""
        filename = self._construct_git_filename(new_commit, old_commit)
        cache_path = os.path.join(self.cache_directory, filename)
        
        cache_obj = {
            "source_spans": data[0],
            "include_relations": data[1],
            "new_commit": new_commit,
            "old_commit": old_commit
        }
        logger.info(f"Saving Git-based cache to: {filename}")
        with open(cache_path, "wb") as f:
            pickle.dump(cache_obj, f)

    def save_mtime_cache(self, data: Any, file_list: List[str]):
        """Saves data to an mtime-based cache file."""
        mtimes = [os.path.getmtime(f) for f in file_list]
        filename = self._construct_mtime_filename(max(mtimes), min(mtimes))
        cache_path = os.path.join(self.cache_directory, filename)
        
        file_list_hash = hashlib.sha256("".join(sorted(file_list)).encode()).hexdigest()
        
        cache_obj = {
            "source_spans": data[0],
            "include_relations": data[1],
            "file_list_hash": file_list_hash
        }
        logger.info(f"Saving mtime-based cache to: {filename}")
        with open(cache_path, "wb") as f:
            pickle.dump(cache_obj, f)


# --- Main Manager Class ---

class CompilationManager:
    """Manages parsing, caching, and strategy selection."""

    def __init__(self, parser_type: str = 'clang', 
                 project_path: str = '.', compile_commands_path: Optional[str] = None):
        self.parser_type = parser_type
        self.project_path = os.path.abspath(project_path)
        self.compile_commands_path = compile_commands_path
        self._parser: Optional[CompilationParser] = None
        self.repo = get_git_repo(self.project_path)

        cache_dir = os.path.join(self.project_path, ".cache")
        project_name = os.path.basename(self.project_path)
        self.cache_manager = CacheManager(cache_dir, project_name)

        if self.parser_type == 'clang' and not self.compile_commands_path:
            inferred_path = os.path.join(project_path, 'compile_commands.json')
            if not os.path.exists(inferred_path):
                raise ValueError("Clang parser requires a path to compile_commands.json via --compile-commands")
            self.compile_commands_path = inferred_path

    def _create_parser(self) -> CompilationParser:
        """Factory method to create the appropriate parser instance."""
        if self._parser is not None:
            return self._parser

        if self.parser_type == 'clang':
            self._parser = ClangParser(self.project_path, self.compile_commands_path)
        else: # 'treesitter'
            self._parser = TreesitterParser(self.project_path)
        return self._parser

    def _perform_parsing(self, files_to_parse: List[str], num_workers: int) -> Tuple[List[Dict], Set[IncludeRelation]]:
        """Internal method to run the actual parsing logic."""
        if not files_to_parse:
            return [], set()
        
        parser = self._create_parser()
        parser.parse(files_to_parse, num_workers)
        gc.collect()
        return parser.get_source_spans(), parser.get_include_relations()

    def parse_folder(self, folder_path: str, num_workers: int, new_commit: str = None):
        """
        Parses a full folder by resolving it to a list of files and delegating to parse_files.

        This is a convenience wrapper that provides two main strategies:
        1. If the folder is a Git repository, it uses `git ls-tree` to get an accurate
           list of all files at a specific commit (or HEAD) and then calls parse_files
           with the commit hash to enable Git-based caching.
        2. If not a Git repository, it walks the filesystem to find all source files
           and calls parse_files without commit info, falling back to mtime-based caching.
        """
        # --- Git-based path ---
        if self.repo:
            final_commit_hash = new_commit
            try:
                if not final_commit_hash:
                    final_commit_hash = self.repo.head.object.hexsha
                else:
                    # Resolve user-provided ref (tag, branch, etc.) to a full hash
                    final_commit_hash = resolve_commit_ref_to_hash(self.repo, final_commit_hash)
            except (ValueError, git.exc.GitCommandError) as e:
                logger.error(f"Failed to resolve commit hash: {e}")
                sys.exit(1)
            
            # Use git ls-tree for a fast and accurate file list
            all_files_str = self.repo.git.ls_tree('-r', '--name-only', final_commit_hash)
            all_files_in_commit = [
                os.path.join(self.project_path, f) for f in all_files_str.split('\n')
                if f.lower().endswith(CompilationParser.ALL_C_CPP_EXTENSIONS)
            ]
            # Delegate to parse_files with the full file list and commit context
            self.parse_files(all_files_in_commit, num_workers, new_commit=final_commit_hash, old_commit=None)
            return

        # --- Non-Git fallback path ---
        logger.warning("Not a Git repository. Falling back to mtime-based caching for the folder.")
        all_files_in_folder = []
        for root, _, fs in os.walk(folder_path):
            for f in fs:
                if f.lower().endswith(CompilationParser.ALL_C_CPP_EXTENSIONS):
                    all_files_in_folder.append(os.path.join(root, f))
        
        # Delegate to parse_files, which has the mtime-based logic
        self.parse_files(all_files_in_folder, num_workers, new_commit=None, old_commit=None)


    def parse_files(self, file_list: List[str], num_workers: int, new_commit: str = None, old_commit: str = None):
        """
        Parses a specific list of files, using a cache if possible. This is the central
        method for all parsing and caching logic.

        It uses one of two caching strategies based on the provided arguments:

        1. Git-based Caching (if `new_commit` is provided):
           - Used for full builds on a Git repo (`old_commit` is None).
           - Used for incremental updates (`old_commit` is specified).
           - The cache key is derived from the commit hashes, making it highly reliable.
           - The cache manager looks for a file like `..._hash_<new_short>_<old_short>.pkl`.

        2. Mtime-based Caching (if `new_commit` is None):
           - Used for non-Git projects or for ad-hoc parsing of file lists.
           - The cache key is derived from the min/max modification times of the files
             in the list.
           - A deep validation check (hashing the sorted file list) is performed to
             prevent cache collisions.
           - The cache manager looks for a file like `..._time_<latest_hex>_<oldest_hex>.pkl`.
        """
        # --- Git-based path for updates ---
        if self.repo and new_commit:
            try:
                # Resolve refs to full hashes for reliable caching
                new_commit_hash = resolve_commit_ref_to_hash(self.repo, new_commit)
                old_commit_hash = resolve_commit_ref_to_hash(self.repo, old_commit) if old_commit else None
            except (ValueError, git.exc.GitCommandError) as e:
                logger.error(f"Failed to resolve commit hash: {e}")
                sys.exit(1)

            cached_data = self.cache_manager.find_and_load_git_cache(new_commit_hash, old_commit_hash)
            if cached_data:
                self._parser = self._create_parser()
                self._parser.source_spans, self._parser.include_relations = cached_data
                return
            
            logger.info(f"No valid cache for update {old_commit_hash[:8] if old_commit_hash else ''} -> {new_commit_hash[:8]}. Parsing {len(file_list)} files.")
            parsed_data = self._perform_parsing(file_list, num_workers)
            self.cache_manager.save_git_cache(parsed_data, new_commit_hash, old_commit_hash)
            return

        # --- Mtime-based path for non-Git or ad-hoc lists ---
        cached_data = self.cache_manager.find_and_load_mtime_cache(file_list)
        if cached_data:
            self._parser = self._create_parser()
            self._parser.source_spans, self._parser.include_relations = cached_data
            return

        logger.info(f"No valid mtime-based cache found. Parsing {len(file_list)} files.")
        parsed_data = self._perform_parsing(file_list, num_workers)
        self.cache_manager.save_mtime_cache(parsed_data, file_list)

    def get_source_spans(self) -> Dict[str, Dict[str, SourceSpan]]:
        if not hasattr(self, '_parser') or self._parser is None:
            raise RuntimeError("CompilationManager has not parsed any files yet.")
        return self._parser.get_source_spans()

    def get_include_relations(self) -> Set[IncludeRelation]:
        if not hasattr(self, '_parser') or self._parser is None:
            raise RuntimeError("CompilationManager has not parsed any files yet.")
        return self._parser.get_include_relations()

if __name__ == "__main__":
    import argparse
    import sys
    import yaml
    from pathlib import Path
    from collections import defaultdict
    import input_params
    # Need to import the provider to use its analysis function
    from include_relation_provider import IncludeRelationProvider
    # Dummy Neo4jManager for type hinting, not actually used to connect to DB
    from neo4j_manager import Neo4jManager

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    parser = argparse.ArgumentParser(description="Parse C/C++ source files to extract function spans and include relations.")
    
    parser.add_argument("paths", nargs='+', type=Path, help="One or more source files or folders to process.")
    parser.add_argument("--output", type=Path, help="Output YAML file path (default: stdout).")

    parser_group = parser.add_argument_group('Parser Configuration')
    input_params.add_source_parser_args(parser_group)

    analysis_group = parser.add_argument_group('Analysis Mode')
    analysis_group.add_argument("--impacting-header", 
                                help="Analyze which source files are impacted by a change in this single header file.")

    args = parser.parse_args()

    # --- Path Normalization ---
    logger.info(f"Scanning {len(args.paths)} input path(s)...")
    unique_files = set()
    for p in args.paths:
        resolved_p = p.resolve()
        if resolved_p.is_file():
            if str(resolved_p).lower().endswith(CompilationParser.ALL_C_CPP_EXTENSIONS):
                unique_files.add(str(resolved_p))
        elif resolved_p.is_dir():
            for root, _, files in os.walk(resolved_p):
                for f in files:
                    if f.lower().endswith(CompilationParser.ALL_C_CPP_EXTENSIONS):
                        unique_files.add(os.path.join(root, f))
    
    file_list = sorted(list(unique_files))
    if not file_list:
        logger.error("No C/C++ source/header files found in the provided paths. Aborting.")
        sys.exit(1)

    logger.info(f"Found {len(file_list)} unique source files to process.")

    # --- Manager Initialization ---
    project_path_for_init = os.path.abspath(os.path.commonpath(file_list) if file_list else os.getcwd())
    if os.path.isfile(project_path_for_init):
        project_path_for_init = os.path.dirname(project_path_for_init)

    try:
        manager = CompilationManager(
            parser_type=args.source_parser,
            project_path=project_path_for_init,
            compile_commands_path=args.compile_commands
        )
    except (ValueError, FileNotFoundError) as e:
        logger.critical(e)
        sys.exit(1)
 
    # --- Extraction ---
    # The __main__ block now calls parse_files, which has caching.
    manager.parse_files(file_list, os.cpu_count() or 1)
    results = {}

    # --- Output Formatting ---
    # Mode 1: Analyze impact of a specific header
    if args.impacting_header:
        logger.info("Running in impact analysis mode...")
        # We can pass a dummy Neo4jManager since it's not used for in-memory analysis
        provider = IncludeRelationProvider(neo4j_manager=None, project_path=project_path_for_init)
        all_relations = manager.get_include_relations()
        
        # Resolve input header to an absolute path for matching
        header_to_check = os.path.abspath(args.impacting_header)

        impact_results = provider.analyze_impact_from_memory(all_relations, [header_to_check])
        results = {'impact_analysis': impact_results}

    # Mode 2: Default mode, dump all parsed data
    else:
        logger.info("Running in default dump mode...")
        # Requirement 2: Filter "including" files to be within the project path
        project_relations = [
            rel for rel in manager.get_include_relations()
            if rel[0].startswith(project_path_for_init)
        ]

        # Requirement 1: Group include output by including file
        grouped_includes = defaultdict(list)
        for including, included in project_relations:
            grouped_includes[including].append(included)
        
        # Sort for consistent output
        for key in grouped_includes:
            grouped_includes[key].sort()

        results = {
            'source_spans': manager.get_source_spans(),
            'grouped_include_relations': dict(sorted(grouped_includes.items()))
        }

    yaml_output = yaml.dump(results, sort_keys=False, allow_unicode=True)

    if args.output:
        output_path = str(args.output.resolve())
        with open(output_path, "w", encoding="utf-8") as out:
            out.write(yaml_output)
        print(f"Output saved to {output_path}")
    else:
        print(yaml_output)