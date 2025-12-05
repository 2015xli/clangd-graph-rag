# RagGenerator: Design and Architecture

## 1. High-Level Role

The `code_graph_rag_generator.py` script, through its `RagGenerator` class, serves as the primary **orchestrator** for the multi-pass RAG (Retrieval-Augmented Generation) enrichment process. Its sole responsibility is to control the *sequence* of summarization, traversing the code graph in a logical, dependency-aware order. It delegates all the complex logic of caching, staleness checking, and LLM interaction to the `SummaryManager`.

It operates in two distinct modes:

*   **Full Build**: Invoked by `clangd_graph_rag_builder.py`, it systematically summarizes every relevant node in the entire graph.
*   **Incremental Update**: Invoked by `clangd_graph_rag_updater.py`, it performs a "surgical" update, summarizing only the nodes affected by a specific set of code changes.

## 2. Core Architectural Principle: Orchestration vs. Provision

The refactored design establishes a clear separation of concerns between the `RagGenerator` and the `SummaryManager`.

*   **`RagGenerator` (The Orchestrator)**: Its job is to answer **"what"** and **"when"** to summarize.
    1.  It determines the correct order of operations (e.g., functions before classes, children before parents).
    2.  For each node, it queries the graph to fetch the entity itself and its direct dependencies (e.g., a function and its callers/callees).
    3.  It makes a single, high-level call to the `SummaryManager` to request a summary.
    4.  It persists the final result returned by the manager to the Neo4j database.

*   **`SummaryManager` (The Provider)**: Its job is to answer **"how"** to provide a summary.
    *   It encapsulates all logic for caching, staleness checks, prompt engineering, and LLM calls. It also internally manages the LLM client, tokenizer, and prompt manager. It is the intelligent engine of the summarization process. For more details, see `docs/summary_summary_manager.md`.

## 3. The Multi-Pass Summarization Workflow (`summarize_code_graph`)

For a full build, `RagGenerator` executes a series of dependent passes in a strict sequence to ensure that contextual information flows correctly from the bottom of the hierarchy upwards.

1.  **Pass 1: Individual Function Summaries (`summarize_functions_individually`)**
    *   **Action**: Queries for all `:FUNCTION` and `:METHOD` nodes in the graph.
    *   **Delegation**: For each function, it calls `_process_one_function_for_code_summary`, which retrieves the raw source code and delegates to `summary_mgr.get_code_summary()` to get a baseline, code-only summary.

2.  **Pass 2: Contextual Function Summaries (`summarize_functions_with_context`)**
    *   **Action**: Queries for all functions/methods that now have a `codeSummary`.
    *   **Delegation**: For each function, it calls `_process_one_function_for_contextual_summary`, which fetches its callers and callees from the graph and delegates to `summary_mgr.get_function_contextual_summary()` to produce the final, context-aware `summary`.

3.  **Pass 3: Class Summaries (`summarize_class_structures`)**
    *   **Action**: Fetches all `:CLASS_STRUCTURE` nodes and groups them by inheritance depth using `_get_classes_by_inheritance_level`. It then processes these groups sequentially, from deepest to shallowest.
    *   **Delegation**: For each class, it calls `_summarize_one_class_structure`, which fetches its parents, methods, and fields, and delegates to `summary_mgr.get_class_summary()`. This ordering guarantees parent summaries are available when processing derived classes.

4.  **Pass 4: Namespace Summaries (`summarize_namespaces`)**
    *   **Action**: Fetches all `:NAMESPACE` nodes and groups them by nesting depth (by counting `::` in the qualified name). It processes these groups from deepest to shallowest.
    *   **Delegation**: For each namespace, it calls `_summarize_one_namespace`, which fetches its direct children and delegates to `summary_mgr.get_namespace_summary()`.

5.  **Pass 5 & 6: File and Folder Roll-Up Summaries**
    *   **Action**: The `_summarize_all_files()` and `_summarize_all_folders()` methods trigger the final roll-up. They process all files, and then all folders in a bottom-up fashion.
    *   **Delegation**: The worker methods (`_summarize_one_file`, `_summarize_one_folder`) fetch the direct children from the graph and delegate to the corresponding `SummaryManager` methods (`get_file_summary`, `get_folder_summary`).

