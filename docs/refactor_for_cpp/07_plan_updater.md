# C++ Refactor Plan - Step 7: Update Incremental Graph Updater

## 1. Goal

This is the final step in the refactoring process. The goal is to make the incremental updater, `clangd_graph_rag_updater.py`, fully compatible with the new, richer C++ schema. This involves updating the data purging logic and ensuring the "rebuild" phase correctly utilizes the updated builder components.

## 2. Affected Files

1.  **`clangd_graph_rag_updater.py`**: The `GraphUpdater` class needs to be aware of all new node and relationship types.
2.  **`neo4j_manager.py`**: The purging methods need to be updated.

## 3. Implementation Plan

### 3.1. `neo4j_manager.py`

*   **In `purge_symbols_defined_in_files`**:
    *   The current query only deletes `:FUNCTION` and `:DATA_STRUCTURE` nodes. This must be expanded to include all new symbol types that can be defined in a file.
    *   The `WHERE` clause should be updated:
        ```cypher
        // Old
        WHERE s:FUNCTION OR s:DATA_STRUCTURE
        // New
        WHERE s:FUNCTION OR s:METHOD OR s:CLASS_STRUCTURE OR s:DATA_STRUCTURE OR s:FIELD OR s:VARIABLE OR s:TYPE_ALIAS
        ```
    *   We also need to consider `:NAMESPACE` nodes. However, namespaces can be defined across multiple files. Deleting a namespace node just because one file that contributes to it has changed is incorrect. We should **not** delete namespace nodes here. They should be managed separately or assumed to be stable.

### 3.2. `clangd_graph_rag_updater.py`

The `GraphUpdater` orchestrates the process, so its logic needs to be carefully reviewed.

*   **In `_purge_stale_graph_data`**:
    *   This method calls `neo4j_mgr.purge_symbols_defined_in_files`. With the change above, it will now correctly purge all old symbol types. No changes are needed in this method itself.

*   **In `_rebuild_dirty_scope`**:
    *   This method runs a "mini" version of the builder pipeline.
    *   The good news is that because we have refactored the builder components (`SymbolProcessor`, `ClangdCallGraphExtractor`, etc.) in the previous steps, this method should largely work as-is.
    *   When it calls `symbol_processor.ingest_symbols_and_relationships`, the updated `SymbolProcessor` will automatically handle the creation of the new C++ node and relationship types for the symbols in the "dirty" scope.
    *   The same applies to the call graph extractor and other components. The modular design pays off here.

*   **In `_regenerate_summary`**:
    *   This method calls `rag_generator.summarize_targeted_update()`.
    *   The `RagGenerator` was already updated in the previous step to be aware of the new schema.
    *   The `seed_symbol_ids` will now contain IDs for both functions and methods, which the updated `RagGenerator` can handle. The logic for finding affected files and folders remains valid.

## 4. Verification

1.  Create a Git repository with a simple C++ project.
2.  Run the full `clangd_graph_rag_builder.py` to ingest the initial state.
3.  Make a change to a C++ source file (e.g., add a new method to a class, change a method's body).
4.  Commit the change.
5.  Generate a new `clangd` index for the updated state.
6.  Run `clangd_graph_rag_updater.py`, pointing it to the new index file.
7.  Verify in Neo4j:
    *   Check that the old nodes corresponding to the changed file have been removed.
    *   Check that the new nodes (e.g., the new method) have been created correctly.
    *   Check that `[:CALLS]` and `[:HAS_METHOD]` relationships are correct for the updated scope.
    *   Check that the `summary` and `summaryEmbedding` properties have been correctly updated for the changed nodes and their neighbors.
    *   Finally, check that the `commit_hash` on the `:PROJECT` node has been updated to the new commit.
