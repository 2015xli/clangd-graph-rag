#!/usr/bin/env python3
"""
Orchestrates the parsing of source code using the Clang-based parser
and manages the caching of results.
"""

import os
import logging
import gc
import sys
from typing import Optional, List, Tuple, Dict, Set, Any
import git

from compilation_ops import SourceSpan, MacroSpan, TypeAliasSpan, IncludeRelation, CacheManager, CompilationParser, ClangParser
from git_manager import get_git_repo, resolve_commit_ref_to_hash

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class CompilationManager:
    """Manages parsing, caching, and orchestration using ClangParser."""

    def __init__(self, project_path: str = '.', compile_commands_path: Optional[str] = None):
        self.project_path = os.path.abspath(project_path)
        self.compile_commands_path = compile_commands_path
        self._parser: Optional[CompilationParser] = None
        self.repo = get_git_repo(self.project_path)

        cache_dir = os.path.join(self.project_path, ".cache")
        project_name = os.path.basename(self.project_path)
        self.cache_manager = CacheManager(cache_dir, project_name)

        if not self.compile_commands_path:
            inferred_path = os.path.join(project_path, 'compile_commands.json')
            if not os.path.exists(inferred_path):
                raise ValueError("Clang parser requires a path to compile_commands.json via --compile-commands")
            self.compile_commands_path = inferred_path

    def _create_parser(self) -> CompilationParser:
        """Factory method to create the ClangParser instance."""
        if self._parser is not None:
            return self._parser

        self._parser = ClangParser(self.project_path, self.compile_commands_path)
        return self._parser

    def _perform_parsing(self, files_to_parse: List[str], num_workers: int) -> Dict[str, Any]:
        """Internal method to run the actual parsing logic."""
        if not files_to_parse:
            return {
                "source_spans": {},
                "include_relations": set(),
                "static_call_relations": set(),
                "type_alias_spans": {},
                "macro_spans": {}
            }
        
        parser = self._create_parser()
        parser.parse(files_to_parse, num_workers)
        gc.collect()
        return {
            "source_spans": parser.get_source_spans(),
            "include_relations": parser.get_include_relations(),
            "static_call_relations": parser.get_static_call_relations(),
            "type_alias_spans": parser.get_type_alias_spans(),
            "macro_spans": parser.get_macro_spans()
        }

    def parse_folder(self, folder_path: str, num_workers: int, new_commit: str = None):
        """Parses a full folder by resolving it to a list of files."""
        if self.repo:
            try:
                final_commit_hash = resolve_commit_ref_to_hash(self.repo, new_commit) if new_commit else self.repo.head.object.hexsha
            except (ValueError, git.exc.GitCommandError) as e:
                logger.error(f"Failed to resolve commit hash: {e}")
                sys.exit(1)
            
            all_files_str = self.repo.git.ls_tree('-r', '--name-only', final_commit_hash)
            all_files_in_commit = [
                os.path.join(self.project_path, f) for f in all_files_str.split('\n')
                if f.lower().endswith(CompilationParser.ALL_C_CPP_EXTENSIONS)
            ]
            self.parse_files(all_files_in_commit, num_workers, new_commit=final_commit_hash, old_commit=None)
            return

        logger.warning("Not a Git repository. Falling back to mtime-based caching for the folder.")
        all_files_in_folder = []
        for root, _, fs in os.walk(folder_path):
            for f in fs:
                if f.lower().endswith(CompilationParser.ALL_C_CPP_EXTENSIONS):
                    all_files_in_folder.append(os.path.join(root, f))
        
        self.parse_files(all_files_in_folder, num_workers, new_commit=None, old_commit=None)

    def parse_files(self, file_list: List[str], num_workers: int, new_commit: str = None, old_commit: str = None):
        """Parses a specific list of files, using a cache if possible."""
        if self.repo and new_commit:
            try:
                new_commit_hash = resolve_commit_ref_to_hash(self.repo, new_commit)
                old_commit_hash = resolve_commit_ref_to_hash(self.repo, old_commit) if old_commit else None
            except (ValueError, git.exc.GitCommandError) as e:
                logger.error(f"Failed to resolve commit hash: {e}")
                sys.exit(1)

            cached_data = self.cache_manager.find_and_load_git_cache(new_commit_hash, old_commit_hash)
            if cached_data:
                self._parser = self._create_parser()
                self._parser.source_spans = cached_data["source_spans"]
                self._parser.include_relations = cached_data["include_relations"]
                self._parser.static_call_relations = cached_data["static_call_relations"]
                self._parser.type_alias_spans = cached_data["type_alias_spans"]
                self._parser.macro_spans = cached_data["macro_spans"]
                return
            
            logger.info(f"No valid cache found. Parsing {len(file_list)} files.")
            parsed_data = self._perform_parsing(file_list, num_workers)
            self.cache_manager.save_git_cache(parsed_data, new_commit_hash, old_commit_hash)
            return

        cached_data = self.cache_manager.find_and_load_mtime_cache(file_list)
        if cached_data:
            self._parser = self._create_parser()
            self._parser.source_spans = cached_data["source_spans"]
            self._parser.include_relations = cached_data["include_relations"]
            self._parser.static_call_relations = cached_data["static_call_relations"]
            self._parser.type_alias_spans = cached_data["type_alias_spans"]
            self._parser.macro_spans = cached_data["macro_spans"]
            return

        logger.info(f"No valid mtime-based cache found. Parsing {len(file_list)} files.")
        parsed_data = self._perform_parsing(file_list, num_workers)
        self.cache_manager.save_mtime_cache(parsed_data, file_list)

    def get_source_spans(self) -> Dict[str, Dict[str, SourceSpan]]:
        if self._parser is None: raise RuntimeError("Files not parsed yet.")
        return self._parser.get_source_spans()

    def get_include_relations(self) -> Set[IncludeRelation]:
        if self._parser is None: raise RuntimeError("Files not parsed yet.")
        return self._parser.get_include_relations()

    def get_static_call_relations(self) -> Set[Tuple[str, str]]:
        if self._parser is None: raise RuntimeError("Files not parsed yet.")
        return self._parser.get_static_call_relations()

    def get_type_alias_spans(self) -> Dict[str, TypeAliasSpan]:
        if self._parser is None: raise RuntimeError("Files not parsed yet.")
        return self._parser.get_type_alias_spans()

    def get_macro_spans(self) -> Dict[str, MacroSpan]:
        if self._parser is None: raise RuntimeError("Files not parsed yet.")
        return self._parser.get_macro_spans()

