# Algorithm Summary: `clangd_call_graph_builder.py`

## 1. Role in the Pipeline

This script is responsible for Pass 3 of the ingestion pipeline: identifying all function call relationships (`:CALLS`) and ingesting them into Neo4j. It functions as both a library module for the main builder and a standalone tool for debugging or partial ingestion.

## 2. Standalone Usage

The script can be run directly to perform call graph extraction. This is useful for debugging the call graph logic or for regenerating only the `:CALLS` relationships in the database.

```bash
# Example: Extract call graph and ingest directly into Neo4j
python3 clangd_call_graph_builder.py /path/to/index.yaml /path/to/project/ --ingest

# Example: Extract call graph and save the Cypher queries to a .cql file
python3 clangd_call_graph_builder.py /path/to/index.yaml /path/to/project/
```

**All Options:**

*   `input_file`: Path to the clangd index YAML file (or a `.pkl` cache file).
*   `span_path`: Path to a project directory to scan for function spans (required for older clangd index formats).
*   `--ingest`: If set, ingest the call graph directly into Neo4j. Otherwise, a `generated_call_graph_cypher_queries.cql` file will be created.
*   `--stats`: Show detailed statistics about the extracted call graph.
*   `--num-parse-workers`: Number of parallel workers for parsing the YAML index.
*   `--ingest-batch-size`: Batch size for ingesting call relations.

---
*The following sections describe the library's internal logic.*

## 3. Core Algorithm: Two Extraction Strategies

The fundamental challenge in building a call graph is that the `clangd` index provides call sites (references to functions) but doesn't always explicitly link a call site to the function that contains it (the caller). This module solves this by automatically selecting one of two strategies based on the features of the parsed `clangd` index.

The logic is encapsulated in two classes that inherit from a common `BaseClangdCallGraphExtractor`.

### Strategy 1: High-Speed Path (`ClangdCallGraphExtractorWithContainer`)

This strategy is used for modern `clangd` index formats (version 21.x and later) that provide a `Container` field for references.

*   **Prerequisite**: The `SymbolParser` detects that `has_container_field` is `True`.
*   **Algorithm**:
    1.  The extractor iterates through every `Symbol` and its list of `references`.
    2.  For each reference, it checks if two conditions are met:
        *   The `reference.container_id` exists and is not a null placeholder (`'0000000000000000'`).
        *   The `reference.kind` indicates a function call (e.g., `20` or `28`).
    3.  If both are true, the `container_id` is the ID of the **caller function**, and the symbol being iterated is the **callee function**.
    4.  A `CallRelation` is recorded immediately.
*   **Subtlety**: This method is extremely fast and efficient because it is a pure in-memory operation on the already-parsed data. It requires no file I/O, no source code parsing with `tree-sitter`, and no complex lookups.

### Strategy 2: Legacy Fallback Path (`ClangdCallGraphExtractorWithoutContainer`)

This strategy is used for older `clangd` index formats that lack the `Container` field. It relies on parsing the source code to spatially determine the caller for each call site.

*   **Prerequisite**: The `SymbolParser` detects that `has_container_field` is `False`.
*   **Algorithm**:
    1.  **Span Loading**: The `SourceSpanProvider` is invoked first. It parses the entire project with `tree-sitter` to find the precise body location of every function and enriches the in-memory `Symbol` objects with this `body_location` data.
    2.  **Build Spatial Index**: The extractor builds a crucial in-memory data structure: a dictionary named `file_to_function_bodies_index`.
        *   **Keys**: File URIs (`'file:///path/to/file.c'`).
        *   **Values**: A list of all function bodies found in that file, sorted by their starting line number.
    3.  **Call Site Lookup**: The extractor iterates through every symbol and its references, looking for potential call sites (references with `Kind: 4` or `12`).
    4.  For each call site, it performs a fast lookup in the spatial index using the call site's file URI. This gives it the sorted list of all functions in that file.
    5.  It then performs a quick linear scan of this list, using the `_is_location_within_function_body` helper to check which function's body coordinates contain the call site's coordinates.
    6.  Once the containing function (the **caller**) is found, a `CallRelation` is recorded.
*   **Subtlety**: This fallback is much more I/O-intensive due to the `tree-sitter` parsing, but the in-memory spatial index makes the subsequent lookup phase very fast, avoiding a brute-force search for every call site.

## 4. Output

Regardless of the strategy used, the final output is a single list of all `CallRelation` objects found. The `ingest_call_relations` method then batches these relations and uses a parameterized `UNWIND` Cypher query to merge all `:CALLS` relationships into the graph in an efficient, bulk operation.
