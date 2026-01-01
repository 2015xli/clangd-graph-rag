# RagUpdater: Design and Architecture

## 1. High-Level Role

The `rag_updater.py` script, through its `RagUpdater` class, serves as the primary entry point and driver for an **incremental** RAG (Retrieval-Augmented Generation) enrichment process. It inherits from `RagOrchestrator` and is designed to perform a "surgical" update, summarizing only the nodes affected by a specific set of code changes identified from a `git diff`.

It is invoked by `clangd_graph_rag_updater.py` after the graph's structure has been patched. Its goal is to efficiently bring the AI-generated summaries and embeddings in sync with the new code state without re-processing the entire graph.

## 2. Core Architectural Principle: Targeted Seeding and Propagation

The `RagUpdater`'s core principle is to **identify a small "seed set" of changed nodes and then rely on the multi-pass, dependency-aware logic of its parent `RagOrchestrator` to propagate those changes up the graph hierarchy.**

It leverages the same underlying architecture as the full builder:
*   **`RagUpdater` (The Driver)**: Its job is to determine the initial **scope** of the RAG update based on the output of the graph structure update.
*   **`RagOrchestrator` (The Logistics Layer)**: The parent class handles the parallelism, database I/O, and pass-by-pass execution order.
*   **`NodeSummaryProcessor` (The Logic Layer)**: The "brain" that performs the state-aware caching and generation logic for each node.
*   **`SummaryCacheManager` (The Data Layer)**: The component that manages the cache, which is critical for determining staleness.

For more details on the underlying components, see `docs/summary_rag_orchestrator.md`, `docs/summary_node_summary_processor.md`, and `docs/summary_summary_cache_manager.md`.

## 3. The Incremental Update Workflow (`summarize_targeted_update`)

The `summarize_targeted_update` method orchestrates the entire workflow. It receives a `seed_symbol_ids` set (from symbols in changed files) and a `structurally_changed_files` dictionary from the main `GraphUpdater`.

1.  **Load Cache**: It begins by calling `self.summary_cache_manager.load()` to populate the in-memory cache from `summary_backup.json`.

2.  **Pass 1: Targeted Code Analyses**
    *   **Action**: It calls `_analyze_functions_individually_with_ids()` on **only the `seed_symbol_ids`**.
    *   **Logic**: The `NodeSummaryProcessor` checks the code hash for each of these seed functions against the cache. Only functions with changed code will be regenerated, and their `code_analysis_changed` flag will be set in the `runtime_status`.
    *   **Checkpoint**: An intermediate save of the cache is performed.

3.  **Pass 2: Contextual Function Summaries**
    *   **Scope Expansion**: It calls `_get_neighbor_ids()` to find the direct callers and callees of the functions whose code analyses were just updated. This expanded set (`all_function_ids_to_process`) becomes the input for the next step.
    *   **Delegation**: It calls `_summarize_functions_with_context_with_ids()` on this expanded set. The distributed logic in the workers ensures that only functions that are themselves stale or have a stale neighbor are actually re-summarized.
    *   **Checkpoint**: The cache is saved.

4.  **Pass 3: Targeted Class Summaries**
    *   **Scope Expansion**: It identifies a seed set of `CLASS_STRUCTURE` nodes that need to be considered for re-summarization. A class is included if:
        *   It was one of the original seed symbols.
        *   One of its methods had its final `summary` updated in Pass 2.
        *   Its defining file was structurally added or modified.
    *   **Delegation**: It calls `_summarize_classes_with_ids()` on this targeted set. The orchestrator's level-by-level processing and the processor's staleness checks ensure only the necessary classes and their descendants are updated.
    *   **Checkpoint**: The cache is saved.

5.  **Pass 4: Targeted Namespace Summaries**
    *   **Scope Expansion**: Similarly, it identifies a seed set of `NAMESPACE` nodes if their contained children (functions, classes) were updated in previous passes or if their declaring file was changed.
    *   **Delegation**: It calls `_summarize_namespaces_with_ids()` on this set.
    *   **Checkpoint**: The cache is saved.

6.  **Pass 5 & 6: Smart File and Folder Roll-Up**
    *   **Scope Expansion**: It determines the precise set of files to re-summarize by collecting all files that either contain updated symbols or were structurally changed. It then finds all parent folders of these files, creating the minimal set of hierarchical nodes that need re-evaluation.
    *   **Delegation**: It calls `_summarize_files_with_paths()` and `_summarize_folders_with_paths()` on these small, targeted sets.
    *   **Checkpoint**: The cache is saved.

7.  **Pass 7: Embedding Generation**
    *   The standard `generate_embeddings()` pass is called. It automatically finds all nodes whose `summary` was changed (and `summaryEmbedding` was removed) and generates new embeddings.

8.  **Final Save**: A final call to `summary_cache_manager.save(mode="updater")` is made.
    *   In `"updater"` mode, the cache is **not** pruned. This is critical because the update only touched a subset of the graph, and we must not delete the valid cache entries for all the untouched nodes.
    *   This final save also triggers the "cache healing" logic if the run started without a cache file.