6.  **Pass 7: Embedding Generation (`generate_embeddings`)**
    *   **Action**: The final pass queries for *all* nodes that have a `summary` property but are missing a `summaryEmbedding`. This state-based approach elegantly handles both newly created and recently updated summaries.
    *   **Logic**: It calls the `EmbeddingClient` to generate vectors for the summary text and then updates the nodes in the database.

## 4. Worker Methods: The Orchestration Pattern

All the `_process_one_*` and `_summarize_one_*` methods follow the same clean, three-step orchestration pattern:

1.  **Fetch Dependencies**: Execute one or more Cypher queries to get the primary entity and all the contextually relevant dependent entities from the graph.
2.  **Delegate to Manager**: Make a single call to the appropriate `SummaryManager.get_*_summary()` method, passing the fetched entities. The manager handles all further logic.
3.  **Persist Result**: If the manager returns a new summary string (indicating a "cache miss" and successful regeneration), execute a final Cypher query to `SET` the new summary on the node and `REMOVE` the now-stale embedding.

## 5. Incremental Update Workflow (`summarize_targeted_update`)

This workflow is optimized to perform the minimum work necessary.

1.  **Input**: It receives a `seed_symbol_ids` set (functions in changed files) and a `structurally_changed_files` dictionary from the `GraphUpdater`.
2.  **Pass 1 (Targeted Function Code Summaries)**: It calls `_summarize_functions_individually_with_ids()` on *only the seed IDs*.
3.  **Scope Expansion (Functions)**: It performs a 1-hop graph query (`_get_neighbor_ids`) to find the direct callers and callees of the functions whose code summaries were just updated. This creates an expanded set of functions whose context may have changed.
4.  **Pass 2 (Targeted Function Contextual Summaries)**: It calls `_summarize_functions_with_context_with_ids()` on this expanded set. The `SummaryManager`'s internal logic ensures that only functions with stale dependencies are actually regenerated.
5.  **Pass 3 (Targeted Class Summaries)**: It identifies `CLASS_STRUCTURE` nodes whose methods were updated or whose defining files were changed. It then calls `_summarize_targeted_class_structures()` on these identified classes, processing them in inheritance order.
6.  **Pass 4 (Targeted Namespace Summaries)**: It identifies `NAMESPACE` nodes whose children (functions, classes, nested namespaces) were updated or whose defining files were changed. It then calls `_summarize_targeted_namespaces()` on these identified namespaces, processing them from deepest to shallowest.
7.  **Smart Roll-up (Files, Folders, Project)**: It identifies the specific files and parent folders affected by the updated function, class, and namespace summaries, as well as structural changes. It then triggers the summarization passes (`_summarize_files_with_paths`, `_summarize_folders_with_paths`, `_summarize_project`) for *only those entities*.
8.  **Embedding Generation**: It runs the standard `generate_embeddings()` pass, which automatically finds and updates embeddings for any node whose summary was changed.

## 6. Key Components & Dependencies

-   **`SummaryManager`**: The intelligent provider for all summaries. This is the `RagGenerator`'s most important dependency.
-   **`Neo4jManager`**: Used for all direct graph database interactions (querying and updating).
-   **`EmbeddingClient`**: Used in the final pass to generate vector embeddings.
-   **`input_params.py`**: Used to define and parse command-line arguments for standalone execution.

## 7. Standalone Execution

The script can be run directly to perform a full-build summarization on an existing graph.

```bash
# Example: Run a full RAG generation process
python3 code_graph_rag_generator.py /path/to/project/ --generate-summary --llm-api openai --token-encoding cl100k_base
```
The `main` function in the script handles:
- Parsing command-line arguments.
- Initializing the `EmbeddingClient` and `SummaryManager` (which internally manages `LlmClient`, `RagGenerationPromptManager`, and `tiktoken` tokenizer).
- Initializing and running the `RagGenerator`. The `RagGenerator` itself is now responsible for loading and saving the summary cache.
