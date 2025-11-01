# Algorithm Summary: `code_graph_rag_generator.py`

## 1. Role in the Pipeline

This script enriches an existing Neo4j code graph with AI-generated summaries and vector embeddings. It has a dual-role architecture, providing two main entry points for different use cases:

*   **Full Build**: Invoked by `clangd_graph_rag_builder.py` during an initial project ingestion to summarize the entire graph from scratch.
*   **Incremental Update**: Invoked by `clangd_graph_rag_updater.py` to efficiently update summaries for only the parts of the graph affected by code changes.

## 2. Core Logic: Modular, Reusable Passes

The `RagGenerator` class has been refactored to use a set of modular, reusable methods for each summarization pass. This design allows both the full build and incremental update workflows to share the same core logic, reducing code duplication and improving clarity.

The core idea is to separate the "what to process" (querying for IDs) from the "how to process" (the summarization logic itself).

### Core Processing Methods

*   **`_summarize_functions_individually_with_ids()`**: The workhorse for Pass 1. It takes a list of function IDs, retrieves their source code, and generates a baseline `codeSummary` for each in parallel. It returns the set of function IDs that were actually updated.
*   **`_summarize_functions_with_context_with_ids()`**: The workhorse for Pass 2. It takes a list of function IDs, gathers call graph context for each, and generates a final, context-aware `summary`. It intelligently avoids database writes if the new summary is identical to the old one and returns the set of IDs that were actually updated.

### Parallelism Strategy

All I/O-bound calls to LLM and embedding APIs are heavily parallelized using a `ThreadPoolExecutor`. The script uses separate worker limits for local vs. remote APIs (`--num-local-workers`, `--num-remote-workers`) to optimize performance.

## 3. Full Build Workflow (`summarize_code_graph`)

This is the comprehensive, top-to-bottom workflow used for initial project ingestion.

1.  **`summarize_functions_individually()`**: Queries the graph for *all* functions that have source code but are missing a `codeSummary`. It then calls the core `_summarize_functions_individually_with_ids()` method to process them.
2.  **`summarize_functions_with_context()`**: Queries for *all* functions that have a `codeSummary` but are missing a final `summary`. It then calls the core `_summarize_functions_with_context_with_ids()` method.
3.  **`summarize_files_and_folders()`**: After all functions are summarized, this triggers the roll-up summaries for all files, then all folders, and finally the project node.
4.  **`generate_embeddings()`**: The final pass queries for *all* nodes in the graph with a `summary` but no `summaryEmbedding` and generates the vectors.

## 4. Incremental Update Workflow (`summarize_targeted_update`)

This is the "surgical strike" workflow used by the graph updater. It is designed to be highly efficient by minimizing work.

1.  **Input**: Receives a small set of `seed_symbol_ids` corresponding to functions directly inside changed files.
2.  **Pass 1 (Targeted)**: It calls `_summarize_functions_individually_with_ids()` with *only the seed IDs*. This ensures baseline summaries are created for any new or directly modified functions.
3.  **Scope Expansion**: It queries the graph to find the 1-hop neighbors (callers and callees) of the seed IDs, creating an expanded set of functions whose context may have changed.
4.  **Pass 2 (Targeted)**: It calls `_summarize_functions_with_context_with_ids()` with this *expanded set* (seeds + neighbors). This method intelligently re-evaluates the final `summary` for each function and returns the precise set of IDs for functions whose summaries were actually changed.
5.  **Smart Roll-up**: Using the precise set of updated function IDs from the previous step, it finds only the parent `:FILE` nodes that need to be re-summarized. It then triggers the roll-up process for those files and their affected parent folders. Unchanged parts of the hierarchy are not touched.
6.  **Embedding Generation**: It calls the standard `generate_embeddings()` method, which efficiently finds and embeds any node that was given a new or updated summary during the process.

## 5. The Summarization Passes in Detail

The generator's logic is broken into a series of dependent passes. Understanding how they interact is key to understanding the script's robustness.

### Pass 1 & 2: Function Summarization

The core of the summarization starts at the function level.
- **Pass 1 (`_summarize_functions_individually_with_ids`)**: Generates a `codeSummary` for a function based only on its source code. This provides a baseline understanding.
- **Pass 2 (`_summarize_functions_with_context_with_ids`)**: Refines the summary. It uses the `codeSummary` as a starting point and enriches it with the summaries of the function's direct callers and callees. This produces the final, context-aware `summary` property.

### Pass 3 & 4: File and Folder "Roll-Up" Summaries

Once functions have their final `summary`, the script aggregates this information upwards through the file system hierarchy.
- **File Summaries**: A file's summary is generated by asking an LLM to synthesize the final summaries of all the functions it contains.
- **Folder Summaries**: A folder's summary is generated by synthesizing the summaries of the files and sub-folders it directly contains. This process is performed "bottom-up" (from the deepest folders to the shallowest) to ensure that child summaries are available before the parent is processed.

### Pass 5: Embedding Generation and State Management

The final pass creates the vector embeddings that enable semantic search. This pass uses a simple but powerful state-based mechanism to correctly handle both new and updated nodes.

- **The Subtlety**: The `generate_embeddings` method does not need to be explicitly told which nodes were updated. Instead, it relies on the state of the node in the database.
- **Invalidation Step**: Whenever a summarization pass (Pass 2, 3, or 4) successfully generates a new or updated `summary` for a node, it performs two actions in the same database transaction:
    1.  `SET n.summary = $new_summary`
    2.  `REMOVE n.summaryEmbedding`
- **Discovery Step**: The `generate_embeddings` method then runs a simple query to find its work: `MATCH (n) WHERE n.summary IS NOT NULL AND n.summaryEmbedding IS NULL`.
- **Result**: This query naturally discovers **both** nodes that are being summarized for the first time **and** nodes whose summaries were just updated (because their old embedding was removed). This allows the embedding pass to be simple and decoupled from the others, while ensuring that no embeddings are ever stale.

## 6. Key Components & Dependencies

- **`Neo4jManager`**: Manages database interaction.
- **`SourceSpanProvider`**: Acts as a decoupled service and cache for function source code. It is initialized with the main `SymbolParser` to extract all function body locations, but it stores this information internally. This allows the large `SymbolParser` object to be released from memory before RAG generation begins, improving memory efficiency.
- **`LlmClient` / `EmbeddingClient`**: Abstractions for AI model APIs.
- **`SymbolParser`**: Used to provide symbol info to the `SourceSpanProvider`.

## 7. Execution

- **Standalone (Full Build):**
  ```bash
  python3 code_graph_rag_generator.py <index.yaml> <project_path/>
  ```
- **Integrated (Full Build):**
  ```bash
  python3 clangd_graph_rag_builder.py <index.yaml> <project_path/> --generate-summary
  ```
- **Integrated (Incremental Update):**
  Invoked automatically by `clangd_graph_rag_updater.py` when run with the `--generate-summary` flag.
