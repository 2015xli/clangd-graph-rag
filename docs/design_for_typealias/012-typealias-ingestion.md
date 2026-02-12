# 012: TypeAlias Ingestion - Detailed Implementation Plan

This document details the implementation steps for the TypeAlias graph ingestion stage: creating `:TYPE_ALIAS` and `:TYPE_EXPRESSION` nodes and their associated relationships in Neo4j.

---

### Objective

To modify `clangd_symbol_nodes_builder.py` to ingest `TypeAlias` and `TypeExpression` data, establishing `[:DEFINES_TYPE_ALIAS]` and `[:ALIAS_OF]` relationships.

### File: `clangd_symbol_nodes_builder.py`

#### 1. Update `SymbolProcessor.process_symbol`

*   This method transforms `Symbol` objects into dictionaries for Neo4j ingestion.
*   **For `sym.kind == 'TypeAlias'`:**
    *   Set `symbol_data["node_label"] = "TYPE_ALIAS"`.
    *   Add `symbol_data["aliased_canonical_spelling"] = sym.aliased_canonical_spelling`.
    *   Add `symbol_data["aliased_type_id"] = sym.aliased_type_id`.
    *   Add `symbol_data["aliased_type_kind"] = sym.aliased_type_kind`.
    *   Add `symbol_data["parent_id"] = sym.parent_id`.
    *   Add `symbol_data["scope"] = sym.scope`.
    *   Add `symbol_data["qualified_name"] = sym.scope + sym.name`.

#### 2. Update `SymbolProcessor.ingest_symbols_and_relationships`

*   This method orchestrates the ingestion process.
*   **Node Ingestion Order (Modified):**
    *   `_ingest_namespace_nodes(...)`
    *   `_ingest_data_structure_nodes(...)`
    *   `_ingest_class_nodes(...)`
    *   `_ingest_type_expression_nodes(...)` (NEW - must happen before `_ingest_type_alias_nodes`)
    *   `_ingest_nodes_by_label(processed_symbols.get('TYPE_ALIAS', []), "TYPE_ALIAS", neo4j_mgr)` (NEW) 
    *   `_dedup_nodes(...)`
    *   `_ingest_nodes_by_label(processed_symbols.get('FUNCTION', []), "FUNCTION", neo4j_mgr)` 
    *   `_ingest_nodes_by_label(processed_symbols.get('METHOD', []), "METHOD", neo4j_mgr)` 
    *   `_ingest_nodes_by_label([f for f in processed_symbols.get('FIELD', []) if 'parent_id' in f], "FIELD", neo4j_mgr)` 
    *   `_ingest_nodes_by_label(processed_symbols.get('VARIABLE', []), "VARIABLE", neo4j_mgr)` 
*   **Relationship Ingestion Order (Modified):**
    *   `_ingest_parental_relationships(...)` (includes `SCOPE_CONTAINS` and `HAS_NESTED`)
    *   `_ingest_file_namespace_declarations(...)`
    *   `_ingest_other_declares_relationships(...)`
    *   `_ingest_defines_relationships(...)` (Modified to include `:DEFINES` relationshio to `TYPE_ALIAS` nodes)
    *   `_ingest_has_member_relationships([f for f in processed_symbols.get('FIELD', []) if 'parent_id' in f], "FIELD", "HAS_FIELD", neo4j_mgr)` 
    *   `_ingest_has_member_relationships([m for m in processed_symbols.get('METHOD', []) if 'parent_id' in m], "METHOD", "HAS_METHOD", neo4j_mgr)` 
    *   `_ingest_inheritance_relationships(...)`
    *   `_ingest_override_relationships(...)`
    *   `_ingest_alias_of_relationships(...)` (NEW, ingests `:ALIAS_OF` relationships)
    *   `_ingest_defines_type_alias_relationships(...)` (NEW, ingests `:DEFINES_TYPE_ALIAS` relationships)

#### 3. New Method: `_ingest_type_expression_nodes(self, type_alias_data_list: List[Dict], neo4j_mgr: Neo4jManager)`

*   This method will collect unique `aliased_canonical_spelling` values from `TypeAlias` symbols where `aliased_type_kind` is "TypeExpression".
*   For each unique spelling, it will create a `:TYPE_EXPRESSION` node.
*   **Properties:** `id` (hash of `type://<spelling>`), `name` (spelling), `kind` ("TypeExpression").
*   **Query:**
    ```cypher
    UNWIND $data AS data
    MERGE (n:TYPE_EXPRESSION {id: data.id})
    SET n.name = data.name, n.kind = data.kind
    ```
*   **Batching:** Implement standard batching.

#### 4. New Method: `_ingest_alias_of_relationships(self, type_alias_data_list: List[Dict], neo4j_mgr: Neo4jManager)`

*   This method will create `[:ALIAS_OF]` relationships.
*   It will iterate through `type_alias_data_list`.
*   For each `TypeAlias` symbol:
    *   `MATCH (alias:TYPE_ALIAS {id: data.id})`
    *   `MATCH (aliasee) WHERE aliasee.id = data.aliased_type_id AND (aliasee:CLASS_STRUCTURE OR aliasee:DATA_STRUCTURE OR aliasee:TYPE_EXPRESSION OR aliasee:TYPE_ALIAS)`
    *   `MERGE (alias)-[:ALIAS_OF]->(aliasee)`
*   **Batching:** Implement standard batching.

#### 5. New Method: `_ingest_defines_type_alias_relationships(self, type_alias_data_list: List[Dict], neo4j_mgr: Neo4jManager)`

*   Group the `type_alias_data_list` by `parent_id` and `child_id` in `_ingest_parental_relationships`. Then, reuse the same logic as for `_ingest_scope_contains_relationships` to ingest the `:DEFINES_TYPE_ALIAS` relationships.


### File: `neo4j_manager.py`

#### 1. Update `create_constraints`

*   Add new constraints for `TYPE_ALIAS` and `TYPE_EXPRESSION` nodes:
    *   `CREATE CONSTRAINT IF NOT EXISTS FOR (ta:TYPE_ALIAS) REQUIRE ta.id IS UNIQUE`
    *   `CREATE CONSTRAINT IF NOT EXISTS FOR (te:TYPE_EXPRESSION) REQUIRE te.id IS UNIQUE`

---

### Considerations

*   **Data Flow:** Ensure that the `SymbolProcessor` receives the enriched `Symbol` objects (containing `aliased_canonical_spelling`, `aliased_structure_id`, etc.) from `SourceSpanProvider`.
*   **Batching:** All new ingestion queries must be properly batched for performance.
*   **Error Handling:** Implement robust error handling.
*   **Qualified Name:** Ensure `qualified_name` is correctly passed and stored for `TYPE_ALIAS` nodes.

---
