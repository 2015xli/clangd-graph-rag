# Algorithm Summary: `graph_builder.py`

## 1. Role in the Pipeline

This script is the **main orchestrator** for the entire code graph ingestion and RAG generation process. It acts as the entry point that ties all the other library modules together, executing a series of sequential passes to build a complete and enriched code graph in Neo4j from a `clangd` index file.

It is designed to be run from the command line and provides numerous options for performance tuning and for controlling optional stages like RAG data generation.

## 2. Execution Flow

The script executes a strict, sequential pipeline. The architecture has been significantly refactored to be more robust, modular, and memory-efficient.

### Pre-Database Passes

These passes prepare all data in memory before connecting to the database.

*   **Pass 0: Parse Clangd Index**
    *   **Component**: `symbol_parser.SymbolParser`
    *   **Purpose**: To parse the massive `clangd` index YAML file into an in-memory collection of `Symbol` objects. This provides the first source of file paths (from symbol locations).

*   **Pass 1: Parse Source Code**
    *   **Component**: `source_parser.CompilationManager`
    *   **Purpose**: To parse the entire project's source code. This provides: include relationships, body locations for functions, **Type Alias** definitions, and ground-truth **Macro** definitions.

*   **Pass 2: Enrich Symbols with Spans**
    *   **Component**: `symbol_enricher.SymbolEnricher`
    *   **Purpose**: Matches indexed symbols with parsed source data. Attaches `body_location` and **macro causality metadata** (`original_name`, `expanded_from_id`) to symbols. It also **discovers and injects new Symbols** for macros and missing type aliases.

### Database Passes

With all data prepared, the orchestrator now connects to Neo4j and builds the graph.

*   **Database Initialization**: The database is completely reset, and constraints and indexes are created for performance.

*   **Pass 3: Ingest File Hierarchy**
    *   **Component**: `graph_ingester.PathProcessor`
    *   **Purpose**: To create all `:FILE` and `:FOLDER` nodes and their `[:CONTAINS]` relationships.
    *   **Design Subtlety**: This pass is now highly robust. It receives data from both the symbol parser and the compilation manager, consolidating a master list of every file path that must exist. This ensures that even "invisible headers" (headers with no symbol definitions) are correctly created as nodes in the graph.

*   **Pass 4: Ingest Symbols and Relationships**
    *   **Component**: `graph_ingester.SymbolProcessor`
    *   **Purpose**: To create all symbol nodes (`:FUNCTION`, `:DATA_STRUCTURE`, `:CLASS_STRUCTURE`, **`:TYPE_ALIAS`**, **`:MACRO`**).
    *   **Key Feature**: Ingests expansion causality (`[:EXPANDED_FROM]`) and type relationships (`[:ALIAS_OF]`). Stores macro source text in the `macro_definition` property.

*   **Pass 5: Ingest Include Relations**
    *   **Component**: `graph_ingester.IncludeRelationProvider`
    *   **Purpose**: To create all `(:FILE)-[:INCLUDES]->(:FILE)` relationships. Because Pass 3 guarantees all file nodes already exist, this can be done safely and efficiently.

*   **Pass 6: Ingest Call Graph**
    *   **Component**: `graph_ingester.ClangdCallGraphExtractor`
    *   **Purpose**: To create all function `[:CALLS]` relationships.

*   **Pass 7: Cleanup Orphan Nodes**
    *   **Component**: `neo4j_manager.Neo4jManager`
    *   **Purpose**: Removes nodes created but ended up with no relationships. (Should be zero in normal operation)

*   **Pass 8: RAG Data Generation (Optional)**
    *   **Component**: `summary_driver.FullSummarizer`
    *   **Purpose**: To enrich the graph with AI-generated summaries and embeddings.

*   **Pass 9: Add Agent-Facing Schema**
    *   **Component**: `neo4j_manager.Neo4jManager`
    *   **Purpose**: Adds synthetic IDs, the `:ENTITY` label to all relevant nodes, and creates unified vector indexes to facilitate agent reasoning and semantic search.

## 3. Memory Management

The orchestrator is designed to be memory-conscious. After the call graph is built in Pass 6, the large `SymbolParser` object is no longer needed by any subsequent pass. The script explicitly deletes it and invokes the garbage collector to free up gigabytes of memory before the potentially memory-intensive RAG generation pass begins.
