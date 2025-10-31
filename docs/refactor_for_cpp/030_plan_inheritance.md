# C++ Refactor Plan - Step 3: Implement Inheritance

## 1. Goal

With `CLASS_STRUCTURE` nodes now in the graph, the next logical step is to model the relationships between them. The goal of this step is to parse inheritance information from the `clangd` index and create `[:INHERITS]` relationships in the graph.

## 2. Affected Files

1.  **`clangd_index_yaml_parser.py`**: To parse `!Relations` documents from the `clangd` index.
2.  **`clangd_symbol_nodes_builder.py`**: To add a new pass that creates the `[:INHERITS]` relationships.

## 3. Implementation Plan

### 3.1. `clangd_index_yaml_parser.py`

*   The `clangd` index contains `!Relations` documents for inheritance. The format is:
    ```yaml
    --- !Relations
    Subject:
      ID:              <ID of Base Class>
    Predicate:       0  # 0 means 'BaseOf'
    Object:
      ID:              <ID of Derived Class>
    ```
*   We need to modify the parser to handle these documents.
*   **In `SymbolParser`**:
    *   Add a new list attribute: `self.relations = []`.
    *   In `_load_from_string` (and the parallel worker), add a condition to detect `!Relations` documents.
        ```python
        # In _load_from_string
        elif 'Subject' in doc and 'Predicate' in doc and 'Object' in doc:
            self.relations.append(doc)
        ```
*   **In `SymbolParser.build_cross_references`**:
    *   This method is currently for `!Refs`. We can rename it or add a new method `build_relations` to process the `self.relations` list.
    *   The processed relations should be stored in a structured way, perhaps in a new attribute `self.inheritance_relations`, which would be a list of tuples `(base_class_id, derived_class_id)`.

### 3.2. `clangd_symbol_nodes_builder.py`

*   **In `SymbolProcessor.ingest_symbols_and_relationships`**:
    *   After all `CLASS_STRUCTURE` nodes have been created, add a new method call: `self._ingest_inheritance_relationships(symbol_parser.inheritance_relations, neo4j_mgr)`.

*   **Create `SymbolProcessor._ingest_inheritance_relationships`**:
    *   This new method will be responsible for creating the `[:INHERITS]` relationships.
    *   It will take the list of `(base_id, derived_id)` tuples.
    *   It will execute a batched `UNWIND` query to create the relationships efficiently.
    *   The query will look like:
        ```cypher
        UNWIND $inheritance_data AS data
        MATCH (base:CLASS_STRUCTURE {id: data.base_id})
        MATCH (derived:CLASS_STRUCTURE {id: data.derived_id})
        MERGE (derived)-[:INHERITS]->(base)
        ```
        *(Note the direction: the Derived class inherits from the Base class).*

## 4. Verification

1.  Run the builder on a C++ project that uses inheritance.
2.  Verify in Neo4j that `[:INHERITS]` relationships exist between the correct `:CLASS_STRUCTURE` nodes.
3.  Execute the query `MATCH (d:CLASS_STRUCTURE)-[:INHERITS]->(b:CLASS_STRUCTURE) RETURN d, b LIMIT 10;` to confirm the relationships and their direction.
4.  Check if any properties need to be added to the `INHERITS` relationship, such as `access` (public, protected, private). The `clangd` index may not provide this directly, so it might be a future enhancement. For now, creating the relationship is the primary goal.
