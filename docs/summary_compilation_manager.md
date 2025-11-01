# Algorithm Summary: `compilation_manager.py`

## 1. Role in the Pipeline

This module provides the `CompilationManager` class, which acts as the high-level orchestrator for the entire source code parsing process. It was created by refactoring the old `function_span_extractor.py` to have a broader and clearer set of responsibilities.

Its purpose is to serve as the single, unified interface for any other part of the system that needs to access source code information like function spans or include relationships. It decouples the main application logic from the low-level details of parsing and caching.

## 2. Core Responsibilities

The `CompilationManager` has two primary responsibilities:

1.  **Strategy Selection**: Based on user input (e.g., `--source-parser clang`), it instantiates the appropriate low-level parser strategy (`ClangParser` or `TreesitterParser`) from the `compilation_parser.py` module.
2.  **Caching**: It manages a sophisticated caching layer to avoid re-running the expensive parsing process unnecessarily.

## 3. Caching Mechanism (`ParserCache`)

To ensure fast subsequent runs, the manager uses a robust caching strategy. This logic is encapsulated within the inner `ParserCache` class.

#### Cache Content
A key design decision, made to resolve a bug where `ctypes` objects could not be pickled, is that the cache **does not store the parser object itself**. Instead, it stores only the raw, serializable data that is extracted: the function spans and the include relations. When loading from a valid cache, a new parser object is instantiated and then populated with this pre-parsed data.

#### Cache Invalidation
The cache is considered valid based on a two-tiered strategy:

1.  **Git-Based (Primary)**: If the project is a Git repository with a clean working directory, the cache is only considered valid if the current `HEAD` commit hash matches the hash stored within the cache file. This is the most reliable method as it precisely tracks the code's version.
2.  **Timestamp-Based (Fallback)**: If the project is not a Git repository or the working tree is dirty, the system falls back to a more traditional check. It compares the modification time of the cache file against the modification times of all source files in the project. If any source file is newer than the cache, the cache is considered stale.

## 4. Public API and Workflows

The manager exposes a clean API to handle different use cases:

*   **`parse_folder()`**: This is used by the full graph builder. It orchestrates the entire caching logic. If a valid cache is found, it loads from it; otherwise, it triggers a full parse of the project folder and saves the results to the cache.
*   **`parse_files()`**: This is used by the incremental graph updater. It takes a specific list of files to parse and **does not** use the cache, as the goal is always to get the fresh, updated information for that small subset of files.
*   **`get_source_spans()` / `get_include_relations()`**: After a `parse_*` method has been called, these methods are used to retrieve the extracted data.
