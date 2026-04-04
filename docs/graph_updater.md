# Summary: `graph_updater.py` - Incremental Code Graph RAG Updater

This document summarizes the design and functionality of `graph_updater.py`. This script is responsible for incrementally updating the Neo4j code graph based on changes in a Git repository.

The logic has been significantly refactored to align with the main graph builder, ensuring consistent and robust updates.

## 1. Purpose

The primary purpose of `graph_updater.py` is to provide an efficient mechanism for keeping the Neo4j code graph synchronized with an evolving C/C++ codebase. This avoids the computationally expensive process of re-ingesting the entire project for minor changes.

## 2. Core Design: Graph-Based Dependency Analysis

The updater's core logic revolves around a robust, multi-stage process to determine the full impact of any code change. It also uses the `[:INCLUDES]` relationships in the graph to find all files affected by a change, making it much more accurate than simple call-graph analysis.

## 3. The Incremental Update Pipeline

The update process is divided into a sequence of high-level phases orchestrated by the `GraphUpdater` class. The pipeline is carefully ordered to allow for easier debugging of the complex scope-building logic before any data is removed from the graph.

### Phase 1 & 2: Identify Full Impact Scope

*   **Component**: `git_manager.GitManager`, `graph_ingester.IncludeRelationProvider`
*   **Purpose**: To determine the complete set of "dirty files" that need to be re-processed.
*   **Mechanism**:
    1.  **Textual Changes**: First, it calls `GitManager` to get the lists of `added`, `modified`, and `deleted` source files between two commits.
    2.  **Header Impact**: It then passes the modified and deleted headers to the `IncludeRelationProvider`, which queries the existing `[:INCLUDES]` graph to find all source files that transitively depend on those headers.
    3.  **Final Scope**: The "dirty files" set is the union of the textually changed files and the files impacted by header changes.

### Phase 3: Build the "Sufficient Subset" for the Update

*   **Component**: `updater_engine.GraphUpdateScopeBuilder`
*   **Purpose**: To determine the precise, self-contained set of symbols required to correctly patch the graph.
*   **Mechanism**: This is the most complex logical step. The `GraphUpdater` delegates this to the `GraphUpdateScopeBuilder`, which performs the following actions:
    1.  It parses the **entire new `clangd` index file** to have a complete, in-memory view of all symbols in the new commit.
    2.  It parses the source code of **only the dirty files** to get fresh information about their structure (function spans, parent-child relationships).
    3.  It enriches the **entire, full symbol set** with this new structural information. This is a critical step to ensure dependency analysis is performed on the most up-to-date view of the code.
    4.  It identifies the "seed symbols" (those defined in the dirty files) and expands this set by one level of dependencies (e.g., parents, children, callers, callees, base classes) to create a "sufficient subset".
    5.  The result is a new, small `SymbolParser` object (the "mini-parser") containing only the symbols needed for the update.
*   **Further Reading**: For a detailed explanation, see [`Updater Engine`](../updater_engine/README.md).

### Phase 4: Purge Stale Graph Data

*   **Component**: `neo4j_manager.Neo4jManager`
*   **Purpose**: To remove all outdated information from the graph, creating a clean slate for the new data.
*   **Mechanism**: It purges all symbols, relationships, and file nodes associated with the "dirty" and "deleted" files.
*   **Design Note**: This purge is intentionally performed *after* the sufficient subset has been built. This allows a developer to more easily debug the scope-building logic by comparing the in-memory "mini-parser" against the existing, un-purged data in the graph.

### Phase 5: Rebuild Dirty Scope (Ingestion)

*   **Component**: `updater_engine.GraphUpdateScopeBuilder`
*   **Purpose**: To surgically "patch" the graph with the new, updated information.
*   **Mechanism**: The `GraphUpdater` calls the `rebuild_mini_scope()` method on the scope builder, which runs the complete ingestion pipeline (`PathProcessor`, `SymbolProcessor`, `ClangdCallGraphExtractor`, etc.) using the "mini-parser" as its data source.

### Phase 6: Wrap Up the Graph

*   **Component**: `neo4j_manager.Neo4jManager`
*   **Purpose**: To finalize the graph state, before updating the RAG summaries
*   **Mechanism**:
    1.  **Cleanup**: Orphan nodes that may have been created during the patch are removed. (Should be zero in normal operation)
    2.  **Update Commit**: The `:PROJECT` node in the graph is updated with the new commit hash, bringing the database's recorded state in sync with the codebase.

### Phase 7: Incremental RAG Summary Update

*   **Component**: `summary_driver.IncrementalSummarizer`
*   **Purpose**: To update AI-generated data for the changed nodes.
*   **Mechanism**: If enabled, `GraphUpdater` calls `IncrementalSummarizer.summarize_targeted_update()`, providing the set of all symbol IDs from the "mini-parser" as the initial "seed." The `RagGenerator` then intelligently updates summaries for the changed nodes and any parent nodes affected by the change.

### Phase 8: Add Agent Schema
*   **Component**: `neo4j_manager.Neo4jManager`
*   **Purpose**: To add the `:ENTITY` label and unified vector indexes to facilitate AI agent reasoning.