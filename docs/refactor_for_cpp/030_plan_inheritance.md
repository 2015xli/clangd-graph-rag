# C++ Refactor Plan - Step 3: Implement Class Relationships (Inheritance and Overrides)

## 1. Goal

With `CLASS_STRUCTURE` nodes now in the graph, the next logical step is to model the relationships between them. The goal of this step is to parse all relationship information from the `clangd` index and create the corresponding graph edges. This includes:
1.  `[:INHERITS]` relationships between classes.
2.  `[:OVERRIDDEN_BY]` relationships between virtual methods.

## 2. Affected Files

1.  **`clangd_index_yaml_parser.py`**: To parse `!Relations` documents from the `clangd` index.
2.  **`clangd_symbol_nodes_builder.py`**: To add new passes that create the `[:INHERITS]` and `[:OVERRIDDEN_BY]` relationships.

## 3. Implementation Plan

### 3.1. `clangd_index_yaml_parser.py`

*   The `clangd` index contains `!Relations` documents that describe both inheritance and method overrides. The `Predicate` field distinguishes them:
    ```yaml
    --- !Relations
    Subject: { ID: <ID of Base> }
    Predicate: <0 for BaseOf, 1 for OverriddenBy>
    Object: { ID: <ID of Derived> }
    ```
*   We need to modify the parser to handle these documents and both predicate types.
*   **In `SymbolParser.__init__`**:
    *   Add new list attributes to hold the parsed data:
        ```python
        self.inheritance_relations: List[Tuple[str, str]] = []
        self.override_relations: List[Tuple[str, str]] = []
        self.unlinked_relations: List[Dict] = [] # For temporary storage
        ```
*   **In `SymbolParser._load_from_string`**:
    *   Add a condition to detect `!Relations` documents and append them to the `unlinked_relations` list.
        ```python
        elif 'Subject' in doc and 'Predicate' in doc and 'Object' in doc:
            self.unlinked_relations.append(doc)
        ```
*   **In `SymbolParser.build_cross_references`**:
    *   Add a new loop to process the `self.unlinked_relations` list.
    *   Inside the loop, check the `Predicate` value:
        *   If `0` (`BaseOf`), add the `(Subject, Object)` ID tuple to `self.inheritance_relations`.
        *   If `1` (`OverriddenBy`), add the `(Subject, Object)` ID tuple to `self.override_relations`.
*   **Caching**:
    *   The `_dump_cache_file` and `_load_cache_file` methods were updated to save and load the `inheritance_relations` and `override_relations` lists, preventing re-parsing on subsequent runs.

### 3.2. `clangd_symbol_nodes_builder.py`

*   **In `SymbolProcessor.ingest_symbols_and_relationships`**:
    *   After all symbol nodes have been created, add new method calls:
        ```python
        self._ingest_inheritance_relationships(symbol_parser.inheritance_relations, neo4j_mgr)
        self._ingest_override_relationships(symbol_parser.override_relations, neo4j_mgr)
        ```

*   **Create `SymbolProcessor._ingest_inheritance_relationships`**:
    *   This method takes the list of `(base_id, derived_id)` tuples and executes a batched `UNWIND` query to create the `[:INHERITS]` relationships.
    *   The Cypher query implements the `(child)-[:INHERITS]->(parent)` direction:
        ```cypher
        UNWIND $relations AS rel
        MATCH (child:CLASS_STRUCTURE {id: rel.object_id})  // Object is the Derived/Child class
        MATCH (parent:CLASS_STRUCTURE {id: rel.subject_id}) // Subject is the Base/Parent class
        MERGE (child)-[:INHERITS]->(parent)
        ```

*   **Create `SymbolProcessor._ingest_override_relationships`**:
    *   This new method takes the list of `(base_method_id, derived_method_id)` tuples.
    *   It executes a batched `UNWIND` query to create the `[:OVERRIDDEN_BY]` relationships.
    *   The Cypher query implements the `(parent_method)-[:OVERRIDDEN_BY]->(child_method)` direction:
        ```cypher
        UNWIND $relations AS rel
        MATCH (base_method:METHOD {id: rel.subject_id}) // Subject is the Base method
        MATCH (derived_method:METHOD {id: rel.object_id}) // Object is the Derived method
        MERGE (base_method)-[:OVERRIDDEN_BY]->(derived_method)
        ```

## 4. Verification

1.  Run the builder on a C++ project that uses inheritance and virtual functions.
2.  Verify in Neo4j that `[:INHERITS]` relationships exist between the correct `:CLASS_STRUCTURE` nodes.
    *   `MATCH (d:CLASS_STRUCTURE)-[:INHERITS]->(b:CLASS_STRUCTURE) RETURN d.name, b.name LIMIT 10;`
3.  Verify in Neo4j that `[:OVERRIDDEN_BY]` relationships exist between the correct `:METHOD` nodes.
    *   `MATCH (base:METHOD)-[:OVERRIDDEN_BY]->(derived:METHOD) RETURN base.name, derived.name LIMIT 10;`
4.  Check if any properties need to be added to the `INHERITS` relationship, such as `access` (public, protected, private). The `clangd` index may not provide this directly, so it remains a future enhancement. For now, creating the relationships is the primary goal.
