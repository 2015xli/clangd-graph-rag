# C++ Refactor Plan - Step 0: Add Body Location to Data Structures

## 1. Goal

Based on the discovery that the `clangd` index intentionally omits type information for fields to reduce file size, we will implement a workaround. 

The goal of this preliminary step is to find and store the full body location of `struct`, `union`, and `enum` definitions on their corresponding `:DATA_STRUCTURE` nodes in the graph. This will allow any downstream tool or AI agent to fetch the complete source code of a type's definition on demand, enabling them to parse out field types or other details as needed.

This step leverages and expands our existing source code parsing infrastructure.

## 2. Affected Files

1.  **`compilation_parser.py`**: To extract body spans for data structures from the source code.
2.  **`function_span_provider.py`**: To map the extracted spans to the correct in-memory `Symbol` objects.
3.  **`clangd_symbol_nodes_builder.py`**: To write the `body_location` property to the `:DATA_STRUCTURE` nodes in Neo4j.

## 3. Implementation Plan

### 3.1. `compilation_parser.py`

*   **Component**: `_ClangWorkerImpl`
*   **Task**: Modify the AST walk to recognize and process data structure definitions, not just functions.

1.  **Update `_walk_ast`**: The `if` condition was expanded with an `elif` to handle `STRUCT_DECL`, `UNION_DECL`, and `ENUM_DECL`.
2.  **Add `_process_structure_node` method**: A new method was added to handle these new kinds. It uses `node.extent` to get the full start and end coordinates of the definition body.
3.  **Update `_process_function_node`**: This existing method was also updated to use the same robust `get_symbol_name_location` helper, ensuring consistent name location logic for all symbol types.
4.  **Correction**: The key used to store the list of extracted spans was changed from `"Functions"` to the more generic `"Spans"`. This ensures that all symbol types are correctly processed by downstream modules.

### 3.2. `function_span_provider.py`

*   **Component**: `FunctionSpanProvider`
*   **Task**: Generalize the enrichment process to apply to all symbols, not just functions.

1.  **Update `enrich_symbols_with_span`**: 
    *   The main loop was changed to iterate over `self.symbol_parser.symbols.values()` instead of just `functions.values()`.
    *   **Correction**: The logic was updated to look for the generic `"Spans"` key from the parser instead of the old `"Functions"` key, ensuring data structure spans are found and processed.

### 3.3. `clangd_symbol_nodes_builder.py`

*   **Component**: `SymbolProcessor`
*   **Task**: Add the `body_location` data to the properties of `:DATA_STRUCTURE` nodes before they are ingested.

1.  **Update `process_symbol`**: 
    *   An `if` block was added to check if the symbol `kind` is one of `Struct`, `Union`, `Enum`, or `Class`.
    *   If it is, and if the in-memory `Symbol` object has a `body_location` attribute (attached by the `FunctionSpanProvider`), this location data is formatted and added to the `symbol_data` dictionary.

## 4. Verification

1.  After the changes are implemented, run the full `clangd_graph_rag_builder.py` pipeline on a C project.
2.  Connect to the Neo4j database.
3.  Execute the following Cypher query: `MATCH (d:DATA_STRUCTURE) WHERE d.body_location IS NOT NULL RETURN d.name, d.body_location, d.kind LIMIT 20;`
4.  Verify that the query returns `:DATA_STRUCTURE` nodes for structs, unions, and enums, and that the `body_location` property contains a valid list of four numbers (start line, start col, end line, end col).
