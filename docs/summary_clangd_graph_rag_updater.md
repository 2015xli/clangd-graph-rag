# Summary: `clangd_graph_rag_updater.py` - Incremental Code Graph RAG Updater

This document summarizes the design and functionality of `clangd_graph_rag_updater.py`. This script is responsible for incrementally updating the Neo4j code graph based on changes in a Git repository.

The logic has been significantly refactored to align with the main graph builder, ensuring consistent and robust updates.

## 1. Purpose

The primary purpose of `clangd_graph_rag_updater.py` is to provide an efficient mechanism for keeping the Neo4j code graph synchronized with an evolving C/C++ codebase. This avoids the computationally expensive process of re-ingesting the entire project for minor changes.

## 2. Core Design: Graph-Based Dependency Analysis

The updater's core logic revolves around a robust, multi-stage process to determine the full impact of any code change. It uses the `[:INCLUDES]` relationships in the graph to find all files affected by a change, making it much more accurate than simple call-graph analysis.

## 3. The Incremental Update Pipeline

The update process is divided into a sequence of high-level phases orchestrated by the `GraphUpdater` class.

### Phase 1 & 2: Identify Full Impact Scope

*   **Component**: `git_manager.GitManager`, `include_relation_provider.IncludeRelationProvider`
*   **Purpose**: To determine the complete set of "dirty files" that need to be re-processed.
*   **Mechanism**:
    1.  **Textual Changes**: First, it calls `GitManager` to get the lists of `added`, `modified`, and `deleted` source files between two commits.
    2.  **Header Impact**: It then passes the modified and deleted headers to the `IncludeRelationProvider`, which queries the existing `[:INCLUDES]` graph to find all source files that transitively depend on those headers.
    3.  **Final Scope**: The "dirty files" set is the union of the textually changed files and the files impacted by header changes.

### Phase 3: Purge Stale Graph Data

*   **Component**: `neo4j_manager.Neo4jManager`
*   **Purpose**: To remove all outdated information from the graph, creating a clean slate for the new data.
*   **Mechanism**: It purges all symbols, relationships, and file nodes associated with the "dirty" and "deleted" files. This now includes a call to `cleanup_orphaned_namespaces()` to correctly handle C++ namespaces that may become empty.

### Phase 4: Rebuild Dirty Scope

*   **Component**: `graph_update_scope_builder.GraphUpdateScopeBuilder`
*   **Purpose**: To surgically "patch" the graph with new, updated information. This is the most complex phase of the update.
*   **Mechanism**: The `GraphUpdater` now delegates the entire rebuilding process to the `GraphUpdateScopeBuilder` module. This new module encapsulates all the logic for this phase:
    1.  It parses the **entire new `clangd` index file** to have a complete view of all symbols.
    2.  It determines the initial "seed symbols" from the dirty files.
    3.  It performs a comprehensive, iterative expansion of the seed set to create a **"sufficient subset"** of symbols, including all necessary C++ dependencies (parent classes, base classes, etc.). This is the core logic that makes C++ updates robust.
    4.  It then runs a "mini" ingestion pipeline on this sufficient subset to update the graph.
*   **Further Reading**: For a detailed explanation of this component, see [`summary_graph_update_scope_builder.md`](./summary_graph_update_scope_builder.md).

### Phase 5: Targeted RAG Update

*   **Component**: `code_graph_rag_generator.RagGenerator`
*   **Purpose**: To efficiently update AI-generated summaries and embeddings.
*   **Mechanism**: After the scope is rebuilt, the `GraphUpdater` calls `summarize_targeted_update()`, providing the set of all symbol IDs from the "mini-parser" as the initial "seed." The `RagGenerator` then intelligently updates summaries for the changed nodes and any parent nodes affected by the change.

## 4. Design Subtlety: Path Management

A critical design principle throughout the updater is the careful management of file paths. The convention is strictly enforced:

*   **Absolute Paths for Processing:** All internal logic, such as identifying changed files with `GitManager` or parsing source code with `CompilationManager`, uses **absolute paths**. This avoids ambiguity and makes processing straightforward.

*   **Relative Paths for Graph Operations:** Any operation that queries the Neo4j database (e.g., to find or delete a `:FILE` node) **must** use **relative paths** (from the project root), as this is how paths are stored in the graph.

The `GraphUpdater` orchestrator explicitly manages this conversion at the boundaries. For example, before calling `_purge_stale_graph_data`, the main `update` method converts its lists of absolute paths for dirty and deleted files into relative paths. This ensures each component receives paths in the format it expects.

## 5. Final Step: Update Commit Hash

*   After all phases are complete, the `:PROJECT` node in the graph is updated with the new commit hash, bringing the database's recorded state in sync with the codebase.
