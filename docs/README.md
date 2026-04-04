# C/C++ Source Code GraphRAG: Design and Architecture

This directory is the central portal for the project's technical documentation. It is designed for developers and contributors who want to understand the design principles, internal strategies, and modular architecture of the code graph system.

## Documentation Map

### 1. High-Level Overview
*   **[Building an AI-Ready Code Graph RAG](./Building_an_AI-Ready_Code_Graph_RAG_based_on_Clangd_index.md)**: The foundational "Main Presentation" document. Start here for high-level concepts, slide-by-slide pipeline breakdowns, and performance rationales.

### 2. End-to-End Orchestration
*   **[graph_builder.md](./graph_builder.md)**: Describes the main pipeline for building the code graph from scratch.
*   **[graph_updater.md](./graph_updater.md)**: Describes the incremental update pipeline for the code graph based on Git changes.

### 3. Modular Component Guides
These READMEs reside within the package folders to provide locality of information for developers working on the code.

#### Symbols Construction
*   **[Symbol Parser](../symbol_parser.md)**: The foundational library for the entire ingestion pipeline. It efficiently transforms a massive `clangd` YAML index into a fully-linked, in-memory graph of Python objects.
*   **[Source Parser](../source_parser/README.md)**: The "Ground Truth" engine that extracts symbol definitions and relations, and header file include relations from the source code via `libclang`.
*   **[Symbol Enricher](../symbol_enricher/README.md)**: The semantic bridge that reconciles the clangd index symbols (from `Symbol Parser`) with physical source code information (from `Source Parser`). It also enriches with additional symbols and relationships such as Macros, TypeAlias, and static call relationships, etc.

#### Graph Construction
*   **[Graph Ingester](../graph_ingester/README.md)**: The process that converts the symbols and relations to Neo4j nodes and relationships.
*   **[Updater Engine](../updater_engine/README.md)**: The brain behind the [Graph Updater](./graph_updater.md)'s "Sufficient Subset" strategy for surgical incremental updates.

#### AI Enrichment (RAG)
*   **[Summary Driver](../summary_driver/README.md)**: Orchestrates the multi-pass workflows for full and incremental RAG summary generation.
*   **[Summary Engine](../summary_engine/README.md)**: The core intelligence layer for LLM-powered summarization and multi-level caching.
*   **[LLM Client](./llm_client.md)**: The unified interface for interacting with various LLM and embedding model APIs.

#### Support and Infrastructure
*   **[Neo4j Manager](../neo4j_manager/README.md)**: The Data Access Layer (DAL) and database maintenance tools.
*   **[Infrastructure Services](./infrastructure.md)**: Shared modules for Git, logging, LLM clients, and memory debugging.

### 4. Integration and Agents
*   **[Example MCP Server](./graph_mcp_server.md)**: Technical details on the MCP Tool Server.
*   **[Example Coding Agent](../rag_adk_agent/README.md)**: An example expert coding agent leveraging the code graph.

### 5. Reference and Deep Dives
*   **[Technical Reference](./reference/README.md)**: Provide very detailed schema definitions, index specifications, and detailed problem analyses.

---

## 🏗️ Appendix: Historical Reference
*   **[refactor_for_cpp/](./refactor_for_cpp/)**: Archive of documents from the C-to-C++ extension phase.
*   **[design_for_macro/](./design_for_macro/)**: Architectural plans for preprocessor support.
*   **[design_for_typealias/](./design_for_typealias/)**: Architectural plans for type alias resolution.
