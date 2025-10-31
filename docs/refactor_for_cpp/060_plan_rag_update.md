# C++ Refactor Plan - Step 6: Update RAG Generator

## 1. Goal

With the graph schema now significantly changed to support C++, the RAG (Retrieval-Augmented Generation) system must be updated to understand and summarize the new constructs. The goal is to make `code_graph_rag_generator.py` aware of `METHOD`, `CLASS_STRUCTURE`, and `NAMESPACE` nodes.

## 2. Affected Files

1.  **`code_graph_rag_generator.py`**: The queries and summarization prompts will need to be updated.
2.  **`neo4j_manager.py`**: To add vector indexes for the new node types.

## 3. Implementation Plan

### 3.1. `neo4j_manager.py`

*   In `create_vector_indices`, add queries to create indexes for the new node types that will have summaries.
    ```python
    "CREATE VECTOR INDEX method_summary_embeddings IF NOT EXISTS FOR (n:METHOD) ON (n.summaryEmbedding) OPTIONS {...}",
    "CREATE VECTOR INDEX class_summary_embeddings IF NOT EXISTS FOR (n:CLASS_STRUCTURE) ON (n.summaryEmbedding) OPTIONS {...}",
    "CREATE VECTOR INDEX namespace_summary_embeddings IF NOT EXISTS FOR (n:NAMESPACE) ON (n.summaryEmbedding) OPTIONS {...}",
    ```

### 3.2. `code_graph_rag_generator.py`

The changes here involve broadening the scope of existing functions.

*   **In `summarize_functions_individually`**:
    *   The query `MATCH (n:FUNCTION) ...` should be changed to `MATCH (n) WHERE n:FUNCTION OR n:METHOD ...`.
    *   The processing logic in `_process_one_function_for_code_summary` can likely remain the same, as it just needs a `body_location` to fetch source code. The prompt can be generalized or updated to say "function or method".

*   **In `summarize_functions_with_context`**:
    *   Similarly, the queries must be updated to include `METHOD` nodes.
    *   The context-gathering query in `_process_one_function_for_contextual_summary` needs to be updated to find callers and callees that can be either `:FUNCTION` or `:METHOD`.
        ```cypher
        // In the query
        OPTIONAL MATCH (caller)-[:CALLS]->(n) WHERE caller:FUNCTION OR caller:METHOD
        OPTIONAL MATCH (n)-[:CALLS]->(callee) WHERE callee:FUNCTION OR callee:METHOD
        ```

*   **Create New Summarization Passes**: We need new methods to summarize classes and namespaces. These should be called *after* methods are summarized but *before* files are summarized.
    *   **`summarize_classes()`**:
        *   This new method will query for all `:CLASS_STRUCTURE` nodes.
        *   For each class, it will gather the summaries of its methods (`HAS_METHOD`) and fields (`HAS_FIELD`).
        *   It will generate a prompt like: "A C++ class named 'MyClass' has methods for [method summaries] and data members for [field names/types]. What is the overall purpose of this class?"
        *   It will store the result in the `summary` property of the `CLASS_STRUCTURE` node.
    *   **`summarize_namespaces()`**:
        *   This method will query for `:NAMESPACE` nodes.
        *   It should be run bottom-up, similar to folders.
        *   For each namespace, it will gather the summaries of the classes, functions, and other namespaces it contains.
        *   It will generate a prompt to synthesize these into a summary for the namespace.

*   **Update `_summarize_one_file`**:
    *   The query should be updated to gather summaries from all symbol types it defines, not just functions.
        ```cypher
        MATCH (f:FILE {path: $path})-[:DEFINES]->(s)
        WHERE (s:FUNCTION OR s:METHOD OR s:CLASS_STRUCTURE) AND s.summary IS NOT NULL
        RETURN s.summary AS summary
        ```

*   **Update `_get_nodes_for_embedding`**:
    *   The final embedding pass needs to be updated to find all nodes that have summaries.
        ```cypher
        MATCH (n)
        WHERE (n:FUNCTION OR n:METHOD OR n:CLASS_STRUCTURE OR n:NAMESPACE OR n:FILE OR n:FOLDER OR n:PROJECT)
          AND n.summary IS NOT NULL 
          AND n.summaryEmbedding IS NULL
        RETURN elementId(n) AS elementId, n.summary AS summary
        ```

## 4. Verification

1.  Run the full builder pipeline with RAG generation on a C++ project.
2.  Verify in Neo4j that `summary` and `summaryEmbedding` properties exist on `:METHOD`, `:CLASS_STRUCTURE`, and `:NAMESPACE` nodes.
3.  Read some of the generated summaries to ensure they are logical and context-aware.
4.  Ensure the roll-up summaries for files and folders correctly incorporate the summaries from the new C++ constructs.
