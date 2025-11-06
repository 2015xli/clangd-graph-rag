# `graph_update_scope_builder.py`

## Purpose

This module provides the `GraphUpdateScopeBuilder` class, a specialized component responsible for executing the most complex phase of an incremental graph update: **rebuilding the dirty scope**.

When files in a project are changed, the `clangd_graph_rag_updater` purges the stale data associated with those files. It then delegates the task of re-ingesting the new information to this module. The `GraphUpdateScopeBuilder` intelligently determines the precise set of symbols that need to be re-processed and runs a targeted, "mini" ingestion pipeline on them.

This encapsulation improves the project architecture by separating the high-level orchestration of the update process from the complex, low-level logic of scope determination and rebuilding.

## Key Components & Logic

### `GraphUpdateScopeBuilder` Class

This is the main class that orchestrates the rebuilding process.

#### `rebuild_dirty_scope()` Method

This is the primary public method. It receives the set of "dirty files" (files that were added, modified, or impacted by a header change) and the full, up-to-date `SymbolParser` object. It performs the following steps:

1.  Parses the source code of *only* the dirty files to get fresh span and include information.
2.  Identifies the initial "seed symbols"—those directly defined within the dirty files.
3.  Calls the internal `_create_sufficient_subset()` method to expand this seed set into a complete, self-contained group of symbols.
4.  Runs the full ingestion pipeline (`PathProcessor`, `SymbolProcessor`, `IncludeRelationProvider`, `ClangdCallGraphExtractor`) on this "mini" subset of symbols to patch the graph.

#### `_create_sufficient_subset()` Method

This is the core intellectual property of the incremental update feature. Its goal is to expand the small set of seed symbols to include every other symbol required to correctly reconstruct all relationships, even if those other symbols reside in unchanged files.

It solves the critical challenge of C++ updates, where a change to a single method can require its parent class, base classes, and other related symbols to be present for a correct update.

**Algorithm:**

1.  **Pre-computation:** For performance, it first scans the full symbol list to build several temporary, in-memory lookup tables:
    *   A `scope_to_structure_id` map to find parent classes/structs.
    *   Bi-directional graphs for inheritance (`:INHERITS`) and method overrides (`:OVERRIDDEN_BY`).
    *   Bi-directional call graph maps (callers and callees), created by reusing the refactored `ClangdCallGraphExtractor`.

2.  **Iterative Expansion:** It initializes a queue with the seed symbols and loops until the queue is empty. In each iteration, it uses the lookup tables to find all symbols related to the current symbol (its callers, callees, parent class, base classes, derived classes, etc.) and adds any newly discovered symbols back to the queue.

This iterative traversal ensures the entire dependency chain is explored, resulting in a truly "sufficient" subset for rebuilding the graph.