if __name__ == "__main__":
    import argparse
    import yaml
    from pathlib import Path
    from collections import defaultdict
    import input_params
    from include_relation_provider import IncludeRelationProvider

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    parser = argparse.ArgumentParser(description="Parse C/C++ source files to extract function spans and include relations.")
    parser.add_argument("paths", nargs='+', type=Path, help="Source files or folders.")
    parser.add_argument("--output", type=Path, help="Output YAML path.")
    input_params.add_worker_args(parser)
    input_params.add_source_parser_args(parser)
    analysis_group = parser.add_argument_group('Analysis Mode')
    analysis_group.add_argument("--impacting-header", help="Analyze which files are impacted by a header change.")
    args = parser.parse_args()

    unique_files = set()
    for p in args.paths:
        resolved_p = p.resolve()
        if resolved_p.is_file() and str(resolved_p).lower().endswith(CompilationParser.ALL_C_CPP_EXTENSIONS):
            unique_files.add(str(resolved_p))
        elif resolved_p.is_dir():
            for root, _, fs in os.walk(resolved_p):
                for f in fs:
                    if f.lower().endswith(CompilationParser.ALL_C_CPP_EXTENSIONS):
                        unique_files.add(os.path.join(root, f))
    
    file_list = sorted(list(unique_files))
    if not file_list:
        logger.error("No files found.")
        sys.exit(1)

    project_path = os.path.abspath(os.path.commonpath(file_list))
    if os.path.isfile(project_path): project_path = os.path.dirname(project_path)

    manager = CompilationManager(project_path=project_path, compile_commands_path=args.compile_commands)
    manager.parse_files(file_list, os.cpu_count() or 1)
    results = {}

    if args.impacting_header:
        provider = IncludeRelationProvider(neo4j_mgr=None, project_path=project_path)
        impact_results = provider.analyze_impact_from_memory(manager.get_include_relations(), [os.path.abspath(args.impacting_header)])
        results = {'impact_analysis': impact_results}
    else:
        project_relations = [rel for rel in manager.get_include_relations() if rel[0].startswith(project_path)]
        grouped = defaultdict(list)
        for incing, incded in project_relations: grouped[incing].append(incded)
        for k in grouped: grouped[k].sort()
        results = {'source_spans': manager.get_source_spans(), 'grouped_include_relations': dict(sorted(grouped.items()))}

    yaml_output = yaml.dump(results, sort_keys=False, allow_unicode=True)
    if args.output:
        with open(str(args.output.resolve()), "w", encoding="utf-8") as out: out.write(yaml_output)
    else: print(yaml_output)
