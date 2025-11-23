# Algorithm Summary: `compilation_parser.py`

## 1. Role in the Pipeline

This module provides the low-level, "raw" parsing strategies for C/C++ source code. It was created as part of a major refactoring to separate the concerns of parsing from the concerns of caching and orchestration. 

Its sole responsibility is to parse a given list of source files and extract two key pieces of information:
1.  The precise locations (spans) of symbol definitions (e.g., functions, classes, structs, templates).
2.  The set of all `#include` relationships.

This module acts as the "worker" layer, providing concrete parsing implementations that are managed by the `CompilationManager`.

## 2. Core Design: The Strategy Pattern

The module is designed using the Strategy pattern to allow for flexible switching between different parsing engines. 

*   **`CompilationParser` (Abstract Base Class)**: Defines the common interface that all concrete strategies must implement. This includes methods like `parse()`, `get_source_spans()`, and `get_include_relations()`.

This design allows the `CompilationManager` to treat any parser polymorphically, simply delegating the task of parsing to whichever concrete strategy has been chosen.

## 3. Concrete Strategy: `ClangParser`

This is the primary and recommended strategy, valued for its accuracy.

*   **Technology**: It uses `clang.cindex`, the official Python bindings for `libclang`.
*   **Semantic Accuracy**: Its key advantage is that it is **semantically aware**. By using a `compile_commands.json` file, it parses source code with the exact same context as the compiler. This allows it to correctly interpret complex macros and accurately identify a wide range of C++ constructs, including functions, methods, classes, structs, and their template specializations.
*   **Robust Location Finding**: The parser is designed to be resilient to complex C++ macros. It uses `libclang`'s "expansion location" feature to find the true physical source location of a symbol, even if the compiler initially reports its location as being inside a macro definition. This is critical for correctly matching parser output with the `clangd` index.
*   **Dual Extraction**: For efficiency, the parser traverses the Abstract Syntax Tree (AST) of each source file once, extracting both symbol definition spans and include relationships in a single pass.
*   **Path Handling**: A critical implementation detail is that it temporarily changes the working directory (`os.chdir`) to the compilation directory specified in `compile_commands.json` for each file it parses. This is essential for `libclang` to correctly resolve any relative include paths. This operation is safely wrapped in a `try...finally` block to guarantee the original working directory is always restored.
*   **Parallel Processing**: The `ClangParser` leverages `concurrent.futures.ProcessPoolExecutor` to parse multiple Translation Units (TUs) in parallel. Each worker process runs an instance of `_ClangWorkerImpl`, which is responsible for parsing a single TU.
*   **Header Caching (`_global_header_cache` and `_tu_hash`)**: To prevent redundant parsing of header files across different TUs, `_ClangWorkerImpl` implements a sophisticated caching mechanism:
    *   A `_tu_hash` is computed for each TU based on its compilation arguments (preprocessor macros, include paths, language dialect). This hash uniquely identifies the compilation environment.
    *   A `_global_header_cache` (shared across worker processes) stores header file paths that have already been fully processed for a given `_tu_hash`.
    *   When a worker encounters a header file during AST traversal, it checks if that header has already been processed under the *exact same `_tu_hash`*. If so, parsing of that header's AST is safely skipped, as the `SourceSpan` data would be identical. This significantly reduces redundant work.
    *   A `_local_header_cache` temporarily collects headers processed by the current TU before merging them into the `_global_header_cache` at the end of the TU's processing.
*   **Node Deduplication**: Within a single TU's AST traversal, the `_process_generic_node` method uses a `node_key` (derived from symbol name, file URI, line, and column) to ensure that only one `SourceSpan` object is created and stored for each unique AST node. This prevents duplicate entries if `libclang` reports the same node multiple times.

### 3.1. Output Data Structure: `SourceSpan` and `source_spans`

The primary output of the `ClangParser` is a collection of `SourceSpan` objects, organized in the `self.source_spans` dictionary:

*   **`self.source_spans`**: `Dict[file_uri, Dict[node_key, SourceSpan]]`
    *   The outer dictionary maps the absolute file URI (e.g., `file:///path/to/file.cpp`) to an inner dictionary.
    *   The inner dictionary maps a unique `node_key` (e.g., `function_name::file_uri:line:col`) to a `SourceSpan` object.
*   **`SourceSpan` (dataclass)**: Represents a lexically defined entity in the source code. It contains:
    *   `name`: The name of the entity (e.g., function name, class name).
    *   `kind`: The type of entity (e.g., "Function", "Class", "Struct", "Namespace").
    *   `lang`: The programming language ("C" or "Cpp").
    *   `name_location`: A `RelativeLocation` object for the entity's name.
    *   `body_location`: A `RelativeLocation` object for the entire body of the entity (start/end line/column). This is crucial for extracting source code snippets.
    *   `id`: A deterministic `synthetic_id` generated from the `node_key`.
    *   `parent_id`: The `synthetic_id` of the immediate lexical parent of this entity, derived from the AST's semantic parent. This is fundamental for establishing parent-child relationships in the graph.

### 3.2. Output Data Structure: `include_relations`

*   **`self.include_relations`**: `Set[Tuple[str, str]]`
    *   A set of tuples, where each tuple `(including_file_abs_path, included_file_abs_path)` represents a direct `#include` directive found during parsing.

## 4. Concrete Strategy: `TreesitterParser`

This strategy is **currently not used** in the pipeline due to its limitations in accurately integrating with clangd indexed symbols.

*   **Technology**: It uses the `tree-sitter` library for purely syntactic parsing.
*   **Pros and Cons**: It is significantly faster than the `ClangParser` but is not semantically aware. It can be easily fooled by functions or signatures defined with complex preprocessor macros.
*   **Key Limitation**: This parser is only capable of extracting function spans. Its `get_include_relations()` method returns an empty data structure. Therefore, it **cannot be used** for the robust, include-based dependency analysis required by the incremental updater.
