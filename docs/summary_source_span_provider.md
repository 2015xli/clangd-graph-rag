# Algorithm Summary: `source_span_provider.py`

## 1. Role in the Pipeline

In the refactored architecture, this script provides the `SourceSpanProvider` class, which acts as a simple **adapter** and **enricher**. Its sole responsibility is to bridge the gap between two pre-populated data sources:

1.  The `SymbolParser`, which holds all the parsed symbols from the `clangd` index.
2.  The `CompilationManager`, which holds all the source code span information (for functions, structs, etc.) extracted by a parser (`ClangParser` or `TreesitterParser`).

Its purpose is to take the span data and attach it to the corresponding in-memory `Symbol` objects. This one-time enrichment is essential for two key downstream tasks:

1.  **Legacy Call Graph Building**: To know the exact boundaries of every function.
2.  **RAG Summary Generation**: To get the source code of a function or data structure.

## 2. Core Logic: The `enrich_symbols_with_span` Method

The provider's logic is contained entirely within the `enrich_symbols_with_span` method, which is called by the main graph builder.

### Step 1: Get Pre-Parsed Spans

*   The provider does not perform any parsing itself. It begins by calling `self.compilation_manager.get_source_spans()` to retrieve the list of all symbol spans that were already extracted from the source code in a prior pipeline step.

### Step 2: Build Lookup Table

*   To facilitate efficient matching, the provider processes the raw list of span data into a `spans_lookup` dictionary.
*   The key for this dictionary is a composite key designed to uniquely identify a symbol's definition: `(name, file_uri, name_start_line, name_start_column)`.

### Step 3: Match and Enrich Symbols

*   The provider iterates through **all** symbols in `self.symbol_parser.symbols.values()` (not just functions).
*   For each symbol, it constructs the same composite key format using the symbol's definition location from the `clangd` index.
*   If this key exists in the `spans_lookup` dictionary, a match is found.
*   **Enrichment**: The `body_location` from the matched span is attached as a new attribute directly onto the in-memory `Symbol` object.

### Step 4: Memory Management

*   After the enrichment process is complete, the provider sets its internal reference to the large `symbol_parser` object to `None` and the method finishes. This allows the Python garbage collector to free the memory used by the `SymbolParser` before the next, memory-intensive stages of the pipeline (like RAG generation) begin.

## 3. Design Rationale

The `SourceSpanProvider` is designed as a simple, short-lived adapter to decouple the main build orchestrator from the details of the enrichment process. By having this class handle the matching logic, the main builder's code is kept cleaner and more focused on the high-level pipeline steps. Its explicit memory cleanup step is also crucial for ensuring the stability of the pipeline when processing very large codebases.