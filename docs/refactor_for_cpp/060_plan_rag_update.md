# C++ Refactor Plan - Step 6: Update RAG Generator

## 1. Goal

With the graph schema now significantly changed to support C++, the RAG (Retrieval-Augmented Generation) system must be updated to understand and summarize the new constructs. The goal is to make `code_graph_rag_generator.py` aware of `:METHOD`, `:CLASS_STRUCTURE`, and `:NAMESPACE` nodes and to build a robust, scalable summarization pipeline.

## 2. Affected Files

1.  **`code_graph_rag_generator.py`**: The queries and summarization logic will be heavily updated.
2.  **`neo4j_manager.py`**: To add vector indexes for the new node types.

## 3. Implementation Plan

### 3.1. `neo4j_manager.py` (Implemented)

*   In `create_vector_indices`, queries have been added to create vector indexes for all summarizable node types.
    ```python
    "CREATE VECTOR INDEX method_summary_embeddings IF NOT EXISTS FOR (n:METHOD) ON (n.summaryEmbedding) OPTIONS {...}",
    "CREATE VECTOR INDEX class_summary_embeddings IF NOT EXISTS FOR (n:CLASS_STRUCTURE) ON (n.summaryEmbedding) OPTIONS {...}",
    "CREATE VECTOR INDEX namespace_summary_embeddings IF NOT EXISTS FOR (n:NAMESPACE) ON (n.summaryEmbedding) OPTIONS {...}",
    // ... plus existing indexes for FUNCTION, FILE, FOLDER
    ```

### 3.2. `code_graph_rag_generator.py`

The summarization logic was significantly refactored to handle the new C++ constructs and to be robust against LLM context window limits.

#### Core Principle: Iterative Summarization

To handle cases where the context for a summary (e.g., a large function body, or a class with hundreds of methods) exceeds the LLM's token limit, a unified **iterative summarization** strategy was implemented. The `max_context_token_size` is now configurable via the `--max-context-size` argument, and `iterative_chunk_size` and `iterative_chunk_overlap` are dynamically calculated based on this value.

Instead of failing or truncating, the system chunks the context (source code or lists of child summaries) and processes it sequentially. The summary from the first chunk is fed into the prompt for the second chunk, and so on. This allows context to be carried forward, enabling summarization of arbitrarily large components.

#### Special Token Sanitization (Implemented)
*   **Problem**: The `tiktoken` library, used for accurately counting tokens, would raise a `ValueError` if it encountered special model tokens (e.g., `<|endoftext|>`) directly within source code, as seen in projects like `llama.cpp`.
*   **Solution**: A `sanitize_special_tokens` helper function was added. This function uses a regex to find special tokens and add spaces within them (e.g., `<|endoftext|>` becomes `< |endoftext| >`). This prevents `tiktoken` from interpreting them as control tokens, allowing them to be processed as normal text. All calls to the tokenizer now use this sanitizer.

#### Prompt Abstraction (Implemented)
*   **Goal**: Improve maintainability and readability by centralizing prompt definitions.
*   **Implementation**: All prompts have been moved into a new module, `rag_generation_prompts.py`, within a `RagGenerationPromptManager` class. The `RagGenerator` now instantiates this manager and retrieves prompts via method calls, allowing for easier modification and experimentation with prompt wording.

#### Dynamic Constants (Implemented)
*   **Goal**: Make key LLM-related constants configurable.
*   **Implementation**: `MAX_CONTEXT_TOKEN_SIZE` is now an optional command-line argument (`--max-context-size`), defaulting to 30000. `ITERATIVE_CHUNK_SIZE` and `ITERATIVE_CHUNK_OVERLAP` are dynamically calculated as 50% and 10% of `max_context_size`, respectively.

#### Pass 1: `codeSummary` for Functions and Methods (Implemented)

*   **Goal**: Generate a baseline summary for every function and method based purely on its source code.
*   **Implementation**:
    1.  The `summarize_functions_individually` method queries for both `:FUNCTION` and `:METHOD` nodes.
    2.  The core logic uses the `_summarize_text_iteratively` helper method for all functions/methods, regardless of size.
    3.  **Token-based Chunking**: This helper uses the `tiktoken` library to accurately count tokens in the sanitized source code. If the count exceeds a threshold, the code is split into overlapping chunks.
    4.  **Dynamic Prompts**: The summarization loop uses dynamic prompts, varying them for the first, middle, and last chunks to give the LLM proper context. These prompts are now managed by `RagGenerationPromptManager`.

#### Pass 2: Contextual `summary` for Functions and Methods (Implemented)

*   **Goal**: Enrich the `codeSummary` with call graph context to create a final, high-level `summary`.
*   **Implementation**:
    1.  The main logic in `_process_one_function_for_contextual_summary` acts as an orchestrator, checking if the full context (code summary + caller summaries + callee summaries) fits in a single LLM call.
    2.  If it doesn't fit, it triggers a **Two-Stage Iteration**:
        *   **Stage 1 (Callers)**: Iteratively folds in the summaries of all **callers**.
        *   **Stage 2 (Callees)**: Takes the result from Stage 1 and iteratively folds in the summaries of all **callees**.
    3.  The `_summarize_relations_iteratively` helper method has been refactored to directly query the `RagGenerationPromptManager` for prompt templates, simplifying its interface. Logging within this method has also been optimized to only show progress when multiple chunks are being processed.

#### Pass 3: Class Summaries (Implemented)

*   **Goal**: Generate a high-level summary for each `:CLASS_STRUCTURE` node.
*   **Implementation**:
    1.  A new `summarize_class_structures` pass was added to the main pipeline.
    2.  For each class, `_summarize_one_class_structure` queries the graph to gather context:
        *   Summaries of parent classes from `[:INHERITS]` relationships.
        *   Final summaries of its own methods from `[:HAS_METHOD]` relationships.
        *   The names and types of its fields from `[:HAS_FIELD]` relationships.
    3.  The same iterative summarization strategy is used. If the context is too large, it first folds in parent summaries, then method summaries, to build the final class summary. Prompts are managed by `RagGenerationPromptManager`.

#### Pass 4: File/Folder/Project Summaries (Partially Implemented)

*   The query in `_summarize_one_file` has been updated to gather summaries from both `:FUNCTION` and `:CLASS_STRUCTURE` nodes defined within the file, providing a more complete context for the file's summary. Prompts are managed by `RagGenerationPromptManager`.
*   Folder and Project summary logic remains unchanged for now but benefits from the richer file summaries. Prompts are managed by `RagGenerationPromptManager`.

#### Final Pass: Embeddings (Implemented)

*   The `_get_nodes_for_embedding` query has been updated to find all summarized node types, now including `:METHOD` and `:CLASS_STRUCTURE`, ensuring they get a `summaryEmbedding`.

#### Future Passes (To Be Implemented)

*   **Namespace Summaries**: Will be implemented using a bottom-up, iterative approach.

## 4. Verification

1.  Run the full builder pipeline with RAG generation on a C++ project.
2.  Verify that `codeSummary` and `summary` properties are correctly generated for both `:FUNCTION` and `:METHOD` nodes.
3.  **Verify that `summary` properties are correctly generated for `:CLASS_STRUCTURE` nodes, synthesizing information from their methods, fields, and parents.**
4.  Inspect the summaries for large functions and classes to ensure they are coherent and complete.
5.  **Verify that file summaries now reflect the purpose of the classes they contain, not just the free functions.**
