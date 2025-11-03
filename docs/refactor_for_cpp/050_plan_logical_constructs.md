# C++ Refactor Plan - Step 5: Add Logical Constructs

## 1. Goal

This step focuses on adding key logical constructs from our schema that are essential for representing C++ code structure accurately. The goal is to implement:
1.  `NAMESPACE` nodes and their full relationship hierarchy.
2.  A careful and selective ingestion of `VARIABLE` nodes to represent global and namespace-level variables, while correctly identifying static class members as `:FIELD`s.

Implementation of `TYPE_ALIAS` nodes is deferred pending further investigation into reliably linking them to their underlying type.

## 2. Affected Files

1.  **`clangd_symbol_nodes_builder.py`**: To ingest the new nodes and relationships.
2.  **`neo4j_manager.py`**: To add new constraints.

## 3. Implementation Plan

### 3.1. `neo4j_manager.py`

*   In `create_constraints`, add constraints for the new node types:
    ```python
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:NAMESPACE) REQUIRE n.qualified_name IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (v:VARIABLE) REQUIRE v.id IS UNIQUE",
    ```

### 3.2. `clangd_symbol_nodes_builder.py`

This file will be refactored to use a more efficient, single-pass discovery mechanism and to implement the logic for namespaces and variables.

*   **Refactor `ingest_symbols_and_relationships` Orchestration**:
    *   The method will be rewritten to perform a single, efficient **Discovery Phase** loop over all symbols from the parser.
    *   This single loop will populate multiple data structures at once:
        1.  The `scope_name_to_id` map (for class/struct scopes).
        2.  A `set` of all unique namespace qualified names (for creating `:NAMESPACE` nodes).
        3.  A `set` of all unique `(file_path, namespace_scope)` tuples (for creating `(FILE)-[:DECLARES]->(NAMESPACE)` relationships).
    *   After the discovery phase, an **Ingestion Phase** will call a series of new, dedicated methods to create the nodes and relationships in the correct order (nodes first, then relationships).

*   **Namespace Implementation Details**:
    *   **`_ingest_namespace_nodes`**: A new method that takes the set of unique namespace names and creates all `:NAMESPACE` nodes in a batched query.
    *   **`_ingest_namespace_containment`**: A new method that creates the `(parent:NAMESPACE)-[:CONTAINS]->(child:NAMESPACE)` hierarchy.
    *   **`_ingest_file_declarations`**: A new method that uses the set of `(file_path, namespace_scope)` tuples to create the `(FILE)-[:DECLARES]->(NAMESPACE)` relationships.
    *   **`_ingest_symbol_containment`**: A new method that creates the `(NAMESPACE)-[:CONTAINS]->(Symbol)` relationships for functions, classes, etc., based on their `scope` property.

*   **Variable and Static Field Implementation Details**:
    *   The logic in `process_symbol` is updated to handle `kind: 'Variable'` and `kind: 'Field'` with careful disambiguation:
    1.  For `kind: 'Field'`, it is processed as a non-static field, and the property `is_static: false` is explicitly added.
    2.  For `kind: 'Variable'`, it first filters out **function-local variables** by checking if the symbol's `scope` string contains parentheses `(` or `)`.
    3.  For the remaining variables, it checks if the `scope` string (e.g., `MyClass::`) exists as a key in the `scope_name_to_id` map.
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
    *   `MATCH (p:NAMESPACE)-[:CONTAINS]->(c:NAMESPACE) RETURN p.name, c.name LIMIT 10;`
    *   `MATCH (n:NAMESPACE)-[:CONTAINS]->(s) WHERE NOT s:NAMESPACE RETURN n.name, s.name, labels(s) LIMIT 20;`
    *   `MATCH (f:FILE)-[:DECLARES]->(n:NAMESPACE) RETURN f.name, n.name LIMIT 10;`
