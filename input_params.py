#!/usr/bin/env python3
"""
Centralized module for defining and adding command-line arguments.
"""

import argparse
import os
import math
from pathlib import Path

def add_core_input_args(parser: argparse.ArgumentParser):
    """Adds core input arguments: index_file and project_path."""
    parser.add_argument('index_file', type=Path, help='Path to the clangd index YAML file (or .pkl cache).')
    parser.add_argument('project_path', type=Path, help='Root path of the project being indexed. Or for call graph builder, it is the path to a directory for function span provider to scan.')

def add_worker_args(parser: argparse.ArgumentParser):
    """Adds arguments related to parallel workers."""
    try:
        default_workers = math.ceil(os.cpu_count() / 2)
    except (NotImplementedError, TypeError):
        default_workers = 2

    parser.add_argument('--num-parse-workers', type=int, default=default_workers,
                        help=f'Number of parallel workers for parsing. (default: {default_workers})')
    parser.add_argument('--num-local-workers', type=int, default=default_workers,
                        help=f'Number of parallel workers for local LLMs/embedding models. (default: {default_workers})')
    parser.add_argument('--num-remote-workers', type=int, default=100,
                        help='Number of parallel workers for remote LLM/embedding APIs. (default: 100)')

def add_batching_args(parser: argparse.ArgumentParser):
    """Adds arguments related to batching and performance tuning."""
    parser.add_argument('--log-batch-size', type=int, default=1000, help='Log progress every N items (default: 1000)')
    parser.add_argument('--cypher-tx-size', type=int, default=2000,
                        help='Target items per server-side transaction (default: 2000).')
    parser.add_argument('--ingest-batch-size', type=int, default=None,
                        help='Target items per client submission. Default: (cypher-tx-size * num-parse-workers).')

def add_rag_args(parser: argparse.ArgumentParser):
    """Adds arguments related to RAG (summary and embedding) generation."""
    rag_group = parser.add_argument_group('RAG Generation (Optional)')
    rag_group.add_argument('--generate-summary', action='store_true',
                        help='Generate AI summaries and embeddings for the code graph.')
    rag_group.add_argument('--llm-api', choices=['openai', 'deepseek', 'ollama', 'fake'], default='fake',
                        help='The LLM API to use for summarization. (default fake)')
    rag_group.add_argument('--max-context-size', type=int, default=30000,
                        help='The maximum context token size for a single LLM call. (default: 30000)')

def add_ingestion_strategy_args(parser: argparse.ArgumentParser):
    """Adds arguments that control ingestion strategy."""
    parser.add_argument('--defines-generation', choices=['unwind-sequential', 'isolated-parallel', 'batched-parallel'], default='unwind-sequential',
                        help='Strategy for ingesting DEFINES relationships. (default: unwind-sequential)')
    parser.add_argument('--keep-orphans', action='store_true',
                      help='Keep orphan nodes in the graph (skip cleanup)')

def add_git_update_args(parser: argparse.ArgumentParser):
    """Adds arguments specific to the incremental git-based updater."""
    parser.add_argument('--old-commit', default=None, help='The old commit hash or reference. Defaults to graph commit_hash')
    parser.add_argument('--new-commit', default=None, help='The new commit hash or reference. Defaults to repo HEAD')


def add_logistic_args(parser: argparse.ArgumentParser):
    """Adds arguments for script logistics like output, stats, and mode."""
    parser.add_argument('--output', '-o', help='Optional output file path for results.')
    parser.add_argument('--stats', action='store_true', help='Show statistics at the end of the process.')
    parser.add_argument('--ingest', action='store_true', help='If set, ingest data directly into Neo4j.')
    parser.add_argument('--debug-memory', action='store_true', help='Enable memory profiling with tracemalloc.')

def add_source_parser_args(parser: argparse.ArgumentParser):
    """Adds arguments for selecting and configuring the source code parser."""
    parser.add_argument('--source-parser', choices=['clang', 'treesitter'], default='clang',
                        help='The source code parser to use for spans and includes. (default: clang)')
    parser.add_argument('--compile-commands', help='Path to the compile_commands.json file (or its directory). Required for the `clang` parser. (Default: the project path)')

