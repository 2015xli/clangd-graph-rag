# SummaryManager: Design and Architecture

## 1. High-Level Overview & Purpose

The `SummaryManager` is the central, intelligent provider for all Retrieval-Augmented Generation (RAG) summaries within the `clangd-graph-rag` project. Its primary purpose is to abstract away the complexities of summary generation, caching, and staleness detection from the main graph traversal logic in `RagGenerator`.

By acting as a unified source of truth for summaries, it dramatically improves performance by avoiding redundant LLM calls and enhances the system's modularity and maintainability. The `RagGenerator` simply asks for a summary for a given entity, and the `SummaryManager` is responsible for returning a valid summary, either from its cache or by generating a new one. It also internally manages the LLM client, tokenizer, and prompt manager, abstracting these details from the orchestrator.

## 2. Core Responsibilities

The `SummaryManager`'s duties can be broken down into four main categories:

1.  **Summary Provisioning**: Exposing a clean, high-level API (e.g., `get_code_summary`, `get_function_contextual_summary`) for other parts of the system to request summaries.
2.  **Caching and Persistence**: Managing an in-memory cache of summaries and their metadata, and handling the loading from and saving to a persistent JSON file (`.cache/summary_backup.json`).
3.  **Staleness Detection and Generation**: Encapsulating the complex logic to determine if a cached or database summary is "stale" and needs to be regenerated. If regeneration is necessary, it orchestrates the entire process, from prompt creation to LLM calls and iterative context handling.
4.  **LLM Component Management**: Internally instantiates and manages the `LlmClient`, `RagGenerationPromptManager`, and `tiktoken` tokenizer, configuring them based on provided parameters (e.g., `llm_api`, `token_encoding`). It also exposes a property `is_local_llm` for external consumers.

## 3. Key Data Structures

The manager's intelligence is powered by two core data structures:

### 3.1. `self.cache_data`

This is the main in-memory cache for all summary-related data.

-   **Structure**: `Dict[str, Dict[str, Any]]`
    -   The outer dictionary is keyed by the node's `label` (e.g., "FUNCTION", "CLASS_STRUCTURE").
    -   The inner dictionary is keyed by the node's unique identifier (e.g., its `id` for symbols or `path` for files/folders).
    -   The value is a dictionary containing the cached data, such as `code_hash`, `codeSummary`, and the final `summary`.
-   **Persistence**: This dictionary is loaded from `.cache/summary_backup.json` at the start of a run and saved back to the same file at the end, ensuring that summaries are preserved between runs.

### 3.2. `self.cache_status`

This is a runtime-only data structure that tracks the state of each entity *during the current execution*. It is the heart of the dependency-based staleness detection.

-   **Structure**: `Dict[str, Dict[str, Any]]` (mirrors the structure of `cache_data`).
-   **Flags**: Each entry contains the following boolean flags, all initialized to `False`:
    -   `entry_is_visited`: Becomes `True` if an entity is processed in any way during the run. This is used at the end to prune the cache file of entries that are no longer present in the graph.
    -   `code_is_same`: Tracks the validity of the `codeSummary`. It is set to `True` if the summary is confirmed to be up-to-date (via hash check or cache hit) and `False` if it was regenerated.
    -   `summary_is_same`: Tracks the validity of the final, contextual `summary`. It is set to `True` if the summary is up-to-date and `False` if it was regenerated due to a stale dependency.

## 4. The Caching and Staleness Engine

The `SummaryManager` avoids expensive re-computation by implementing a sophisticated, dependency-aware staleness-checking mechanism.

### 4.1. Content-Based Staleness (`is_code_changed`)

This is the first level of caching, used for `codeSummary` generation for functions and methods.

-   **Trigger**: A function's source code.
-   **Mechanism**:
    1.  When `get_code_summary` is called, it calculates a new MD5 hash of the function's source code.
    2.  It compares this new hash with the `code_hash` stored in the Neo4j database for that node. If they match and a `codeSummary` exists, the summary is considered fresh.
    3.  If the DB hash doesn't match, it checks the in-memory `cache_data` for an entry with a matching new hash.
    4.  Only if both checks fail is a new `codeSummary` generated.
-   **Status Update**: The `code_is_same` flag in `cache_status` is set to `True` for a cache/DB hit and `False` for a regeneration.

### 4.2. Dependency-Based Staleness (`is_summary_changed`)

This is the second level of caching, used for all context-aware summaries (contextual function summaries, classes, namespaces, etc.). It creates a hierarchical dependency chain.

-   **Trigger**: The status of an entity's dependencies.
-   **Mechanism**:
    -   **For a contextual function summary**: It is considered stale if `is_code_changed` is `True` for the function itself, or for any of its callers or callees.
    -   **For a class/namespace/file summary**: It is considered stale if `is_summary_changed` is `True` for any of its children (e.g., methods for a class, contained nodes for a namespace).
