# C++ Refactor Plan - Step 5: Logical Constructs & Lexical Nesting

## 1. Goal

This step focuses on accurately modeling the logical and lexical structure of a C++ codebase. The goals are to implement:
1.  `NAMESPACE` nodes and their containment hierarchy using a **`:SCOPE_CONTAINS`** relationship.
2.  A robust, span-based mechanism to model the lexical nesting of structures (classes, structs, functions), including for **anonymous structures**, using a **`:HAS_NESTED`** relationship.
3.  A careful ingestion of `VARIABLE` nodes, correctly distinguishing between global/namespace variables and static class fields.

## 2. Affected Files

1.  **`compilation_parser.py`**: Refactored to generate a hierarchical "Span Tree" for each file, which is the foundation for the new lexical nesting logic.
2.  **`clangd_index_yaml_parser.py`**: The `Symbol` dataclass was updated to include an optional `parent_id`.
3.  **`source_span_provider.py`**: Completely refactored to implement a two-pass algorithm that uses the Span Tree to enrich all symbols with a `parent_id` and to synthesize new symbols for anonymous structures.
4.  **`clangd_symbol_nodes_builder.py`**: Updated to use the new `parent_id` and `namespace_id` attributes to create `:HAS_NESTED` and `:SCOPE_CONTAINS` relationships.
5.  **`neo4j_manager.py`**: To add new constraints for `:NAMESPACE` and `:VARIABLE` nodes.

## 3. Implementation Plan (Final)

The implementation was broken into two major features: Namespace Containment and Lexical Nesting.

### 3.1. Feature: Namespace Containment (`:SCOPE_CONTAINS`)

This feature correctly models how symbols are contained within namespaces.

*   **`clangd_symbol_nodes_builder.py`**:
    *   A helper method, `_build_scope_maps`, now runs first, creating a `qualified_namespace_to_id` lookup table from all `Symbol` objects with `kind: 'Namespace'`.
    *   The `process_symbol` method was enhanced. For every symbol, it checks if the symbol's `scope` string matches a key in the `qualified_namespace_to_id` map. If so, it adds a `namespace_id` property to the symbol's processed data.
    *   A consolidated ingestion method, `_ingest_scope_contains_relationships`, collects all `(namespace_id, symbol_id)` pairs and batch-creates the `(parent:NAMESPACE)-[:SCOPE_CONTAINS]->(child)` relationships. This handles both namespace-to-namespace and namespace-to-symbol containment.

### 3.2. Feature: Lexical Nesting (`:HAS_NESTED`)

This feature correctly models the nesting of structures like `class A { class B; };`, and is powerful enough to handle anonymous structures, which are not present in the `clangd` index.

#### 3.2.1. The Problem: Why the `scope` String Failed

The initial idea of using a symbol's `scope` string to find its lexical parent was abandoned. The `scope` string is designed for IDE display and is unreliable for graph construction due to:
*   **Anonymous Structures:** Scopes like `(anonymous struct)::` have no corresponding named symbol, making lookups impossible.
*   **Template Specializations:** Scope strings can contain fully specialized templates (e.g., `MyClass<int>::`), which do not directly match the base symbol name (`MyClass`).
*   **Function Signatures:** Scopes can include function signatures (e.g., `my_func(int, float)::`), which also do not match the simple function name.

#### 3.2.2. The Solution: A Span-Based Approach

The final implementation uses a purely spatial, span-based approach, with the `SpanTree` as the source of truth for all lexical nesting.

1.  **`compilation_parser.py` - The Span Tree:**
    *   This file was refactored to produce a hierarchical **`SpanTree`** for each parsed file.
    *   The `_ClangWorkerImpl` was modified to perform a recursive AST walk that builds `SpanNode` objects. Each `SpanNode` contains its kind, name, name/body spans, and a list of its `children` `SpanNode`s, perfectly mirroring the code's nested structure.
    *   The final output of the parser is a dictionary mapping each file URI to a "forest" (a list of top-level `SpanNode` trees).

2.  **`source_span_provider.py` - Two-Pass Enrichment:**
    *   This provider was completely redesigned to implement a powerful two-pass algorithm.
    *   **Pre-computation:** It first traverses the `SpanTree` data to build two lookup maps:
        1.  A map from a span's location to a generated `synthetic_id`.
        2.  A map from a child span's location to its parent's `synthetic_id`.
    *   **Pass 1 (Enrich & Synthesize):** It iterates through the real symbols from the `clangd` index. For each symbol, it finds its corresponding span in the lookup table, enriches the symbol with its `body_location`, and creates a mapping from the `synthetic_id` to the real symbol's `id`. Any spans left in the lookup table are identified as **anonymous structures**, and new "synthetic" `Symbol` objects are created for them and added to the main symbol list.
    *   **Pass 2 (Link Parents):** With a complete list of all symbols (real and synthetic), it iterates through them again. Using the parent lookup map, it finds the resolved ID of the lexical parent for each symbol and attaches it as a `parent_id` attribute to the symbol object.

3.  **`clangd_symbol_nodes_builder.py` - Ingestion:**
    *   The main `ingest_symbols_and_relationships` method was updated to be the final assembly point.
    *   It now contains a loop that prepares data for all relationship types in a single pass. For every symbol with a `parent_id`, it adds a `(parent_id, child_id)` pair to a `has_nested_relations` list.
    *   Crucially, this loop **pre-filters** the pairs based on the parent and child node labels to ensure they conform to the strict rules for the `:HAS_NESTED` relationship.
    *   A new method, `_ingest_nesting_relationships`, was added to efficiently batch-ingest these pre-filtered pairs, creating the final `(parent)-[:HAS_NESTED]->(child)` relationships in the graph.

### 3.3. Other Implementation Details

*   **`neo4j_manager.py`**: Constraints for `:NAMESPACE` (on `id` and `qualified_name`) and `:VARIABLE` (on `id`) were confirmed to be present.
*   **Variable and Static Field Logic**: The logic in `process_symbol` was corrected to reliably distinguish between global/namespace variables (`:VARIABLE`) and static class members (`:FIELD`). It now does this by checking if a `Variable` symbol's `parent_id` points to a `Class` or `Struct`, in which case it is re-labeled as a static `FIELD`.

## 4. Verification

1.  Run the builder on a C++ project with nested classes, anonymous structs, and namespaces.
2.  Verify in Neo4j:
    *   `MATCH (n:NAMESPACE) RETURN n LIMIT 10;`
    *   `MATCH (n:CLASS_STRUCTURE) WHERE n.name CONTAINS "anonymous" RETURN n LIMIT 10;` (Should show the new synthetic nodes).
    *   `MATCH (v:VARIABLE) RETURN v LIMIT 10;`
    *   `MATCH (f:FIELD {is_static: true}) RETURN f LIMIT 10;`
3.  Verify relationships:
    *   `MATCH (p:NAMESPACE)-[:SCOPE_CONTAINS]->(c) RETURN p.name, c.name, labels(c) LIMIT 20;`
    *   `MATCH (p)-[:HAS_NESTED]->(c) RETURN p.name, labels(p), c.name, labels(c) LIMIT 20;`
    *   `MATCH (f:FILE)-[:DECLARES]->(n:NAMESPACE) RETURN f.name, n.name LIMIT 10;`
