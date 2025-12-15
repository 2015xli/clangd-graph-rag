# Source Code Graph RAG on Clangd Index

## Table of Contents
- [Why This Project?](#why-this-project)
- [Key Features & Design Principles](#key-features--design-principles)
- [Primary Usage](#primary-usage)
  - [Full Graph Build](#full-graph-build)
  - [Incremental Graph Update](#incremental-graph-update)
  - [Common Options](#common-options)
- [Supporting Scripts](#supporting-scripts)

## Why This Project?

For C/C++ project, Clangd index YAML file is an intermediate data format from [Clangd-indexer](https://clangd.llvm.org/design/indexing.html) containing detailed syntactical information used by language servers for code navigation and completion. However, while powerful for IDEs, the raw index data doesn't expose the full graph structure of a codebase (especially the call graph) or integrate the semantic understanding that Large Language Models (LLMs) can leverage.

This project fills that gap. It ingests Clangd index data into a Neo4j graph database, reconstructing the complete file, symbol, and call graph hierarchy. It then enriches this structure with AI-generated summaries and vector embeddings, transforming the raw compiler index into a semantically rich knowledge graph. In essence, `clangd-graph-rag` extends Clangd's powerful foundation into an AI-ready code graph, enabling LLMs to reason about a codebase's structure and behavior for advanced tasks like in-depth code analysis, refactoring, and automated reviewing.

## Key Features & Design Principles

*   **AI-Enriched Code Graph**: Builds a comprehensive graph of files, folders, symbols, and function calls, then enriches it with AI-generated summaries and vector embeddings for semantic understanding.
*   **Robust Dependency Analysis**: Builds a complete `[:INCLUDES]` graph by parsing source files, enabling accurate impact analysis for header file changes.
*   **Compiler-Accurate Parsing**: Leverages `clang` via a `compile_commands.json` file to parse source code with full semantic context, correctly handling complex macros and include paths.
*   **Incremental Updates**: Includes a Git-aware updater script that efficiently processes only the files changed between commits, avoiding the need for a full rebuild.
*   **Adaptive Call Graph Construction**: Intelligently adapts its strategy for building the call graph based on the version of the `clangd` index, using the `Container` field when available and falling back to a spatial analysis when not.
*   **High-Performance & Memory Efficient**: Designed for performance with multi-process and multi-threaded parallelism, efficient batching for database operations, and intelligent memory management to handle large codebases.
*   **Modular & Reusable**: The core logic is encapsulated in modular classes and helper scripts, promoting code reuse and maintainability.

## Primary Usage

The two main entry points for the pipeline are the builder and the updater.

**Note**: All scripts now rely on a `compile_commands.json` file for accurate source code analysis. The examples below assume this file is located in the root of your project path. If it is located elsewhere, you must specify its location with the `--compile-commands` option (see Common Options).

For all the scripts that can run standalone, you can always use --help to see the full CLI options.

### Full Graph Build

Used for the initial, from-scratch ingestion of a project. Orchestrated by `clangd_graph_rag_builder.py`.

```bash
# Basic build (graph structure only)
python3 clangd_graph_rag_builder.py /path/to/index.yaml /path/to/project/

# Build with RAG data generation
python3 clangd_graph_rag_builder.py /path/to/index.yaml /path/to/project/ --generate-summary
```

### Incremental Graph Update

Used to efficiently update an existing graph with changes from Git. Orchestrated by `clangd_graph_rag_updater.py`.

```bash
# Update from the last known commit in the graph to the current HEAD
python3 clangd_graph_rag_updater.py /path/to/new/index.yaml /path/to/project/

# Update between two specific commits
python3 clangd_graph_rag_updater.py /path/to/new/index.yaml /path/to/project/ --old-commit <hash1> --new-commit <hash2>
```

### Common Options

Both the builder and updater accept a wide range of common arguments, which are centralized in `input_params.py`. These include:

*   **Compilation Arguments**:
    *   `--compile-commands`: Path to the `compile_commands.json` file. This file is essential for the new accurate parsing engine. By default, the tool searches for `compile_commands.json` in the project's root directory.
*   **RAG Arguments**: Control summary and embedding generation (e.g., `--generate-summary`, `--llm-api`).
*   **Worker Arguments**: Configure parallelism (e.g., `--num-parse-workers`, `--num-remote-workers`).
*   **Batching Arguments**: Tune performance for database ingestion (e.g., `--ingest-batch-size`, `--cypher-tx-size`).
*   **Ingestion Strategy Arguments**: Choose different algorithms for relationship creation (e.g., `--defines-generation`).

Run any script with `--help` to see all available options.

## Supporting Scripts

These scripts are the core components of the pipeline and can also be run standalone for debugging or partial processing.

*   **`clangd_symbol_nodes_builder.py`**:
    *   **Purpose**: Ingests the file/folder structure and symbol definitions.
    *   **Assumption**: Best run on a clean database.
    *   **Usage**: `python3 clangd_symbol_nodes_builder.py <index.yaml> <project_path/>`

*   **`clangd_call_graph_builder.py`**:
    *   **Purpose**: Ingests *only* the function call graph relationships.
    *   **Assumption**: Symbol nodes (such as `:FILE`, `:FUNCTION`) must already exist in the database.
    *   **Usage**: `python3 clangd_call_graph_builder.py <index.yaml> <project_path/> --ingest`

*   **`code_graph_rag_generator.py`**:
    *   **Purpose**: Runs the RAG enrichment process on an *existing* graph.
    *   **Assumption**: The structural graph (files, symbols, calls) must already be populated in the database.
    *   **Usage**: `python3 code_graph_rag_generator.py <index.yaml> <project_path/> --llm-api fake`

*   **`neo4j_manager.py`**:
    *   **Purpose**: A command-line utility for database maintenance.
    *   **Functionality**: Includes tools to `dump-schema` for inspection or `delete-property` to clean up data.
    *   **Usage**: `python3 neo4j_manager.py dump-schema`

## Documentation & Contributing

### Documentation

Detailed design documents for each component can be found at [docs/README.md](docs/README.md) under [docs/](docs/) folder. 
For a comprehensive overview of the project's architecture, design principles, and pipelines, please refer to [docs/Building_an_AI-Ready_Code_Graph_RAG_based_on_Clangd_index.md](docs/Building_an_AI-Ready_Code_Graph_RAG_based_on_Clangd_index.md).

### Contributing

Contributions are welcome! This includes bug reports, feature requests, and pull requests. Feel free to try `clangd-graph-rag` on your own `clangd`-indexed projects and share your feedback.

### Future Work

The current roadmap includes:
-   Adding a wrapper layer for AI agentic tasks (e.g., an MCP server).