-   **Code Flow**: When a `get_*_summary` method is called, it first iterates through all its dependencies and calls `is_summary_changed` (or `is_code_changed`) on them. If any dependency is stale, the current entity is also marked as stale, triggering a regeneration.

## 5. Summary Generation Flow (Public API)

The `RagGenerator` interacts with the `SummaryManager` through its public `get_*` methods.

### 5.1. `get_code_summary(entity, source_code)`

1.  Calculates the hash of the `source_code`.
2.  Checks if the hash matches the `db_code_hash` on the `entity`. If yes, marks status as `code_is_same: True` and returns `(None, None)`.
3.  If not, checks the in-memory `cache_data` for an entry with the new hash. If found, returns the cached summary.
4.  If no valid summary is found, it's a "miss". It marks `code_is_same: False`, calls `_summarize_function_text_iteratively` to generate a new summary, updates its cache, and returns the new summary.

### 5.2. `get_function_contextual_summary(entity, callers, callees)`

1.  Checks for staleness by calling `is_code_changed()` on the `entity` and all `callers` and `callees`.
2.  If not stale and a summary exists in the DB, it updates the cache and returns `None`.
3.  If not stale and no summary is in the DB, it checks the cache. If a summary is found, it returns it for the `RagGenerator` to write to the DB.
4.  If stale, it marks `summary_is_same: False`, gathers the required `codeSummary`s for all dependencies from its cache, and calls `generate_contextual_summary()` to produce a new summary, which is then returned.

### 5.3. `get_class_summary`, `get_namespace_summary`, etc.

These methods all follow a similar pattern to the contextual function summary:
1.  Check for staleness by calling `is_summary_changed()` on all child/dependent entities.
2.  If not stale, attempt to return a valid summary from the DB or cache.
3.  If stale, mark `summary_is_same: False`, gather the final `summary` of all children from the cache, and call the appropriate internal generation method (e.g., `generate_class_summary` or the generic `_generate_hierarchical_summary`).

## 6. Internal Generation and Iteration Logic

When a "cache miss" occurs, the manager uses its internal methods to generate a new summary.

### 6.1. `generate_*_summary()` Methods

These methods (`generate_contextual_summary`, `generate_class_summary`) are the internal orchestrators for generation. They are responsible for:
-   Logging that a new summary is being generated.
-   Checking if the total context size exceeds the LLM's limit.
-   If the context is small enough, they build the final prompt using `RagGenerationPromptManager` and make a single call to the `llm_client`.
-   If the context is too large, they delegate to an iterative summarization method.
-   Updating the `cache_data` with the newly generated summary.

### 6.2. `_generate_hierarchical_summary()`

This is a generic helper for generating summaries for namespaces, files, folders, and the project. It takes the context name and a list of child summaries, determines the correct prompt to use, and handles the single-shot vs. iterative generation logic.

### 6.3. Iterative Summarization

To handle very large contexts, the manager uses a prompt-chaining technique:
1.  **`_chunk_text_by_tokens()`**: This utility uses the `tiktoken` library to split a large body of text (like source code or a collection of child summaries) into smaller, overlapping chunks that fit within the LLM's context window.
2.  **`_summarize_relations_iteratively()`**: This is a generic prompt-chaining engine. It starts with a base summary and iteratively "folds in" the summary of each chunk of relations, calling the LLM at each step to produce a new, combined `running_summary`.
3.  **`_summarize_function_text_iteratively()`**: A specific implementation for long function source code.

## 7. Persistence and CLI Tool

-   **`backup_cache_file_from_neo4j()` / `restore_cache_file_to_neo4j()`**: These methods implement the CLI backup/restore for Neo4j graph summaries. They use the `_load_summary_cache_file()` and `_save_summary_cache_file()` functions.
-   **`_load_summary_cache_file()` / `_save_summary_cache_file()`**: These methods handle the serialization of the `cache_data` dictionary to and from the `.cache/summary_backup.json` file, ensuring persistence between runs. In the RAG generation workflow, these methods are explicitly called by the `RagGenerator`'s main summarization methods (`summarize_code_graph` and `summarize_targeted_update`) to manage the cache lifecycle.
-   **`main()` block**: The script can be run standalone to provide two command-line utilities:
    -   `python3 summary_manager.py backup`: Reads all summaries directly from the Neo4j graph and writes them to the JSON cache file.
    -   `python3 summary_manager.py restore`: Reads all summaries from the JSON cache file and writes them back to the Neo4j graph. This is useful for restoring a known good state.
    *Note*: When running standalone, the `SummaryManager` is initialized with default `llm_api='fake'` and `token_encoding='cl100k_base'` values.
