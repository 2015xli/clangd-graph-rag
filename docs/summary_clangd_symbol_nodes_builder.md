# Algorithm Summary: `clangd_symbol_nodes_builder.py`

## 1. Role in the Pipeline

This module provides the `SymbolProcessor` class, which is responsible for the most complex part of the graph construction: **ingesting all logical code symbols and their intricate web of relationships**. It acts as the engine for **Pass 4** of the full ingestion pipeline, running after symbols have been parsed (`SymbolParser`) and enriched with lexical data (`SourceSpanProvider`).

Its purpose is to transform the final, in-memory collection of `Symbol` objects into a rich, interconnected graph in Neo4j. The logic for processing file and folder paths, which precedes this pass, resides in the separate `path_processor.py` module.

## 2. High-Level Workflow: `ingest_symbols_and_relationships`

The `SymbolProcessor`'s main entry point orchestrates a three-phase process designed for clarity, performance, and correctness:

1.  **Phase 1: Data Preparation**: All raw `Symbol` objects are processed, enriched with linking information, and grouped into a structure optimized for ingestion. This includes capturing **macro causality** (`original_name`, `expanded_from_id`) and **type alias targets**.
2.  **Phase 2: Node Ingestion**: All symbol nodes (`:FUNCTION`, `:CLASS_STRUCTURE`, `:MACRO`, `:TYPE_ALIAS`, etc.) are created in the database. A special de-duplication step is run here.
3.  **Phase 3: Relationship Ingestion**: All relationships between the newly created nodes (`:HAS_METHOD`, `:EXPANDED_FROM`, `:ALIAS_OF`, `:DEFINES`, etc.) are created in a specific, optimized order.

---

## 3. Deep Dive: Data Preparation

*   **Problem**: How to efficiently process tens of thousands of raw `Symbol` objects into a format ready for Neo4j, while also resolving necessary information for relationship linking (like namespace or lexical containment) *before* any database writes occur?

*   **Solution**: A multi-step preparation pipeline is used:
    1.  **`_build_scope_maps`**: This first pre-processes all symbols to create a critical lookup table that maps fully qualified namespace names (e.g., `std::chrono::`) to their unique symbol IDs. This is essential because a symbol's `scope` property is a string, not an ID, and this map allows for the later creation of `(:NAMESPACE)-[:SCOPE_CONTAINS]->(Symbol)` relationships.
    2.  **`process_symbol`**: This method acts as a powerful translator, converting a `Symbol` object into a dictionary. During this translation, it:
        *   **Filters** out symbols defined outside the project path (except for `Namespace` symbols).
        *   **Assigns a `node_label`**: This maps the `clangd` `kind` to a specific Neo4j label. Notably, it handles the new **`:MACRO`** and **`:TYPE_ALIAS`** labels.
        *   **Captures Causality**: It attaches the **`original_name`** property (the macro invocation text) and the **`expanded_from_id`** to the data dictionary for generated symbols.
        *   **Type Alias Resolution**: For aliases, it includes **`aliased_type_id`**, **`aliased_type_kind`**, and **`aliased_canonical_spelling`**.
        *   **Attaches Temporary IDs**: It attaches linking properties like **`parent_id`** and **`namespace_id`**. These are used only for relationship creation in Phase 3 and are not stored on the final node.
    3.  **`_process_and_group_symbols`**: The final preparation step groups all the processed symbol dictionaries by their assigned `node_label`. This creates a clean, organized data structure (e.g., a list of all `FUNCTION` data, a list of all `CLASS_STRUCTURE` data) ready for efficient batch ingestion.

*   **Benefit**: This "prepare-then-ingest" approach is highly efficient. It ensures all data is validated, grouped, and enriched with temporary linking IDs in-memory before a single node is created, minimizing complex queries during the database writing phase.

---

## 4. Deep Dive: Node and Relationship Ingestion

### 4.1. Unified Node Ingestion

*   **Problem**: Ingesting over ten different types of symbol nodes (`:FUNCTION`, `:METHOD`, `:CLASS_STRUCTURE`, etc.) could lead to a large amount of duplicated code.

