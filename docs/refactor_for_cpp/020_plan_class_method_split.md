# C++ Refactor Plan - Step 2: Introduce Class and Method Nodes

## 1. Goal

This is a major step. The goal is to differentiate between C-style constructs and C++ object-oriented constructs. We will:
1.  Split the `FUNCTION` node type into `FUNCTION` (for free functions) and `METHOD` (for class-bound functions).
2.  Split the `DATA_STRUCTURE` node type into `DATA_STRUCTURE` (for C language structs, and all enums/unions in C/C++) and `CLASS_STRUCTURE` (for C++ language classes and structs).
3.  Introduce the `[:HAS_METHOD]` relationship.

## 2. Affected Files

1.  **`clangd_symbol_nodes_builder.py`**: The `SymbolProcessor` will be heavily modified to handle the new node labels and relationships.
2.  **`neo4j_manager.py`**: To add new constraints and update existing ones.
3.  **`clangd_call_graph_builder.py`**: Minor changes to acknowledge the existence of `METHOD` nodes, although full call graph updates will be in a later step.

## 3. Implementation Plan

### 3.1. `neo4j_manager.py`

*   In `create_constraints`, update the existing constraints and add new ones.
    ```python
    constraints = [
        "CREATE CONSTRAINT IF NOT EXISTS FOR (f:FILE) REQUIRE f.path IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (f:FOLDER) REQUIRE f.path IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (fn:FUNCTION) REQUIRE fn.id IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (ds:DATA_STRUCTURE) REQUIRE ds.id IS UNIQUE",
        # Add new constraints
        "CREATE CONSTRAINT IF NOT EXISTS FOR (c:CLASS_STRUCTURE) REQUIRE c.id IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (m:METHOD) REQUIRE m.id IS UNIQUE",
    ]
    ```

### 3.2. `clangd_symbol_nodes_builder.py`

The `SymbolProcessor` class will need significant changes to its dispatch logic.

*   **In `SymbolProcessor._process_and_filter_symbols`**:
    *   The logic will become more complex. Instead of just two kinds of data (function, data_structure), we now need new kinds for each new type: `method`, and `class`.
    *   The loop will classify symbols based on their `kind` from `clangd`:
        *   `Function` -> `FUNCTION` as current
        *   `InstanceMethod`, `StaticMethod`, `Constructor`, `Destructor`, `ConversionFunction` -> `METHOD`
        *   `Class` -> `CLASS_STRUCTURE`
        *   `Struct` -> if lang is Cpp, it's a `CLASS_STRUCTURE`. Otherwise, it's a `DATA_STRUCTURE`.
        *   `Enum`, `Union` -> `DATA_STRUCTURE`  as current
        *   Fields of Class/Struct/Enum/Union -> `FIELD`

*   **In `SymbolProcessor.ingest_symbols_and_relationships`**:
    *   This method will now call new ingestion methods for each node type: `_ingest_class_nodes`, `_ingest_method_nodes`, etc.
    *   It will also call a new method to create the `[:HAS_METHOD]` relationships.

*   **Create `_ingest_class_nodes` and `_ingest_method_nodes`**:
    *   These will be new methods, similar to the existing `_ingest_function_nodes`. They will take their respective data lists and create `:CLASS_STRUCTURE` and `:METHOD` nodes in batched queries.
    *   The `METHOD` node ingestion should include the `body_location` if available, but no Cpp specific properties (`is_static`, `access`, `is_virtual`, `is_const`) for now, because clangd indexer does not output that info for its method symbols. If we need those info, we have to use clang.cindex to parse the source files. We will not do that for now unless we see real needs.

*   **Create `_ingest_has_method_relationships`**:
    *   This new method will be similar to the `_ingest_has_field_relationships` from 010_plan_fields.md.
    *   It will use the `parent_id` (derived from a map table for the `scope` of the `METHOD` symbol) to match `CLASS_STRUCTURE` nodes with their `METHOD` children and create the `[:HAS_METHOD]` relationship.

### 3.3. `clangd_call_graph_builder.py`

*   For now, we only need to make the existing queries aware of the new `METHOD` type to avoid breaking the build. Full integration will come later when we refactor the call graph builder.
*   In `get_call_relation_ingest_query`, the `MATCH` clauses should be updated.
    *   `MATCH (caller:FUNCTION {id: ...})` becomes `MATCH (caller) WHERE (caller:FUNCTION OR caller:METHOD) AND caller.id = ...`
    *   This ensures that if a `METHOD` ID appears as a caller or callee, the query's `MATCH` doesn't fail.
*   **Note**: The `symbol_parser.functions` property was also updated to correctly return both `FUNCTION` and `METHOD` nodes by checking for all callable symbol kinds, ensuring that downstream consumers like the call graph builder have a complete view of all callable symbols.

## 4. Verification

1.  Run the builder on a C++ project.
2.  Verify in Neo4j that `:CLASS_STRUCTURE` and `:METHOD` nodes are created.
3.  Verify that C `structs` are still created as `:DATA_STRUCTURE`, but C++ `class`, `struct` are `:CLASS_STRUCTURE`.
4.  Verify `[:HAS_METHOD]` relationships exist between classes and their methods.
5.  Run the builder on the original C project to ensure no regressions have been introduced. It should still work perfectly.

## 5. Missing Considerations (Future Work)

*   **`SourceSpanProvider` Log Message**: The `SourceSpanProvider` currently logs "Matched and enriched X functions with body spans." This message will become inaccurate as it now enriches data structures and methods too. This is a minor point but worth noting for a later cleanup.
*   **`code_graph_rag_generator.py`**: This file will eventually need to be updated to generate RAG data for `METHOD` and `CLASS_STRUCTURE` nodes. The current queries likely only target `FUNCTION` and `DATA_STRUCTURE`. This is a significant downstream impact that will be addressed in a later step.
