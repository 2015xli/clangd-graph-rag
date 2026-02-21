# Macro Expansion and Causality

This phase focuses on linking symbols (Functions, Classes, etc.) to the macros that generated them, and capturing the original source invocation text.

### 1. Detection Strategy (Clang Parser)

Macro causality is detected during AST traversal by checking if a symbol's definition range is enclosed by a macro expansion.

#### 1.1 Containment Rule
We use a robust `extent_contains` helper that checks both line and column coordinates. A symbol is macro-generated if its entire `extent` is within the `extent` of a `MACRO_INSTANTIATION`.

#### 1.2 Selection Rule (Top-Level)
If multiple macro expansions enclose a symbol, we select the **outermost** expansion.
*   **Rationale**: The agent typically sees the macro written in the source code (the top-level call), not the internal implementation macros it might expand into. This preserves the agent's ground-truth context.

#### 1.3 Novelty Filtering
We only apply causality to **symbol definitions** (`node.is_definition()`).
*   **Rationale**: This prevents every reference inside a macro from being linked to it. We only care about the causality of symbol **identity** (creation).

### 2. Implementation (`_ClangWorkerImpl`)

*   **`instantiations`**: The worker collects all `MACRO_INSTANTIATION` cursors encountered in the TU.
*   **`_get_macro_causality`**: 
    1.  Finds the outermost instantiation enclosing the node.
    2.  Resolves the macro definition synthetic ID (`expanded_from_id`).
    3.  Extracts the raw invocation text from the source file (`original_name`).

### 3. Data Flow

1.  **`compilation_parser.py`**: Stores `original_name` and `expanded_from_id` in `SourceSpan` and `TypeAliasSpan`.
2.  **`source_span_provider.py`**: Transfers these fields to the corresponding `Symbol` objects during enrichment or synthesis.
3.  **`clangd_symbol_nodes_builder.py`**:
    *   Writes `original_name` as a node property.
    *   Ingests the `[:EXPANDED_FROM]` relationship from the symbol to the macro node.

### 4. Rationales

*   **Lexical Evidence**: The `original_name` (e.g., `DECLARE_MODULE(FS)`) provides immediate context to the agent about what arguments were used to generate the symbol.
*   **Logical Link**: The `[:EXPANDED_FROM]` edge provides the "logic" by pointing to the macro's `#define`.
*   **Agent Utility**: This combination directly solves the "magic symbol" problem encountered in DSL-heavy or boilerplate-rich C/C++ codebases.
