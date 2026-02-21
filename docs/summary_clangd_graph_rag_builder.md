# Algorithm Summary: `clangd_graph_rag_builder.py`

## 1. Role in the Pipeline

This script is the **main orchestrator** for the entire code graph ingestion and RAG generation process. It acts as the entry point that ties all the other library modules together, executing a series of sequential passes to build a complete and enriched code graph in Neo4j from a `clangd` index file.

It is designed to be run from the command line and provides numerous options for performance tuning and for controlling optional stages like RAG data generation.

## 2. Execution Flow: The Refactored Multi-Pass Pipeline

The script executes a strict, sequential pipeline. The architecture has been significantly refactored to be more robust, modular, and memory-efficient.

### Pre-Database Passes

These passes prepare all data in memory before connecting to the database.

*   **Pass 0: Parse Clangd Index**
    *   **Component**: `clangd_index_yaml_parser.SymbolParser`
    *   **Purpose**: To parse the massive `clangd` index YAML file into an in-memory collection of `Symbol` objects. This provides the first source of file paths (from symbol locations).

*   **Pass 1: Parse Source Code**
    *   **Component**: `compilation_manager.CompilationManager`
    *   **Purpose**: To parse the entire project's source code. This provides: include relationships, body locations for functions, **Type Alias** definitions, and ground-truth **Macro** definitions.

*   **Pass 2: Enrich Symbols with Spans**
    *   **Component**: `source_span_provider.SourceSpanProvider`
    *   **Purpose**: Matches indexed symbols with parsed source data. Attaches `body_location` and **macro causality metadata** (`original_name`, `expanded_from_id`) to symbols. It also **discovers and injects new Symbols** for macros and missing type aliases.

### Database Passes

With all data prepared, the orchestrator now connects to Neo4j and builds the graph.

*   **Database Initialization**: The database is completely reset, and constraints and indexes are created for performance.

*   **Pass 3: Ingest File Hierarchy**
    *   **Component**: `clangd_symbol_nodes_builder.PathProcessor`
    *   **Purpose**: To create all `:FILE` and `:FOLDER` nodes and their `[:CONTAINS]` relationships.
    *   **Design Subtlety**: This pass is now highly robust. It receives data from both the symbol parser and the compilation manager, consolidating a master list of every file path that must exist. This ensures that even "invisible headers" (headers with no symbol definitions) are correctly created as nodes in the graph.

*   **Pass 4: Ingest Symbols and Relationships**
    *   **Component**: `clangd_symbol_nodes_builder.SymbolProcessor`
    *   **Purpose**: To create all symbol nodes (`:FUNCTION`, `:DATA_STRUCTURE`, `:CLASS_STRUCTURE`, **`:TYPE_ALIAS`**, **`:MACRO`**).
    *   **Key Feature**: Ingests expansion causality (`[:EXPANDED_FROM]`) and type relationships (`[:ALIAS_OF]`). Stores macro source text in the `macro_definition` property.

*   **Pass 5: Ingest Include Relations**
    *   **Component**: `include_relation_provider.IncludeRelationProvider`
    *   **Purpose**: To create all `(:FILE)-[:INCLUDES]->(:FILE)` relationships. Because Pass 3 guarantees all file nodes already exist, this can be done safely and efficiently.

*   **Pass 6: Ingest Call Graph**
    *   **Component**: `clangd_call_graph_builder.ClangdCallGraphExtractor`
    *   **Purpose**: To create all function `[:CALLS]` relationships.

*   **Pass 7: Cleanup Orphan Nodes**
    *   **Component**: `neo4j_manager.Neo4jManager`
    *   **Purpose**: Removes nodes created but ended up with no relationships.

*   **Pass 8: RAG Data Generation (Optional)**
    *   **Component**: `code_graph_rag_generator.RagGenerator`
    *   **Purpose**: To enrich the graph with AI-generated summaries and embeddings.

*   **Pass 9: Add Agent-Facing Schema**
    *   **Component**: `neo4j_manager.Neo4jManager`
    *   **Purpose**: Adds synthetic IDs, the `:ENTITY` label to all relevant nodes, and creates unified vector indexes to facilitate agent reasoning and semantic search.

## 3. Memory Management

The orchestrator is designed to be memory-conscious. After the call graph is built in Pass 6, the large `SymbolParser` object is no longer needed by any subsequent pass. The script explicitly deletes it and invokes the garbage collector to free up gigabytes of memory before the potentially memory-intensive RAG generation pass begins.
