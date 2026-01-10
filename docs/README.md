# Project Design Documentation

This directory contains detailed design documents for the `clangd-graph-rag` project. 

For a comprehensive high-level overview of the project's architecture, design principles, and pipelines, please start with the main presentation summary.

---

### Comprehensive Overview

-   **[Building_an_AI-Ready_Code_Graph_RAG_based_on_Clangd_index.md](./Building_an_AI-Ready_Code_Graph_RAG_based_on_Clangd_index.md)**: A detailed, slide-by-slide breakdown of the entire project, covering high-level concepts, pipeline designs, architecture, and performance optimizations. 

### End-to-End Orchestrators

These documents describe the high-level scripts that a user runs to orchestrate end-to-end workflows.

#### Graph building orchestrators
-   **[summary_clangd_graph_rag_builder.md](./summary_clangd_graph_rag_builder.md)**: Describes the main pipeline for building the code graph from scratch.
-   **[summary_clangd_graph_rag_updater.md](./summary_clangd_graph_rag_updater.md)**: Describes the incremental update pipeline for the code graph based on Git changes.

#### RAG generation orchestrators
-   **[summary_code_graph_rag_generator.md](./summary_code_graph_rag_generator.md)**: Describes the pipeline for running a full RAG summarization pass on the entire graph.
-   **[summary_rag_updater.md](./summary_rag_updater.md)**: Describes the pipeline for running a targeted, incremental RAG update.

### Graph Building Components

These documents detail the core modules responsible for each major stage of building the initial code graph.

#### Major clangd index parsing and processing components
-   **[summary_clangd_index_yaml_parser.md](./summary_clangd_index_yaml_parser.md)**: Explains the high-performance, parallel parsing of the raw `clangd` index file.
-   **[summary_path_processor.md](./summary_path_processor.md)**: Describes the logic for building the file and folder hierarchy in the graph.

#### Major source code parsing and processing components
-   **[summary_compilation_manager.md](./summary_compilation_manager.md)**: Explains the high-level orchestration of source code parsing, caching, and strategy selection.
-   **[summary_compilation_parser.md](./summary_compilation_parser.md)**: Details the low-level parsing logic for extracting function spans an-d include relations.

#### The component that reconciles clangd index data with source-parsed data
-   **[summary_source_span_provider.md](./summary_source_span_provider.md)**: Details the critical process of reconciling `clangd` index data with source-parsed data to establish lexical hierarchies.

#### The component that creates all logical code symbols and their relationships
-   **[summary_clangd_symbol_nodes_builder.md](./summary_clangd_symbol_nodes_builder.md)**: Details the creation of all logical code symbols (functions, classes, etc.) and their relationships.
-   **[summary_include_relation_provider.md](./summary_include_relation_provider.md)**: Covers the logic for ingesting and querying file include relationships.
-   **[summary_clangd_call_graph_builder.md](./summary_clangd_call_graph_builder.md)**: Covers the adaptive strategies for constructing the function call graph.

### RAG Generation Architectural Layers

These documents describe the core components of three-layer RAG generation system. Both RagGenerator and RagUpdater share the same components of three-layer architecture.

-   **[summary_rag_orchestrator.md](./summary_rag_orchestrator.md)**: The Logistics & Validation Layer, responsible for workflow, parallelism, and data validation.
-   **[summary_node_summary_processor.md](./summary_node_summary_processor.md)**: The Logic Layer, which contains the "brain" for processing each node.
-   **[summary_summary_cache_manager.md](./summary_summary_cache_manager.md)**: The Data & Persistence Layer, responsible for cache safety and management.

### Supporting Modules

These documents describe the helper modules that provide essential services like database access, Git integration, and argument parsing.

-   **[summary_neo4j_manager.md](./summary_neo4j_manager.md)**: The Data Access Layer for Neo4j.
-   **[summary_git_manager.md](./summary_git_manager.md)**: The abstraction layer for Git operations.
-   **[summary_llm_client.md](./summary_llm_client.md)**: The factory for providing model-agnostic LLM and embedding clients.
-   **[summary_input_params.md](./summary_input_params.md)**: The centralized module for handling command-line arguments.
-   **[summary_log_manager.md](./summary_log_manager.md)**: Describes the advanced, dual-level logging configuration.
-   **[summary_memory_debugger.md](./summary_memory_debugger.md)**: A simple utility for debugging memory usage.

### Agentic Components

These documents describe components designed to allow AI agents to interact with the generated code graph.

-   **[summary_graph_mcp_server.md](./summary_graph_mcp_server.md)**: An example MCP server that exposes the code graph as a set of tools for an AI agent.
-   **[summary_rag_adk_agent.md](./summary_rag_adk_agent.md)**: An example AI coding agent that uses the MCP server to answer questions about a codebase.

### References

-   **[neo4j_current_schema.txt](./neo4j_current_schema.txt)**: Description on the current schema of the Neo4j database.
-   **[clangd-index-yaml-spec.txt](./clangd-index-yaml-spec.txt)**: Reference information on the `clangd` index format.
-   **[problem_in_yaml_index_Scope_string.txt](./problem_in_yaml_index_Scope_string.txt)**: Description on the problem in the `Scope` field of the `clangd` index format.

### Appendix

-  **[refactor_for_cpp/](./refactor_for_cpp/)**: The outdated documents when I extended the code graph from supporting C-language project to C++-language project.