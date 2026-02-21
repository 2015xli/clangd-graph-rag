# 030: TypeAlias Ingestion - Detailed Implementation Plan

This document details the implementation steps for the TypeAlias graph ingestion stage: creating `:TYPE_ALIAS` nodes and their associated relationships in Neo4j. The `:TYPE_EXPRESSION` node type has been removed, and the `[:ALIAS_OF]` relationship is now optional, reflecting Solution Option 1 from the high-level plan.

---

### Objective

To modify `clangd_symbol_nodes_builder.py` to ingest `TypeAlias` data, establishing `[:DEFINES_TYPE_ALIAS]` and `[:ALIAS_OF]` relationships, and to update `neo4j_manager.py` accordingly.

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
    *   Add `symbol_data["original_name"] = sym.original_name`.
    *   Add `symbol_data["expanded_from_id"] = sym.expanded_from_id`.
*   We don't maintain qualified_name property in `Symbol` object to avoid the redundancy, so we construct it on the fly.

#### 2. Update `SymbolProcessor.ingest_symbols_and_relationships`

*   This method orchestrates the ingestion process.
*   **Node Ingestion:**
    *   Add `_ingest_nodes_by_label(processed_symbols.get('TYPE_ALIAS', []), "TYPE_ALIAS", neo4j_mgr)` (NEW)

*   **Relationship Ingestion:**
    *   Modify `_ingest_parental_relationships(...)` to include also `DEFINES_TYPE_ALIAS`.
    *   Add`_ingest_alias_of_relationships(self, type_alias_data_list: List[Dict], neo4j_mgr: Neo4jManager)`
        *   This method will create `[:ALIAS_OF]` relationships.
        *   It will iterate through `type_alias_data_list` to filter the TypeAlias symbols that have a valid `aliased_type_id`.
    *   Add `_ingest_expanded_from_relationships(processed_symbols, neo4j_mgr)` (NEW)
        *   This method creates `[:EXPANDED_FROM]` relationships for all generated symbols, including type aliases.

#### 3. Modified Method: `_ingest_defines_relationships`

*   Add `TYPE_ALIAS` to the list of labels that are processed for `:DEFINES` relationships.

### File: `neo4j_manager.py`

#### 1. Update `create_constraints`

*   `CREATE CONSTRAINT IF NOT EXISTS FOR (te:TYPE_ALIAS) REQUIRE te.id IS UNIQUE`

---

### Considerations

*   **Data Flow:** Ensure that the `SymbolProcessor` receives the enriched `Symbol` objects (containing `aliased_canonical_spelling`, `aliased_structure_id`, etc.) from `SourceSpanProvider`.
*   **Batching:** All new ingestion queries must be properly batched for performance.
*   **Error Handling:** Implement robust error handling.
*   **Qualified Name:** Ensure `qualified_name` is correctly passed and stored for `TYPE_ALIAS` nodes.

---
