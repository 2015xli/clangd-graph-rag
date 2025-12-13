# RAG Generation: Detailed Design and Architecture

This document provides a comprehensive overview of the design and architecture of the Retrieval-Augmented Generation (RAG) system. It details the components, workflows, and core mechanisms that enable robust, efficient, and dependency-aware summary generation for the code graph.

## 1. High-Level Principles

The design is guided by the following principles:

*   **Distributed, Worker-Driven Logic**: The core decision-making logic (staleness checks, caching, LLM calls) is distributed into worker threads. Each worker is responsible for a single node, making the system modular and scalable.
*   **Simplified Orchestration**: The main `RagOrchestrator` class is simplified. Its primary roles are to manage the high-level processing order (e.g., by inheritance or directory depth), dispatch tasks to the parallel engine, and handle database I/O.
*   **State-Aware Processors**: The `NodeSummaryProcessor` contains the "brain" for each node type. It is state-aware, accessing the shared cache to intelligently decide whether to regenerate a summary, restore it from cache, or do nothing.
*   **Robust, Safe Persistence**: The system is designed to be resilient against failures and to protect the integrity of the summary cache. This is achieved through intermediate saves to temporary files and a "Promote-on-Success" strategy for the final cache file, which includes sanity checks and rolling backups.
*   **Explicit Failure States**: The system distinguishes between normal "unchanged" states and true `generation_failed` states, allowing for better debugging and preventing the propagation of `null` data.

## 2. Component Responsibilities

The architecture is decomposed into three distinct layers, each with a clear responsibility.

### 2.1. `RagOrchestrator` (The Logistics and Validation Layer)

This class is the **"orchestration and logistics layer"**. It manages the overall workflow, controls parallelism, and is the only component responsible for direct database interactions.

*   **Responsibilities**:
    *   Determines the **scope** of work (all nodes for a `RagGenerator` build, or a seed set for a `RagUpdater` update).
    *   Enforces **processing order** for passes with hierarchical dependencies.
    *   Manages the `ThreadPoolExecutor` via the `_parallel_process` method.
    *   Acts as the **central gatekeeper for cache quality**, ensuring that only valid, non-empty summaries are ever written to the in-memory cache.
    *   Contains the **worker functions** (e.g., `_process_one_class_summary`) that handle fetching data from Neo4j and writing results back.

### 2.2. `NodeSummaryProcessor` (The Logic Layer)

This class is the stateless **"brain"** of the RAG process. It encapsulates all the intelligence for processing a single node.

*   **Responsibilities**:
    *   Receives node data and dependency information from a worker function.
    *   Implements the robust **"Waterfall" Decision Process** (check DB -> check cache -> regenerate) for each node type.
    *   Constructs prompts and executes LLM calls.
    *   Handles complex logic like iterative summarization for large contexts.
    *   Returns a granular status (`unchanged`, `restored`, `regenerated`, `generation_failed`) and data payload to the worker.

### 2.3. `SummaryCacheManager` (The Data and Persistence Layer)

This class is the dedicated **data layer**. It is the single source of truth for all cached summary data and its persistence.

*   **Responsibilities**:
    *   Manages the in-memory `cache` dictionary.
    *   Manages the transient `runtime_status` dictionary for the current run.
    *   Handles loading from `.cache/summary_backup.json`.
    *   Implements the robust **"Promote-on-Success"** save strategy with temporary files, sanity checks, and rolling backups.
    *   Provides a **command-line interface** for manual backup and restore operations.

## 3. Core Mechanisms

### 3.1. The Worker-Processor-Cache Interaction

The entire system revolves around the interaction between these three components, executed in parallel by the `_parallel_process` engine.

1.  **Dispatch**: The `RagOrchestrator` dispatches a node `id` to a worker.
2.  **Worker (Prep)**: The worker function queries Neo4j to get the data for that `id` and its dependencies.
3.  **Processor (Logic)**: The worker passes this data to the `NodeSummaryProcessor`. The processor follows the "waterfall" logic: it checks the DB state, then the cache state, and finally decides if it needs to call the LLM to regenerate a summary. It returns a `status` and `data`.
4.  **Worker (Finalize)**: The worker receives the result. If the status is `..._regenerated` or `..._restored`, it writes the data to Neo4j. It then returns the result packet to the orchestrator.
5.  **Orchestrator (Reduce & Validate)**: The `_parallel_process` loop receives the packet. It inspects the `data` and **only updates the in-memory cache if the summary is valid and non-empty**. It then sets the appropriate `runtime_status` flags based on the `status`.

### 3.2. The "Promote-on-Success" Cache Strategy

This mechanism ensures the integrity of the main cache file (`summary_backup.json`).
1.  **Intermediate Saves**: During a run, the cache is saved to a temporary file (`summary_backup.json.tmp`) after each pass. The main file and its backups are never touched.
2.  **Final Save**: At the end of a successful run, a final version of the cache is written to the `.tmp` file.
3.  **Sanity Check**: The system compares the number of entries in the new `.tmp` file to the existing main cache file.
4.  **Promotion**: If the new cache is not drastically smaller (e.g., >95% of the old size), the old backups are rotated (`.json` -> `.bak.1`, etc.) and the `.tmp` file is renamed to become the new `summary_backup.json`.
5.  **Failure**: If the sanity check fails, the promotion is aborted, a critical error is logged, and the `.tmp` file is left for manual inspection, protecting the known-good backups.

## 4. The Multi-Pass Workflow

The `RagGenerator` and `RagUpdater` execute a series of passes in a specific order to ensure dependencies are met.

1.  **Pass 1: Code Summaries**: Generates `codeSummary` for functions/methods.
2.  **Pass 2: Contextual Summaries**: Generates final `summary` for functions/methods.
3.  **Pass 3: Class Summaries**: Generates `summary` for classes.
4.  **Pass 4: Namespace Summaries**: Generates `summary` for namespaces.
5.  **Pass 5-7: Hierarchical Summaries**: Generates `summary` for files, folders, and the project.
6.  **Final Pass: Embeddings**: Generates vector embeddings for any node with a new or updated `summary`.

At the end of each pass, an intermediate save to the `.tmp` file is performed to ensure robustness against failures. At the very end of the entire process, the final promotion of the cache is attempted.