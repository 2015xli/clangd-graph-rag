# Project Documentation

This directory contains detailed design documents for the `clangd-graph-rag` project. 

For a comprehensive high-level overview of the project's architecture, design principles, and pipelines, please start with the main presentation summary.

---

### Comprehensive Overview

-   **[Building_an_AI-Ready_Code_Graph_RAG_based_on_Clangd_index.md](./Building_an_AI-Ready_Code_Graph_RAG_based_on_Clangd_index.md)**: A detailed, slide-by-slide breakdown of the entire project, covering high-level concepts, pipeline designs, architecture, and performance optimizations. A [PDF version](./Building_an_AI-Ready_Code_Graph_RAG_based_on_Clangd_index.pdf) is also available.

### RAG Generation Architectural Layers

These documents describe the core components of the refactored, three-layer RAG generation system.

-   **[summary_rag_orchestrator.md](./summary_rag_orchestrator.md)**: The Logistics & Validation Layer, responsible for workflow, parallelism, and data validation.
-   **[summary_node_summary_processor.md](./summary_node_summary_processor.md)**: The Logic Layer, which contains the "brain" for processing each node.
-   **[summary_summary_cache_manager.md](./summary_summary_cache_manager.md)**: The Data & Persistence Layer, responsible for cache safety and management.

### End-to-End Orchestrators

These documents describe the high-level scripts that a user runs to orchestrate end-to-end workflows.

-   **[summary_clangd_graph_rag_builder.md](./summary_clangd_graph_rag_builder.md)**: Describes the main pipeline for building the code graph from scratch.
-   **[summary_clangd_graph_rag_updater.md](./summary_clangd_graph_rag_updater.md)**: Describes the incremental update pipeline for the code graph based on Git changes.
-   **[summary_code_graph_rag_generator.md](./summary_code_graph_rag_generator.md)**: Describes the pipeline for running a full RAG summarization pass on the entire graph.
-   **[summary_rag_updater.md](./summary_rag_updater.md)**: Describes the pipeline for running a targeted, incremental RAG update.

### Core Ingestion Components

These documents detail the core modules responsible for each major stage of building the initial code graph.

-   **[summary_clangd_index_yaml_parser.md](./summary_clangd_index_yaml_parser.md)**: Explains the high-performance, parallel parsing of the raw `clangd` index file.
-   **[summary_compilation_manager.md](./summary_compilation_manager.md)**: Explains the high-level orchestration of source code parsing, caching, and strategy selection.
-   **[summary_compilation_parser.md](./summary_compilation_parser.md)**: Details the low-level parsing logic for extracting function spans and include relations.
-   **[summary_source_span_provider.md](./summary_source_span_provider.md)**: Details the critical process of reconciling `clangd` index data with source-parsed data to establish lexical hierarchies.
-   **[summary_path_processor.md](./summary_path_processor.md)**: Describes the logic for building the file and folder hierarchy in the graph.
-   **[summary_clangd_symbol_nodes_builder.md](./summary_clangd_symbol_nodes_builder.md)**: Details the creation of all logical code symbols (functions, classes, etc.) and their relationships.
-   **[summary_include_relation_provider.md](./summary_include_relation_provider.md)**: Covers the logic for ingesting and querying file include relationships.
-   **[summary_clangd_call_graph_builder.md](./summary_clangd_call_graph_builder.md)**: Covers the adaptive strategies for constructing the function call graph.

### Supporting Modules

These documents describe the helper modules that provide essential services like database access, Git integration, and argument parsing.

-   **[summary_neo4j_manager.md](./summary_neo4j_manager.md)**: The Data Access Layer for Neo4j.
-   **[summary_git_manager.md](./summary_git_manager.md)**: The abstraction layer for Git operations.
-   **[summary_llm_client.md](./summary_llm_client.md)**: The factory for providing model-agnostic LLM and embedding clients.
-   **[summary_input_params.md](./summary_input_params.md)**: The centralized module for handling command-line arguments.
-   **[summary_memory_debugger.md](./summary_memory_debugger.md)**: A simple utility for debugging memory usage.

### External Specifications

-   **[clangd-index-yaml-spec.txt](./clangd-index-yaml-spec.txt)**: Reference information on the `clangd` index format.
