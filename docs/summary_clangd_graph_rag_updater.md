# Summary: `clangd_graph_rag_updater.py` - Incremental Code Graph RAG Updater

This document summarizes the design and functionality of `clangd_graph_rag_updater.py`. This script is responsible for incrementally updating the Neo4j code graph based on changes in a Git repository.

The logic has been significantly refactored to align with the main graph builder, ensuring consistent and robust updates.

## 1. Purpose

The primary purpose of `clangd_graph_rag_updater.py` is to provide an efficient mechanism for keeping the Neo4j code graph synchronized with an evolving C/C++ codebase. This avoids the computationally expensive process of re-ingesting the entire project for minor changes.

## 2. Core Design: Graph-Based Dependency Analysis

The updater's core logic revolves around a robust, multi-stage process to determine the full impact of any code change. It uses the `[:INCLUDES]` relationships in the graph to find all files affected by a change, making it much more accurate than simple call-graph analysis.

## 3. The Incremental Update Pipeline

The update process is divided into a sequence of phases orchestrated by the `GraphUpdater` class.

### Phase 1: Identify Git Changes

*   **Component**: `git_manager.GitManager`
*   **Purpose**: To determine which source files (`.c`, `.h`, etc.) have been `added`, `modified`, or `deleted` between two Git commits, returned as absolute paths.

### Phase 2: Analyze Header Impact via Graph Query

*   **Component**: `include_relation_provider.IncludeRelationProvider`
*   **Purpose**: To find all source files that are indirectly affected by header file changes.
*   **Mechanism**: It takes the absolute paths of modified/deleted headers from Phase 1 and queries the graph to find all dependent files, returning them as a set of absolute paths.

### Phase 3: Purge Stale Graph Data

*   **Purpose**: To remove all outdated information from the graph, creating a clean slate for the new data.
*   **Mechanism**: The main orchestrator determines the complete set of **"dirty files"** (the union of files from Phase 1 and 2) as absolute paths. It then converts these paths to **relative paths** before calling the appropriate `neo4j_manager` methods to purge nodes and relationships from the graph.

### Phase 4: Rebuild Dirty Scope

*   **Purpose**: To surgically "patch" the graph with new, updated information by running a "mini" version of the main builder pipeline.
*   **Mechanism**:
    1.  **Parse Full Symbol Index**: Parses the **entire new `clangd` index file** to get up-to-date symbol information for the whole project.
    2.  **Parse Dirty Sources**: Calls `CompilationManager` to parse **only the dirty files** (using their absolute paths), efficiently gathering their include relationships and function spans.
    3.  **Create "Mini" Parser**: Creates a small, in-memory `SymbolParser` containing only the symbols whose definitions are located within one of the dirty files.
    4.  **Enrich "Mini" Symbols**: Uses `SourceSpanProvider` to attach the `body_location` data (from step 2) to the in-memory symbols in the `mini_symbol_parser` (from step 3).
    5.  **Re-run Processors**: It re-runs the standard `PathProcessor`, `SymbolProcessor`, `IncludeRelationProvider`, and `ClangdCallGraphExtractor` using the data from the "mini" and "dirty" datasets. These processors internally handle the conversion from absolute to relative paths where necessary for graph operations.

### Phase 5: Targeted RAG Update

*   **Purpose**: To efficiently update AI-generated summaries and embeddings.
*   **Component**: `code_graph_rag_generator.RagGenerator`
*   **Mechanism**: It calls the `summarize_targeted_update()` method, providing the set of all function IDs from the "mini-parser" as the initial "seed." The `RagGenerator` reads the `body_location` property directly from the graph nodes to fetch the source code it needs.

## 4. Design Subtlety: Path Management

A critical design principle throughout the updater is the careful management of file paths. The convention is strictly enforced:

*   **Absolute Paths for Processing:** All internal logic, such as identifying changed files with `GitManager` or parsing source code with `CompilationManager`, uses **absolute paths**. This avoids ambiguity and makes processing straightforward.

*   **Relative Paths for Graph Operations:** Any operation that queries the Neo4j database (e.g., to find or delete a `:FILE` node) **must** use **relative paths** (from the project root), as this is how paths are stored in the graph.

The `GraphUpdater` orchestrator explicitly manages this conversion at the boundaries. For example, before calling `_purge_stale_graph_data`, the main `update` method converts its lists of absolute paths for dirty and deleted files into relative paths. This ensures each component receives paths in the format it expects.

## 5. Final Step: Update Commit Hash

*   After all phases are complete, the `:PROJECT` node in the graph is updated with the new commit hash, bringing the database's recorded state in sync with the codebase.