# `graph_update_scope_builder.py`

## 1. Purpose

This module provides the `GraphUpdateScopeBuilder` class, a specialized component responsible for executing the most complex phase of an incremental graph update: **building the "sufficient subset" of symbols** needed to correctly patch the graph.

When files in a project are changed, the `clangd_graph_rag_updater` determines the set of "dirty files". It then delegates the task of figuring out the precise scope of the update to this module. The `GraphUpdateScopeBuilder` identifies all symbols directly affected by the changes and expands that set to include their direct dependencies (parents, children, base classes, etc.). The result is a small, self-contained `SymbolParser` object ("mini-parser") that is passed back to the updater to perform the actual graph ingestion.

This encapsulation improves the project architecture by separating the high-level orchestration of the update process from the complex, low-level logic of dependency scope determination.

## 2. Key Components & Logic

### `GraphUpdateScopeBuilder` Class

This is the main class that orchestrates the scope-building process.

#### `build_miniparser_for_dirty_scope()` Method

This is the primary public method and contains the core logic for determining the update scope. It receives the set of "dirty files" and the full, up-to-date `SymbolParser` object. It performs the following critical steps in order:

1.  **Parse Dirty Files**: It first calls the `CompilationManager` to parse the source code of *only* the dirty files. This provides the most up-to-date source code information (function body spans, lexical parent/child relationships, and include directives) for the changed parts of the codebase.

2.  **Enrich Full Symbol Set**: **This is a critical step for correctness.** It then uses the `SourceSpanProvider` to enrich the **entire `full_symbol_parser` object** with the fresh information gathered in the previous step. By updating the full set of symbols, it ensures that any changes to lexical structure (e.g., a function moving into a class, a class gaining a new parent) are reflected *before* dependency analysis occurs.

3.  **Identify Seed Symbols**: With the full symbol set now accurately reflecting the new state of the code, it identifies the initial "seed symbols"—those directly defined within the dirty files.

4.  **Create Sufficient Subset**: It calls the internal `_create_sufficient_subset()` method to expand this seed set into a complete, self-contained group of symbols and returns the resulting "mini-parser".

#### `rebuild_mini_scope()` Method

This method is called by the updater *after* the old data has been purged from the graph. Its role is to take the `mini_symbol_parser` created by the previous method and run the actual ingestion processors (`PathProcessor`, `SymbolProcessor`, `IncludeRelationProvider`, `ClangdCallGraphExtractor`) on it to surgically patch the graph with the new and updated information.

#### `_create_sufficient_subset()` Method

This is the core intellectual property of the incremental update feature. Its goal is to expand the small set of seed symbols to include every other symbol required to correctly reconstruct all relationships, even if those other symbols reside in unchanged files.

**Algorithm:**

1.  **Pre-computation:** For performance, it first scans the full (and now fully enriched) symbol list to build several temporary, in-memory lookup tables for all possible relationships:
    *   A `parent_id` to children map for lexical containment.
    *   A `scope` name to ID map for namespace containment.
    *   Bi-directional graphs for inheritance (`:INHERITS`) and method overrides (`:OVERRIDDEN_BY`).
    *   Bi-directional call graph maps (callers and callees).

2.  **Single-Level Dependency Expansion:** The algorithm performs a **1-hop expansion** from the initial seed set. It iterates through each seed symbol and uses the lookup tables to find all directly related symbols (its callers, callees, parent class, base classes, derived classes, children, etc.). All newly discovered symbols are added to a final set. This ensures the subset is self-contained for rebuilding all direct relationships of the changed symbols.