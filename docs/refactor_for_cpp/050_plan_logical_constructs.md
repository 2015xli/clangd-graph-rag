# C++ Refactor Plan - Step 5: Add Logical Constructs

## 1. Goal

This step focuses on adding key logical constructs from our schema that are essential for representing C++ code structure accurately. The goal is to implement:
1.  `NAMESPACE` nodes and their full relationship hierarchy.
2.  A careful and selective ingestion of `VARIABLE` nodes to represent global and namespace-level variables, while correctly identifying static class members as `:FIELD`s.

Implementation of `TYPE_ALIAS` nodes is deferred pending further investigation into reliably linking them to their underlying type.

## 2. Affected Files

1.  **`clangd_symbol_nodes_builder.py`**: To ingest the new nodes and relationships.
2.  **`neo4j_manager.py`**: To add new constraints.

## 3. Implementation Plan (Revised)

### 3.1. `neo4j_manager.py`

*   In `create_constraints`, add constraints for the new node types and properties. The `id` is the primary key for `NAMESPACE`, while `qualified_name` is a secondary unique property for lookups.
    ```python
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:NAMESPACE) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:NAMESPACE) REQUIRE n.qualified_name IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (v:VARIABLE) REQUIRE v.id IS UNIQUE",
    ```

### 3.2. `clangd_symbol_nodes_builder.py`

This file was refactored to follow a cleaner, more correct, and sequenced ingestion flow.

*   **New Orchestration in `ingest_symbols_and_relationships`**:
    1.  **Build Scope Maps**: A new helper method, `_build_scope_maps`, performs a single pass over all in-memory symbols to efficiently create two lookup tables: `qualified_namespace_to_id` and `scope_to_structure_id`.
    2.  **Process All Symbols**: A generic method, `_process_and_filter_symbols`, iterates through all symbols. It calls `process_symbol` which now uses the pre-built maps to enrich each symbol's data with its parent namespace ID (`namespace_id`) where applicable.
    3.  **Ingest All Nodes**: A series of `_ingest_*_nodes` methods are called for each label (`_ingest_namespace_nodes`, `_ingest_function_nodes`, etc.). This ensures all nodes exist in the database before any relationships are created.
    4.  **Ingest All Relationships**: A final series of `_ingest_*_relationships` methods are called. This now includes a consolidated method that efficiently creates all `:SCOPE_CONTAINS` relationships in a single batch operation.

*   **Namespace Implementation Details (Corrected)**:
    *   **Node Creation**: The logic relies exclusively on `!Symbol` documents with `Kind: Namespace`. The `process_symbol` method prepares a data dictionary for each, and `_ingest_namespace_nodes` creates them in the graph.
        *   The node's primary key for `MERGE` is its unique `id` from the YAML.
        *   Properties stored on the node include `id`, `name`, `qualified_name`, `path`, and `name_location` (from the `declaration`).
    *   **File Declarations (`:DECLARES`)**: The `_ingest_file_declarations` method creates the `(FILE)-[:DECLARES]->(NAMESPACE)` link. It iterates through the processed `Namespace` data and uses the `path` from each symbol's `declaration` to find the correct file.
    *   **Hierarchy and Symbol Containment (`:SCOPE_CONTAINS`)**: The creation of all logical containment relationships for namespaces is now handled by a single, efficient process.
        1.  During the `process_symbol` step, a `namespace_id` (the ID of the parent namespace) is added to any symbol whose `scope` directly matches a known namespace's qualified name.
        2.  After all symbols are processed, a single list of `(parent_id, child_id)` pairs is created from this data.
        3.  A new, consolidated method, `_ingest_scope_contains_relationships`, takes this list and batch-ingests all `(parent:NAMESPACE)-[:SCOPE_CONTAINS]->(child)` relationships at once. This single method handles both `NAMESPACE` -> `NAMESPACE` and `NAMESPACE` -> `SYMBOL` containment, replacing the previous, less efficient multi-method approach.

*   **Variable and Static Field Implementation Details**:
    *   The logic in `process_symbol` is updated to handle `kind: 'Variable'` and `kind: 'Field'` with careful disambiguation:
    1.  For `kind: 'Field'`, it is processed as a non-static field, and the property `is_static: false` is explicitly added.
    2.  For `kind: 'Variable'`, it first filters out **function-local variables** by checking if the symbol's `scope` string contains parentheses `(` or `)`.
    3.  For the remaining variables, it checks if the `scope` string (e.g., `MyClass::`) exists as a key in the `scope_to_structure_id` map.
    4.  If **YES**, the symbol is a **static class field**. It is processed as a `:FIELD` node, and the property `is_static: true` is added.
    5.  If **NO**, the symbol is a **global or namespace-level variable**. It is processed as a `:VARIABLE` node.
    *   A new method, `_ingest_variable_nodes`, was created to batch-create these new `:VARIABLE` nodes.
    *   All three `_ingest_defines_relationships_*` methods (`batched_parallel`, `isolated_parallel`, and `unwind_sequential`) were updated to accept a list of variables and create the `(FILE)-[:DEFINES]->(VARIABLE)` relationships.

## 4. `TYPE_ALIAS` Implementation (Deferred)

The implementation of `:TYPE_ALIAS` nodes is postponed. The `clangd` index provides the name of the aliased type as a string (e.g., `type: 'struct OtherType'`), not a direct symbol ID. Reliably resolving this string back to the correct symbol ID in the graph requires a robust name lookup mechanism that needs further design and investigation to avoid ambiguity.

## 5. Verification

1.  Run the builder on a C++ project with namespaces, global variables, and static class variables.
2.  Verify in Neo4j:
    *   `MATCH (n:NAMESPACE) RETURN n LIMIT 10;`
    *   `MATCH (v:VARIABLE) RETURN v LIMIT 10;` (Should only show global/namespace variables).
    *   `MATCH (f:FIELD {is_static: true}) RETURN f LIMIT 10;` (Should show static class members).
3.  Verify relationships:
    *   `MATCH (p:NAMESPACE)-[:SCOPE_CONTAINS]->(c:NAMESPACE) RETURN p.name, c.name LIMIT 10;`
    *   `MATCH (n:NAMESPACE)-[:SCOPE_CONTAINS]->(s) WHERE NOT s:NAMESPACE RETURN n.name, s.name, labels(s) LIMIT 20;`
    *   `MATCH (f:FILE)-[:DECLARES]->(n:NAMESPACE) RETURN f.name, n.name LIMIT 10;`
