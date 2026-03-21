# Algorithm Summary: `compilation_parser.py`

## 1. Role in the Pipeline

This module provides the low-level parsing strategies for C/C++ source code. Its responsibility is to extract ground-truth implementation data—symbol spans, macro definitions, type aliases, and include relationships—normalizing diverse AST data into a unified format.

## 2. Core Design: Semantic and Syntactic Identities

The module supports two identity strategies to ensure both high performance and modular flexibility.

### 2.1. Semantic Identity (Clang Strategy)
For `ClangParser`, the system uses **USR-derived IDs** as the primary identity.
*   **The Rationale**: USRs (Unified Symbol Resolutions) are globally unique. By hashing them with the same algorithm as Clangd (`hash_usr_to_id`), parser IDs mathematically match Clangd index IDs (16-char hex).
*   **Fallback**: If a node lacks a USR (rare), it falls back to a location-based hash (`make_symbol_key` -> `make_synthetic_id`).

### 2.2. Syntactic Identity (Tree-sitter Strategy)
For `TreesitterParser`, the system uses **Lexical Identity**.
*   **The Rationale**: Syntactic parsers lack semantic knowledge and USRs. They rely on source coordinates (`file:line:col`) to generate identities.

## 3. Concrete Strategy: `ClangParser`

### 3.1. Technical Nuances in C++ Mapping
The parser incorporates specialized logic to overcome several limitations in the `libclang` AST representation:

*   **Context-Aware Method Identification**: In the AST, `FUNCTION_TEMPLATE` is used for both top-level and member functions. The parser checks the `semantic_parent` of such nodes. If the parent is a composite type (Class, Struct, etc.), it re-classifies the symbol as a `StaticMethod` or `InstanceMethod` (via `is_static_method()`) to ensure the identity matches the Clangd index.
*   **Template Keyword Resolution**: `libclang` often identifies all templated types as `CLASS_TEMPLATE`, losing the distinction between `struct`, `class`, and `union`. The parser solves this by performing a targeted token scan (using `islice(node.get_tokens(), 100)` for efficiency) immediately after the template parameter list (`>`) to find the actual defining keyword and assign the correct `kind`.
*   **Anonymity Management**: Anonymous nodes are detected via spelling patterns (e.g., `(unnamed enum at...)`). For these, the **raw USR string** is used as the node's name for debug visibility, and the USR-hash is used as its identity. This allows children symbols to find their anonymous parents mathematically.

### 3.2. Performance and Memory Optimization
*   **"Spawn" Execution**: Workers are initialized using `multiprocessing.get_context("spawn")`. This creates fresh Python interpreters, avoiding memory inheritance from the large main process and preventing fragmentation.
*   **Deterministic TU Hashing**: `_get_tu_hash` generates a unique key for the preprocessor environment. It buckets flags into categories (Language Standards, Macros, Features, Includes, Other) and preserves relative order within buckets. This ensures that different flag orderings that produce the same AST state result in the same cache key.
*   **Header Caching**: 
    *   `_global_header_cache`: Shared across workers, stores headers already processed for a specific `tu_hash`.
    *   `_local_header_cache`: Collects headers processed in the current TU.
    *   If a header was already parsed in an identical preprocessor context, the parser skips its AST traversal, significantly reducing redundant work.

## 4. Output Data Structure: `SourceSpan`

The output is a dictionary: `Dict[file_uri, Dict[id, SourceSpan]]`. 
*   Keying by `id` (16-char hash) drastically reduces memory compared to coordinate strings.
*   Keying by `file_uri` ensures the RAG system knows exactly which file to open to read the implementation.

## 5. Concrete Strategy: `TreesitterParser`

Retained as an interface verifier. It only supports location-based identities and extracts basic function spans, ensuring the pipeline's modularity logic remains robust.