*   **Solution**: A single, generic method, **`_ingest_nodes_by_label`**, is used for all node creation.
    *   It accepts a list of data dictionaries and a `label` string.
    *   It uses a generic Cypher query (`MERGE (n:{label} {{id: d.id}})...`) to create nodes in batches.
    *   For **`:MACRO`** nodes, it writes the `macro_definition` and `is_function_like` properties.
    *   For **`:TYPE_ALIAS`** nodes, it writes the aliased type metadata.
    *   It intelligently uses `apoc.map.removeKeys` to prevent temporary linking properties like `parent_id` and `namespace_id` from being written to the node itself.

*   **Benefit**: This unified design dramatically reduces code duplication and makes the system easier to maintain. Adding a new symbol type in the future only requires updating the label mapping, not writing a new ingestion method.

### 4.2. De-duplication of Structs/Classes

*   **Problem**: A header file might be included by both C and C++ source files. This can cause the `ClangParser` to see the same `struct` in two different language contexts, leading to the creation of two nodes with the same `id` but different labels (`:DATA_STRUCTURE` and `:CLASS_STRUCTURE`).
*   **Solution**: After all nodes are ingested, the `_dedup_nodes` method runs a specific query: `MATCH (ds:DATA_STRUCTURE), (cs:CLASS_STRUCTURE {id: ds.id}) DETACH DELETE ds`. This finds and removes the C-style `:DATA_STRUCTURE` node if a C++-style `:CLASS_STRUCTURE` with the same ID exists.
*   **Benefit**: This ensures the C++ representation is preferred when ambiguity exists, maintaining a clean and accurate graph without duplicates. This is particularly important for macro-generated structs that might appear at the same location as their typedefs.

### 4.3. Multi-Stage Relationship Ingestion

*   **Problem**: The graph's value comes from its rich relationships. How can these be created efficiently and correctly after the nodes exist, using the temporary IDs attached during data preparation?

*   **Solution**: After all nodes are created, a series of specialized methods are called to create each type of relationship in batches.
    1.  **Parental Relationships (`:SCOPE_CONTAINS`, `:HAS_NESTED`, `:DEFINES_TYPE_ALIAS`)**: The `_ingest_parental_relationships` method uses the `namespace_id` and `parent_id` on the processed symbol data. It pre-groups the relationships in Python by the `(parent_label, child_label)` combination. 
    2.  **Causality Relationship (`:EXPANDED_FROM`)**: The `_ingest_expanded_from_relationships` method creates links from generated symbols back to their source `:MACRO` nodes using the `expanded_from_id`.
    3.  **Alias Relationship (`:ALIAS_OF`)**: The `_ingest_alias_of_relationships` method creates links from `:TYPE_ALIAS` nodes to their targets (classes, structs, or other aliases).
    4.  **File Relationships (`:DEFINES`, `:DECLARES`)**: These methods link files to the symbols they contain. The `:DEFINES` relationship logic includes **`:MACRO`** and **`:TYPE_ALIAS`** in its scope.
    5.  **Member & Inheritance Relationships**: Other methods follow a similar pattern to create `:HAS_FIELD`, `:HAS_METHOD`, `:INHERITS`, and `:OVERRIDDEN_BY` edges.

*   **Benefit**: This multi-stage approach breaks down a highly complex task into a series of clear, manageable, and individually optimized steps, resulting in a correctly and efficiently constructed graph.

### 4.4. Deep Dive: The `:DEFINES` Relationship Strategies

*   **Problem**: Creating the `(FILE)-[:DEFINES]->(Symbol)` relationship for every symbol in the project is a major performance challenge. A naive parallel approach can easily cause database deadlocks when multiple threads try to acquire a write lock on the same `:FILE` node simultaneously.
*   **Solution**: The system offers two distinct strategies, controlled by the `--defines-generation` flag, allowing the user to choose the best trade-off between speed and safety for their environment.
    1.  **`unwind-sequential`**: This is a simple and safe strategy that uses standard, non-APOC Cypher. It processes batches of relationships sequentially using `UNWIND` and `MERGE`. While not parallel, it is idempotent and easy to debug.
    2.  **`isolated-parallel`**: This is the deadlock-safe parallel strategy. It first groups all relationships by their source `:FILE` node on the client side. These groups are then passed to `apoc.periodic.iterate` with `parallel: true`. Because all relationships for a given file are processed within a single group by a single thread, no two parallel threads will ever contend for the same `:FILE` node, completely eliminating the risk of deadlocks.
*   **Benefit**: This provides tunable performance. The `isolated-parallel` strategy allows for significant speedups on multi-core machines during large-scale ingestion, while the `unwind-sequential` strategy provides a robust, dependency-free alternative.
