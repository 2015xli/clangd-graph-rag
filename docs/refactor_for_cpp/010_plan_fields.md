# C++ Refactor Plan - Step 1: Add Fields to Data Structures

## 1. Goal

The goal of this first step is to introduce `FIELD` nodes and connect them to the existing `DATA_STRUCTURE` nodes. This will enrich the graph by modeling the members of C-style `structs` and `unions`. This change is confined to the existing C-language support and serves as a foundational step.

## 2. Affected Files

1.  **`clangd_index_yaml_parser.py`**: To ensure symbols of kind `Field` are parsed correctly.
2.  **`clangd_symbol_nodes_builder.py`**: To add logic for creating `:FIELD` nodes and `[:HAS_FIELD]` relationships.
3.  **`neo4j_manager.py`**: To add a uniqueness constraint for the new `:FIELD` node label.

## 3. Implementation Plan

### 3.1. `neo4j_manager.py`

*   In the `create_constraints` method, a new constraint was added for the `FIELD` node to enforce the uniqueness of its ID.
    ```python
    "CREATE CONSTRAINT IF NOT EXISTS FOR (f:FIELD) REQUIRE f.id IS UNIQUE",
    ```

### 3.2. `clangd_index_yaml_parser.py`

*   **Correction**: The initial assumption that a `Field` symbol's `scope` property contains the direct ID of its parent was incorrect. The `scope` is a string representation (e.g., `MyStruct::`).
*   To handle this, a lookup map must be constructed in the `clangd_symbol_nodes_builder.py` module by iterating through all data structure symbols first. No changes are needed in the parser itself.

### 3.3. `clangd_symbol_nodes_builder.py`

This file saw the most changes for this step.

*   **Create `scope_name_to_id` Map**: In `ingest_symbols_and_relationships`, a lookup map is now created at the beginning of the process. It iterates through all `Struct`, `Class`, and `Union` symbols and maps their scope name (e.g., `MyNamespace::MyStruct::`) to their unique symbol ID.

*   **Refactor `SymbolProcessor._process_and_filter_symbols`**:
    *   This method was refactored to return a single dictionary (`defaultdict(list)`) that groups symbols by their `kind`.
    *   It now requires the `scope_name_to_id` map to be passed down to `process_symbol`.

*   **Update `SymbolProcessor.process_symbol`**:
    *   The method now accepts the `scope_name_to_id` map.
    *   When processing a `Field` symbol, it now correctly looks up the parent ID from the map: `parent_id = scope_name_to_id.get(sym.scope)`.
    *   This `parent_id` is temporarily added to the data dictionary for the field.

*   **Update `SymbolProcessor.ingest_symbols_and_relationships`**:
    *   This method now orchestrates the new flow: it builds the `scope_name_to_id` map, calls the refactored `_process_and_filter_symbols`, and then calls the new ingestion methods for fields.

*   **Create `SymbolProcessor._ingest_field_nodes`**:
    *   This new method creates `:FIELD` nodes in batches.
    *   The Cypher query was optimized to prevent the temporary `parent_id` from being stored on the node, using `apoc.map.removeKey`:
        ```cypher
        UNWIND $field_data AS data
        MERGE (n:FIELD {id: data.id})
        SET n += apoc.map.removeKey(data, 'parent_id')
        ```

*   **Create `SymbolProcessor._ingest_has_field_relationships`**:
    *   This new method creates the `[:HAS_FIELD]` relationships using the `parent_id` from the processed field data.
        ```cypher
        UNWIND $field_data AS data
        MATCH (parent:DATA_STRUCTURE {id: data.parent_id})
        MATCH (child:FIELD {id: data.id})
        MERGE (parent)-[:HAS_FIELD]->(child)
        ```


## 4. Verification

1.  Run the `clangd_graph_rag_builder.py` on a C project.
2.  After ingestion, connect to the Neo4j database.
3.  Execute the query `MATCH (n:FIELD) RETURN n LIMIT 10;` to verify that `:FIELD` nodes have been created with the correct properties.
4.  Execute the query `MATCH (d:DATA_STRUCTURE)-[:HAS_FIELD]->(f:FIELD) RETURN d, f LIMIT 10;` to verify that the relationships are correctly established.
