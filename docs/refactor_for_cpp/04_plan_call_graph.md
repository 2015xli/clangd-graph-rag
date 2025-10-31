# C++ Refactor Plan - Step 4: Update Call Graph Builder

## 1. Goal

Now that `FUNCTION` and `METHOD` nodes are distinct in the graph, the call graph builder must be updated to handle this. The goal is to ensure that `[:CALLS]` relationships are correctly created for all four possible scenarios:
1.  Function -> Function
2.  Function -> Method
3.  Method -> Function
4.  Method -> Method

## 2. Affected Files

1.  **`clangd_call_graph_builder.py`**: This is the only file that needs significant changes. The core logic of both `WithContainer` and `WithoutContainer` extractors needs to be updated.

## 3. Implementation Plan

The key is to modify the Cypher queries to match against either a `:FUNCTION` or a `:METHOD` label.

### 3.1. `ClangdCallGraphExtractorWithContainer`

This extractor is simpler to update.

*   **In `extract_call_relationships`**:
    *   The logic that identifies a `caller_symbol` and `callee_symbol` remains the same.
    *   The change is in how we verify the symbols. The check `if caller_symbol and caller_symbol.is_function():` needs to be updated.
    *   The `Symbol` data class in `clangd_index_yaml_parser.py` should be updated with a new helper method:
        ```python
        def is_callable(self) -> bool:
            return self.kind in ('Function', 'InstanceMethod', 'StaticMethod', 'Constructor', 'Destructor', 'ConversionFunction')
        ```
    *   The check in the extractor becomes `if caller_symbol and caller_symbol.is_callable():`.

### 3.2. `ClangdCallGraphExtractorWithoutContainer`

This extractor requires more changes as it builds a spatial index.

*   **In `extract_call_relationships`**:
    *   The initial set of functions to build the spatial index, `functions_with_bodies`, must now include both functions and methods. The filter `if f.body_location` should be applied to all callable symbols.
    *   The `file_to_function_bodies_index` will now contain both `FUNCTION` and `METHOD` symbols. This is fine as they are both just `Symbol` objects at this stage.
    *   The logic that iterates through `callee_symbol.references` must also consider callees that are methods. The check `if not callee_symbol.is_function():` should be changed to `if not callee_symbol.is_callable():`.

### 3.3. `BaseClangdCallGraphExtractor` (The Common Part)

*   **In `get_call_relation_ingest_query`**: This is the most important change. The query must be generalized.
    *   The current query is:
        ```cypher
        UNWIND $relations as relation
        MATCH (caller:FUNCTION {id: relation.caller_id})
        MATCH (callee:FUNCTION {id: relation.callee_id})
        MERGE (caller)-[:CALLS]->(callee)
        ```
    *   The updated query should be:
        ```cypher
        UNWIND $relations as relation
        MATCH (caller) WHERE (caller:FUNCTION OR caller:METHOD) AND caller.id = relation.caller_id
        MATCH (callee) WHERE (callee:FUNCTION OR callee:METHOD) AND callee.id = relation.callee_id
        MERGE (caller)-[:CALLS]->(callee)
        ```
    *   This uses a `WHERE` clause to match nodes that have one of two labels, which is efficient if the `id` property is indexed on both labels.

## 4. Verification

1.  Run the builder on a C++ project.
2.  Execute queries in Neo4j to verify all four call patterns:
    *   `MATCH (a:FUNCTION)-[:CALLS]->(b:FUNCTION) RETURN a, b LIMIT 5;`
    *   `MATCH (a:FUNCTION)-[:CALLS]->(b:METHOD) RETURN a, b LIMIT 5;`
    *   `MATCH (a:METHOD)-[:CALLS]->(b:FUNCTION) RETURN a, b LIMIT 5;`
    *   `MATCH (a:METHOD)-[:CALLS]->(b:METHOD) RETURN a, b LIMIT 5;`
3.  Ensure the total number of `[:CALLS]` relationships seems reasonable and that no calls are being dropped.
4.  Run the builder on a C-only project to ensure the updated queries have not caused a regression.
