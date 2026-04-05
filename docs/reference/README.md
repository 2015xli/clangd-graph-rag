# Reference: Specifications and Deep Dives

This directory contains technical specifications, database schema definitions, and detailed design documents for specific subsystems.

## Table of Contents

### Database Schema
*   **[neo4j_simplified_schema.txt](./neo4j_simplified_schema.txt)**: A simplified, agent-readable overview of node labels, properties, and relationship types.
*   **[neo4j_current_schema.txt](./neo4j_current_schema.txt)**: The complete, raw schema introspection including all property keys.
*   **[neo4j_current_schema.png](./neo4j_current_schema.png)**: A visual visualization of the graph data model.

### Clangd Index Specifications
*   **[clangd-index-yaml-spec.txt](./clangd-index-yaml-spec.txt)**: Reference information on the raw `!Symbol`, `!Refs`, and `!Relations` documents found in the `clangd` index format.
*   **[problem_in_yaml_index_Scope_string.txt](./problem_in_yaml_index_Scope_string.txt)**: A technical analysis of inconsistencies in the `clangd` scope field and our strategies for resolving them.

### Clang AST Specifications
*   **[primary-template-relationships-in-Clang-AST.md](./primary-template-relationships-in-Clang-AST.md)**: Technical explanation of the primary template and specialization relationship in Clang AST.

### Deep Dive Designs
*   **[issue_of_symbol_collision_in_incremental_update.md](./issue_of_symbol_collision_in_incremental_update.md)**: A comprehensive analysis of the "Identity Migration" problem and the surgical aggregation strategy used by the `GraphUpdater`.
*   **[rag_generation_design.md](./rag_generation_design.md)**: Details the original architectural vision for the multi-pass RAG summarization system.
