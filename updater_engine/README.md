# Updater Engine: Incremental Graph Maintenance

This package encapsulates the architectural "brain" and auditing tools for the incremental update process. It is responsible for identifying the minimal, self-contained set of symbols affected by a code change and ensuring the graph remains structurally and semantically consistent.

## Table of Contents
1. [The "Sufficient Subset" Strategy (scope_builder.py)](#1-the-sufficient-subset-strategy-scope_builderpy)
2. [Instrumentation and Auditing (debug_manager.py)](#2-instrumentation-and-auditing-debug_managerpy)

---

## 1. The "Sufficient Subset" Strategy (scope_builder.py)

In a complex C++ project, symbols are highly interdependent. A naive update that only re-ingests modified files would result in "broken" or missing relationships. The `GraphUpdateScopeBuilder` solves this by constructing a **Sufficient Subset**—a self-contained "mini-index".

### The Build Pipeline
The class manages the transition from raw source changes to an enriched, dependency-aware symbol set through two primary phases:

#### Phase A: Scoping (`build_miniparser_for_dirty_scope`)
1.  **Parsing Strategy**: It checks the `SymbolParser` capabilities.
    *   **Modern Clangd**: Performs an incremental parse of only the dirty files.
    *   **Legacy Clangd**: Performs a **full project parse** to gather global function spans, ensuring call-graph accuracy (See "Identity Dependency Note" below).
2.  **Full-Index Enrichment**: Uses the `SymbolEnricher` to enrich the **entire** full symbol set with fresh spans. This is critical because a change in a dirty file might resolve the identity of a symbol in a "clean" header.
3.  **Expansion**: Performs a **Bi-directional 1-Hop Expansion** from the seed symbols.

#### Phase B: Ingestion (`rebuild_mini_scope`)
Once the stale data is purged from Neo4j, this method runs the standard ingestion processors (`PathProcessor`, `SymbolProcessor`, `CallGraphBuilder`) using the mini-parser as the data source.

### The Expansion Algorithm
The algorithm is designed around the principle of ensuring every relationship involving a changed node is correctly re-materialized.

| Relationship | Upward Expansion (Child -> Parent) | Downward Expansion (Parent -> Child) |
| :--- | :--- | :--- |
| **Lexical** | Pull in `parent_id` symbol. | Pull in all members/nested types. |
| **Namespace** | Pull in parent Namespace via `scope`. | Pull in all symbols in that scope. |
| **Calls** | Pull in all `callers`. | Pull in all `callees`. |
| **Inheritance** | Pull in all `base_classes`. | Pull in all `derived_classes`. |
| **Overrides** | Pull in `overridden_methods`. | Pull in `overriding_methods`. |
| **Macros** | Pull in source `MACRO`. | Pull in all expanded symbols. |
| **TypeAlias** | Pull in `aliaser_ids`. | Pull in target `aliased_type_id`. |

#### Identity Dependency Note
To expand the dirty scope via the call graph, we need to know "who calls whom."
*   **Metadata Path**: Modern Clangd (v21+) provides a `Container` field. This allows building the call graph using ONLY the YAML index (fast).
*   **Spatial Path**: Older Clangd only provides coordinates. To find the caller, we must map these to a physical function body, which requires function spans for the **entire project**.

---

## 2. Instrumentation and Auditing (debug_manager.py)

The `GraphDebugManager` provides tools to verify the correctness of the incremental update and diagnose identity collisions.

### APOC Update Triggers
During an update, the manager can install a trigger that tags every newly created or modified node/relationship with the current `commit_hash`.
*   **Usage**: Tagged entities can be queried later to verify exactly what the updater changed.
*   **Cleanup**: Provides methods to remove both the trigger and the temporary `updated` properties.

### Scope Dumping
Provides detailed logs of the "Purged Scope" and the "Updated Scope" for 1:1 verification.
*   **Purge Dump**: Records every node and relationship deleted from the graph.
*   **Collision Detection**: Specifically logs "Colliding Nodes"—symbols whose IDs are in the new dirty scope but whose existing path in the database is in a different file. This is vital for debugging USR-identity migration.
*   **Update Dump**: Records every node and relationship newly created or tagged by the update trigger.

### Verifying Updates
By comparing the `combined_purged_scope.log` with the `updater_updated_scope.log`, a developer can empirically prove that the incremental update produced a result identical to a full build.
