#!/usr/bin/env python3
"""
CLI entry point for the compilation engine.
Allows parsing source files and extracting metadata via:
python3 -m source_parser [args]
"""

import argparse
import sys
import os
import yaml
import logging
from pathlib import Path
from collections import defaultdict
from dataclasses import asdict

import input_params
from .manager import CompilationManager
from utils import FileExtensions
# Need to import the provider to use its analysis function
from graph_ingester import IncludeRelationProvider
# Neo4jManager is imported for potential type hinting or future use
from neo4j_manager import Neo4jManager

logger = logging.getLogger(__name__)

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    parser = argparse.ArgumentParser(description="Parse C/C++ source files to extract function spans and include relations.")
    
    parser.add_argument("paths", nargs='+', type=Path, help="One or more source files or folders to process.")
    parser.add_argument("--output", type=Path, help="Output YAML file path (default: stdout).")

    input_params.add_worker_args(parser)
    
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
            if str(resolved_p).lower().endswith(FileExtensions.ALL_C_CPP):
                unique_files.add(str(resolved_p))
        elif resolved_p.is_dir():
            for root, _, files in os.walk(resolved_p):
                for f in files:
                    if f.lower().endswith(FileExtensions.ALL_C_CPP):
                        unique_files.add(os.path.join(root, f))
    
    file_list = sorted(list(unique_files))
    if not file_list:
        logger.error("No C/C++ source/header files found in the provided paths. Aborting.")
        sys.exit(1)

    logger.info(f"Found {len(file_list)} unique source files to process.")

    # --- Manager Initialization ---
    try:
        common_path = os.path.commonpath(file_list)
        project_path_for_init = os.path.abspath(common_path if common_path else os.getcwd())
    except ValueError:
        project_path_for_init = os.getcwd()

    if os.path.isfile(project_path_for_init):
        project_path_for_init = os.path.dirname(project_path_for_init)

    try:
        manager = CompilationManager(
            project_path=project_path_for_init,
            compile_commands_path=args.compile_commands
        )
    except (ValueError, FileNotFoundError) as e:
        logger.critical(e)
        sys.exit(1)
 
    # --- Extraction ---
    manager.parse_files(file_list, args.num_parse_workers or os.cpu_count() or 1)
    results = {}

    # --- Output Formatting ---
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
        # Filter "including" files to be within the project path
        project_relations = [
            rel for rel in manager.get_include_relations()
            if rel.source_file.startswith(project_path_for_init)
        ]

        # Group include output by including file
        grouped_includes = defaultdict(list)
        for rel in project_relations:
            grouped_includes[rel.source_file].append(rel.included_file)
        
        # Sort for consistent output
        for key in grouped_includes:
            grouped_includes[key].sort()

        # Flatten nested source_spans (Dict[file_uri, Dict[id, SourceSpan]])
        all_source_spans = []
        for file_spans in manager.get_source_spans().values():
            all_source_spans.extend(file_spans.values())

        results = {
            'source_spans': [asdict(s) for s in all_source_spans],
            'macro_spans': [asdict(s) for s in manager.get_macro_spans().values()],
            'type_alias_spans': [asdict(s) for s in manager.get_type_alias_spans().values()],
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

if __name__ == "__main__":
    main()
