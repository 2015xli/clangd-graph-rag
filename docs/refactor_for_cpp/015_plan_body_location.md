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

1.  **Update `_walk_ast`**: The `if` condition will be expanded to check for more cursor kinds.
    *   **Current**: `if node.kind == clang.cindex.CursorKind.FUNCTION_DECL and node.is_definition():`
    *   **New**: Add an `elif` to handle `STRUCT_DECL`, `UNION_DECL`, and `ENUM_DECL`.
2.  **Add `_process_structure_node` method**: A new method will be added to `_ClangWorkerImpl` to handle these new kinds. It will:
    *   Use the robust `get_symbol_name_location` helper to find the accurate start of the symbol's name.
    *   Use `node.extent` to get the full start and end coordinates of the entire definition body.
    *   Store this information in `self.span_results` with the appropriate `Kind` (e.g., 'Struct', 'Union').

### 3.2. `function_span_provider.py`

*   **Component**: `FunctionSpanProvider`
*   **Task**: Generalize the enrichment process to apply to all symbols, not just functions.

1.  **Update `enrich_symbols_with_span`**: 
    *   The main loop currently iterates over `self.symbol_parser.functions.values()`.
    *   This will be changed to iterate over `self.symbol_parser.symbols.values()`.
    *   The existing matching logic, which uses a composite key of `(name, file_uri, line, column)`, is generic and will work perfectly for matching `struct`, `union`, and `enum` symbols in addition to functions.

### 3.3. `clangd_symbol_nodes_builder.py`

*   **Component**: `SymbolProcessor`
*   **Task**: Add the `body_location` data to the properties of `:DATA_STRUCTURE` nodes before they are ingested.

1.  **Update `process_symbol`**: 
    *   A new `if` block will be added. It will check if the symbol `kind` is one of `Struct`, `Union`, `Enum`, or `Class`.
    *   If it is, and if the in-memory `Symbol` object has a `body_location` attribute (attached by the `FunctionSpanProvider`), this location data will be formatted and added to the `symbol_data` dictionary.
    *   This is the exact same logic that is already in place for `Function` symbols.

## 4. Verification

1.  After the changes are implemented, run the full `clangd_graph_rag_builder.py` pipeline on a C project.
2.  Connect to the Neo4j database.
3.  Execute the following Cypher query: `MATCH (d:DATA_STRUCTURE) WHERE d.body_location IS NOT NULL RETURN d.name, d.body_location, d.kind LIMIT 20;`
4.  Verify that the query returns `:DATA_STRUCTURE` nodes for structs, unions, and enums, and that the `body_location` property contains a valid list of four numbers (start line, start col, end line, end col).
