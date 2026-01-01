# RagGenerator: Design and Architecture

## 1. High-Level Role

The `code_graph_rag_generator.py` script, through its `RagGenerator` class, serves as the primary entry point and driver for a **full-build** RAG (Retrieval-Augmented Generation) enrichment process. It inherits from `RagOrchestrator` and its sole responsibility is to control the *sequence* of summarization passes for an entire graph, ensuring the process moves logically from low-level code constructs up to high-level architectural components.

It is invoked by `clangd_graph_rag_builder.py` to systematically summarize every relevant node in a newly created graph.

## 2. Core Architectural Principle: Inherited Orchestration

The `RagGenerator`'s design is simple because it leverages the powerful, distributed architecture of its parent class, `RagOrchestrator`. It does not contain any complex logic itself. Instead, it makes a series of high-level calls to the methods it inherits, relying on the underlying machinery to handle the details.

The architecture follows a clear separation of concerns:
*   **`RagGenerator` (The Driver)**: Its job is to define the **"what"** and **"when"** of a full build. It dictates the sequence of passes (functions, then classes, then namespaces, etc.).
*   **`RagOrchestrator` (The Logistics Layer)**: The parent class handles parallelism and all database I/O. Its worker functions fetch data for individual nodes.
*   **`NodeSummaryProcessor` (The Logic Layer)**: The "brain" that performs the state-aware caching and generation logic for each node.
*   **`SummaryCacheManager` (The Data Layer)**: The component that manages the persistence and state of the cache.

For more details on the underlying components, see `docs/summary_rag_orchestrator.md`, `docs/summary_node_summary_processor.md`, and `docs/summary_summary_cache_manager.md`.

## 3. The Multi-Pass Summarization Workflow (`summarize_code_graph`)

For a full build, `RagGenerator` executes a series of dependent passes in a strict sequence. This ensures that contextual information flows correctly from the bottom of the hierarchy upwards. At each step, it calls the appropriate summarization method from the parent `RagOrchestrator`, which handles the parallel execution.

1.  **Load Cache**: It begins by calling `self.summary_cache_manager.load()` to populate the in-memory cache from `summary_backup.json`, if it exists.

2.  **Pass 1: Individual Function Analyses (`analyze_functions_individually`)**
    *   **Action**: Queries for all `:FUNCTION` and `:METHOD` nodes in the graph.
    *   **Delegation**: It calls the inherited `_analyze_functions_individually_with_ids()` method, passing it all the collected IDs. The underlying worker/processor logic then handles hashing the source code, checking the cache, and generating a `code_analysis` for each function as needed.
    *   **Checkpoint**: After the pass completes, it calls `save(is_intermediate=True)` to persist the results to disk.

3.  **Pass 2: Contextual Function Summaries (`summarize_functions_with_context`)**
    *   **Action**: Queries for all functions/methods that now have a `code_analysis`.
    *   **Delegation**: It calls `_summarize_functions_with_context_with_ids()` on this full set of IDs. The workers then handle fetching dependencies and delegating to the processor, which determines if a new context-aware `summary` is needed.
    *   **Checkpoint**: The cache is saved again.

4.  **Pass 3: Class Summaries (`summarize_class_structures`)**
    *   **Action**: Fetches all `:CLASS_STRUCTURE` nodes.
    *   **Delegation**: It calls `_summarize_classes_with_ids()`. The orchestrator's logic processes the classes level-by-level through the inheritance hierarchy, ensuring parent summaries are generated before their children.
    *   **Checkpoint**: The cache is saved.

5.  **Pass 4: Namespace Summaries (`summarize_namespaces`)**
    *   **Action**: Fetches all `:NAMESPACE` nodes.
    *   **Delegation**: It calls `_summarize_namespaces_with_ids()`. The orchestrator processes namespaces from the most deeply nested outwards.
    *   **Checkpoint**: The cache is saved.

6.  **Pass 5 & 6: File and Folder Roll-Up Summaries**
    *   **Action**: The `_summarize_all_files()` and `_summarize_all_folders()` methods trigger the final roll-up.
    *   **Delegation**: They call the corresponding orchestrator methods, which process all files, and then all folders in a bottom-up fashion.
    *   **Checkpoint**: The cache is saved.

7.  **Pass 7: Embedding Generation (`generate_embeddings`)**
    *   **Action**: The final generation pass queries for *all* nodes that have a `summary` property but are missing a `summaryEmbedding`.
    *   **Logic**: It calls the `EmbeddingClient` to generate vectors for the summary text and then updates the nodes in the database.

8.  **Final Save**: A final call to `summary_cache_manager.save(mode="builder")` is made. In "builder" mode, this performs a pruning step, removing any entries from the cache that were not "visited" during the run, keeping the cache file clean and in sync with the newly built graph.

## 4. Standalone Execution

The script can be run directly to perform a full-build summarization on an existing graph.

```bash
# Example: Run a full RAG generation process
python3 code_graph_rag_generator.py /path/to/project/ --generate-summary --llm-api openai
```
The `main` function handles parsing arguments and initializing the `RagGenerator` and its dependencies.
