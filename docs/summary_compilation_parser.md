# Algorithm Summary: `compilation_parser.py`

## 1. Role in the Pipeline

This module provides the low-level, "raw" parsing strategies for C/C++ source code. It was created as part of a major refactoring to separate the concerns of parsing from the concerns of caching and orchestration. 

Its sole responsibility is to parse a given list of source files and extract two key pieces of information:
1.  The precise locations (spans) of function bodies.
2.  The set of all `#include` relationships.

This module acts as the "worker" layer, providing concrete parsing implementations that are managed by the `CompilationManager`.

## 2. Core Design: The Strategy Pattern

The module is designed using the Strategy pattern to allow for flexible switching between different parsing engines. 

*   **`CompilationParser` (Abstract Base Class)**: Defines the common interface that all concrete strategies must implement. This includes methods like `parse()`, `get_source_spans()`, and `get_include_relations()`.

This design allows the `CompilationManager` to treat any parser polymorphically, simply delegating the task of parsing to whichever concrete strategy has been chosen.

## 3. Concrete Strategy: `ClangParser`

This is the primary and recommended strategy, valued for its accuracy.

*   **Technology**: It uses `clang.cindex`, the official Python bindings for `libclang`.
*   **Semantic Accuracy**: Its key advantage is that it is **semantically aware**. By using a `compile_commands.json` file, it parses source code with the exact same context as the compiler (including all flags, defines, and include paths). This allows it to correctly interpret complex macros and accurately identify function definitions.
*   **Dual Extraction**: For efficiency, the parser traverses the Abstract Syntax Tree (AST) of each source file once, extracting both function spans and include relationships in a single pass.
*   **Path Handling**: A critical implementation detail is that it temporarily changes the working directory (`os.chdir`) to the compilation directory specified in `compile_commands.json` for each file it parses. This is essential for `libclang` to correctly resolve any relative include paths. This operation is safely wrapped in a `try...finally` block to guarantee the original working directory is always restored.

## 4. Concrete Strategy: `TreesitterParser`

This is a legacy strategy, kept for situations where a `compile_commands.json` is not available.

*   **Technology**: It uses the `tree-sitter` library for purely syntactic parsing.
*   **Pros and Cons**: It is significantly faster than the `ClangParser` but is not semantically aware. It can be easily fooled by functions or signatures defined with complex preprocessor macros.
*   **Key Limitation**: This parser is only capable of extracting function spans. Its `get_include_relations()` method returns an empty data structure. Therefore, it **cannot be used** for the robust, include-based dependency analysis required by the incremental updater.
