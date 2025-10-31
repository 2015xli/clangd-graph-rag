# C++ Refactor Plan - Step 5: Add Logical Constructs

## 1. Goal

This step focuses on adding the remaining logical constructs from our schema that are essential for representing C++ code structure accurately. The goal is to implement:
1.  `NAMESPACE` nodes and their relationships.
2.  `VARIABLE` nodes for global/namespace-level variables.
3.  `TYPE_ALIAS` nodes for C++ `using` aliases.

## 2. Affected Files

1.  **`clangd_index_yaml_parser.py`**: To parse the new symbol kinds.
2.  **`clangd_symbol_nodes_builder.py`**: To ingest the new nodes and relationships.
3.  **`neo4j_manager.py`**: To add new constraints.

## 3. Implementation Plan

### 3.1. `neo4j_manager.py`

*   In `create_constraints`, add constraints for the new node types.
    ```python
    "CREATE CONSTRAINT IF NOT EXISTS FOR (n:NAMESPACE) REQUIRE n.qualified_name IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (v:VARIABLE) REQUIRE v.id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (t:TYPE_ALIAS) REQUIRE t.id IS UNIQUE",
    ```

### 3.2. `clangd_index_yaml_parser.py`

*   The parser is already generic enough to handle these new `kind`s (`Namespace`, `Variable`, `TypeAlias`). No changes are likely needed, but we must be aware of these kinds in the builder.

### 3.3. `clangd_symbol_nodes_builder.py`

This file will require another round of significant updates to `SymbolProcessor`.

*   **In `SymbolProcessor._process_and_filter_symbols`**:
    *   Add new lists for the new types: `namespace_list`, `variable_list`, `type_alias_list`.
    *   Update the classification logic to populate these lists based on the symbol `kind`.

*   **In `SymbolProcessor.ingest_symbols_and_relationships`**:
    *   Add calls to new ingestion methods: `_ingest_namespace_nodes`, `_ingest_variable_nodes`, `_ingest_type_alias_nodes`.
    *   Add calls for new relationship methods: `_ingest_namespace_containment`, `_ingest_file_declarations`, `_ingest_alias_relationships`.

*   **Create Ingestion Methods for New Nodes**:
    *   Create `_ingest_namespace_nodes`, `_ingest_variable_nodes`, and `_ingest_type_alias_nodes`.
    *   These will follow the same batched `UNWIND` + `MERGE` pattern as the other node ingestion methods.
    *   For `NAMESPACE`, we must construct the `qualified_name` during processing, as it's the unique key.

*   **Create Ingestion Methods for New Relationships**:
    *   **`_ingest_namespace_containment`**: This method will create `[:CONTAINS]` relationships between namespaces. It can iterate through the `namespace_list`. For a namespace with `scope` 'A' and `name` 'B', it creates a `(NAMESPACE {name:'A'})-[:CONTAINS]->(NAMESPACE {name:'A::B'})` relationship.
    *   **`_ingest_file_declarations`**: This method creates the `[:DECLARES_IN]` relationship. It needs to associate files with the namespaces they contribute to. This can be derived by looking at the `file_path` and `scope` of all symbols.
    *   **`_ingest_alias_relationships`**: This method creates the `[:ALIASES_TYPE]` relationship. The `TypeAlias` symbol from `clangd` usually has an `underlying_type` property that contains the ID of the type it aliases. The query will match the `TYPE_ALIAS` node and the `CLASS_STRUCTURE` or `DATA_STRUCTURE` node by their respective IDs and create the relationship.

*   **Update `DEFINES` Relationship**:
    *   The generic `(FILE)-[:DEFINES]->(Symbol)` relationship should be updated to include the new symbol types like `VARIABLE` and `TYPE_ALIAS`.

## 4. Verification

1.  Run the builder on a C++ project with namespaces, global variables, and type aliases.
2.  Verify in Neo4j:
    *   `MATCH (n:NAMESPACE) RETURN n LIMIT 10;`
    *   `MATCH (n:VARIABLE) RETURN n LIMIT 10;`
    *   `MATCH (n:TYPE_ALIAS) RETURN n LIMIT 10;`
3.  Verify relationships:
    *   `MATCH (n:NAMESPACE)-[:CONTAINS]->(m) RETURN n,m LIMIT 10;` (Check for nested namespaces and contained classes/functions).
    *   `MATCH (f:FILE)-[:DECLARES_IN]->(n:NAMESPACE) RETURN f,n LIMIT 10;`
    *   `MATCH (t:TYPE_ALIAS)-[:ALIASES_TYPE]->(c) RETURN t,c LIMIT 10;`
