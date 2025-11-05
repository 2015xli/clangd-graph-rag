# C++ Refactor Plan - Step 7: Update Incremental Graph Updater

## 1. Goal

This is the final step in the refactoring process. The goal is to make the incremental updater, `clangd_graph_rag_updater.py`, fully compatible with the new, richer C++ schema. This involves updating the data purging logic and ensuring the "rebuild" phase correctly utilizes the updated builder components.

## 2. Affected Files

1.  **`clangd_graph_rag_updater.py`**: The `GraphUpdater` class needs to be aware of all new node and relationship types.
2.  **`neo4j_manager.py`**: The purging methods need to be updated.

## 3. Implementation Plan

### 3.1. `neo4j_manager.py` (Implemented)

*   **In `purge_symbols_defined_in_files`**:
    *   The `WHERE` clause has been updated to include all new C++ symbol types that can be defined in a file.
        ```cypher
        WHERE s:FUNCTION OR s:METHOD OR s:CLASS_STRUCTURE OR s:DATA_STRUCTURE OR s:FIELD OR s:VARIABLE
        ```
    *   **Note**: `:TYPE_ALIAS` is excluded as it is not part of the current schema. `:NAMESPACE` nodes are also explicitly excluded from this purge, as they can be defined across multiple files and are handled by a dedicated cleanup.

*   **New Method: `cleanup_orphaned_namespaces()`**:
    *   **Purpose**: Deletes `NAMESPACE` nodes that are no longer declared by any file and contain no other nodes.
    *   **Mechanism**: This method runs an iterative Cypher query to handle cascading deletions of nested namespaces. It repeatedly identifies and deletes `NAMESPACE` nodes that satisfy both conditions:
        1.  `NOT EXISTS((ns)<-[:DECLARES]-(:FILE))`: No files declare this namespace anymore.
        2.  `NOT EXISTS((ns)-[:CONTAINS]->())`: It contains no other nodes (including sub-namespaces or symbols).
    *   This iterative approach ensures that orphaned namespace trees are correctly pruned from the bottom up.

### 3.2. `clangd_graph_rag_updater.py` (Implemented)

The `GraphUpdater` orchestrates the process, so its logic has been carefully reviewed and updated.

*   **In `_purge_stale_graph_data`**:
    *   This method calls `neo4j_mgr.purge_symbols_defined_in_files` and `neo4j_mgr.purge_files`. After these operations, it now explicitly calls `neo4j_mgr.cleanup_orphaned_namespaces()` to remove any `NAMESPACE` nodes that have become orphaned.

*   **In `_rebuild_dirty_scope`**:
    *   The existing calls to `symbol_processor.ingest_symbols_and_relationships`, the call graph extractor, and other components automatically handle the new C++ node and relationship types for the symbols in the "dirty" scope.
    *   **Modification**: The collection of `rag_seed_ids` has been expanded to include `METHOD` and `CLASS_STRUCTURE` nodes from the `mini_symbol_parser`, ensuring all relevant C++ symbols are considered for targeted RAG updates.
        ```python
        rag_seed_ids = {s.id for s in mini_symbol_parser.symbols.values()
                        if s.is_function() or s.kind == 'Class'}
        ```

*   **In `_regenerate_summary`**:
    *   The `RagGenerator` constructor call has been updated to pass the `max_context_size` argument, which is now required.
        ```python
        rag_generator = RagGenerator(
            neo4j_mgr=self.neo4j_mgr,
            project_path=self.project_path,
            llm_client=llm_client,
            embedding_client=embedding_client,
            num_local_workers=self.args.num_local_workers,
            num_remote_workers=self.args.num_remote_workers,
            max_context_size=self.args.max_context_size,
        )
        ```

## 4. Verification

1.  Create a Git repository with a simple C++ project.
2.  Run the full `clangd_graph_rag_builder.py` to ingest the initial state.
3.  Make a change to a C++ source file (e.g., add a new method to a class, change a method's body, add a new class, delete a file that declares a namespace).
4.  Commit the change.
5.  Generate a new `clangd` index for the updated state.
6.  Run `clangd_graph_rag_updater.py`, pointing it to the new index file.
7.  Verify in Neo4j:
    *   Check that the old nodes corresponding to the changed file have been removed.
    *   Check that the new nodes (e.g., the new method, new class) have been created correctly.
    *   Check that `[:CALLS]`, `[:HAS_METHOD]`, `[:HAS_FIELD]`, `[:INHERITS]`, and `[:OVERRIDDEN_BY]` relationships are correct for the updated scope.
    *   **Check that orphaned `NAMESPACE` nodes have been correctly deleted.**
    *   Check that the `summary` and `summaryEmbedding` properties have been correctly updated for the changed nodes and their neighbors, including `METHOD`, `CLASS_STRUCTURE`, and `NAMESPACE` nodes.
    *   Finally, check that the `commit_hash` on the `:PROJECT` node has been updated to the new commit.
