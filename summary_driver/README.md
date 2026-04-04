# Summary Driver: RAG Workflow Orchestration

This package provides the top-level orchestrators for the AI enrichment (RAG) process. It defines how the system systematically generates and updates code summaries and embeddings for the entire graph.

## Table of Contents
1. [Full RAG Generation (full_summarizer.py)](#1-full-rag-generation-full_summarizerpy)
2. [Incremental RAG Update (incremental_summarizer.py)](#2-incremental-rag-update-incremental_summarizerpy)

---

## 1. Full RAG Generation (full_summarizer.py)

The `FullSummarizer` drives the RAG enrichment for an entire codebase, typically after a fresh full build. It systematically processes every relevant node in the graph, from individual functions up to the project root.

### The Multi-Pass Workflow
To ensure contextual information flows correctly, the summarizer executes passes in a strict sequence:

1.  **Pass 1: Individual Function Analysis**: Generates code-only summaries based on the raw source of every function and method.
2.  **Pass 2: Contextual Function Summarization**: Refines function summaries by incorporating the responsibilities of their direct callers and callees.
3.  **Pass 3: Class Structures**: Summarizes classes level-by-level through the inheritance hierarchy.
4.  **Pass 4: Namespaces**: Summarizes namespaces based on their contained logical members.
5.  **Pass 5 & 6: File and Folder Roll-ups**: Bottom-up physical roll-up, aggregating node summaries into file summaries, and file summaries into folder summaries.
6.  **Pass 7: Project Summary**: The final architectural overview of the entire project.
7.  **Pass 8: Embedding Generation**: Generates vector embeddings for every finalized summary to enable semantic search.

---

## 2. Incremental RAG Update (incremental_summarizer.py)

The `IncrementalSummarizer` performing a "surgical" update. Its goal is to synchronize the AI's knowledge with the new code state after a Git change, without re-processing the entire graph.

### Principle: Targeted Seeding and Propagation
The updater identifies a small "seed set" of changed nodes and then relies on the dependency-aware logic of the `SummaryEngine` to propagate those changes up the hierarchy.

#### The Update Pipeline
1.  **Seed Identification**: Identifies functions directly changed in "dirty" files.
2.  **Pass 1 (Targeted Analysis)**: Regenerates code analysis only for the seed functions.
3.  **Pass 2 (Context expansion)**: Identifies neighbors (callers/callees) of changed functions. Only re-summarizes functions that are stale or have stale neighbors.
4.  **Logical Propagation**: Identifies and re-summarizes parent classes and namespaces if any of their children were updated.
5.  **Physical Roll-up**: Re-summarizes only the files and parent folders affected by the changed symbols.
6.  **Embeddings**: Automatically identifies nodes with missing embeddings and updates them.

### Standalone Usage
You can trigger a full RAG generation on an existing graph:
```bash
python3 -m summary_driver <index.yaml> <project_path> --llm-api <api_name>
```
