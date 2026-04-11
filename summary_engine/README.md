# Summary Engine: AI-Powered Code Summarization

The `summary_engine` is the core intelligence layer of the `clangd-graph-rag` project. It transforms raw structural graph data into a semantically rich knowledge base through a multi-pass, dependency-aware summarization process.

## 1. Core Architectural Pillars

### 1.1 Separation of Concerns (Logistics vs. Logic)
*   **Orchestrator (`orchestrator.py`)**: Manages Neo4j I/O, parallelism, and processing order. It is the sole gatekeeper for database writes and serial state mutation.
*   **Processor (`node_summarizer.py`)**: A stateless "Brain" that performs decision-making, LLM interaction, and token management.

### 1.2 The "Waterfall" Decision Process
To minimize API costs, every node follows a strict priority path:
1.  **DB Hit**: Perfect state (Matches DB hash, context is clean).
2.  **Cache Hit**: Restorable state (Matches L1 cache hash, context is clean).
3.  **Regeneration**: Fallback to LLM call.

### 1.3 Map-Reduce Execution
Parallel "Map" phase (LLM calls) followed by a **Serial "Reduce" phase** in the main thread. This ensures that modifications to the `SummaryCacheManager` and runtime status tracking are thread-safe and deterministic.

---

## 2. Multi-Pass Summarization Pipeline

Summarization is performed in strictly ordered passes to ensure that dependencies are resolved before they are needed as context.

| Pass | Target | Description |
| :--- | :--- | :--- |
| **Pass 1** | Functions & Classes | Granular analysis for functions; Physical hashing for classes. |
| **Pass 2** | Functions (Context) | Roll-up of analysis plus caller/callee context. |
| **Pass 3.1**| SCC Clusters | Resolves recursive inheritance cycles first to "unlock" the graph. |
| **Pass 3.2**| DAG Classes | Standard level-based roll-up for the remaining class hierarchy. |
| **Pass 4** | Namespaces | Logical roll-up of entities into C++ namespaces. |
| **Pass 5** | Files | Structural manifests of inclusions and symbol definitions. |
| **Pass 6** | Folders | Physical roll-up of the directory structure. |
| **Pass 7** | Project | Final top-level architectural summary. |

---

## 3. Deep Dive Documentation

For detailed technical rationales, Cypher queries, and implementation nuances, please refer to the following specialized documents:

*   **[Recursion & SCC Handling](./docs/class_recursion.md)**: How we solve C++ template recursion and mutual inheritance.
*   **[Structural Manifests & Hashing](./docs/context_manifests.md)**: Detailed logic for Class, File, and Folder summarization and physical staleness tracking.
*   **[Orchestration & Infrastructure](./docs/orchestration_engine.md)**: Details on the Map-Reduce engine, Waterfall logic, and Dynamic Context Detection.

---

## 4. Summary of Status Codes
*   `unchanged`: Database state is perfectly synchronized.
*   `summary_restored`: Recovered from L1 cache; DB updated, but no upstream changes triggered.
*   `summary_regenerated`: New LLM content created; triggers upstream dependency staleness.
*   `no_children`: Node has no relevant summarized components (handled by manifests).
*   `generation_failed`: Hard error (e.g., timeout). Prevents cache pollution.
