# Neo4j Manager: Data Access and Schema Management

The `neo4j_manager` package is the project's Data Access Layer (DAL). It centralizes all interactions with the Neo4j database, providing a high-level API for graph construction, querying, and maintenance.

## Table of Contents
1. [Core Architecture (base.py)](#1-core-architecture-basepy)
2. [Schema and Indexing (schema.py)](#2-schema-and-indexing-schemapy)
3. [Data Purging Logic (purge.py)](#3-data-purging-logic-purgepy)
4. [Project Metadata (project.py)](#4-project-metadata-projectpy)

---

## 1. Core Architecture (base.py)

The `Neo4jManager` unified class inherits from several specialized mixins. It is designed to be used as a context manager:
```python
with Neo4jManager() as neo4j_mgr:
    neo4j_mgr.execute_read_query(...)
```
This ensures the driver connection lifecycle is managed safely.

---

## 2. Schema and Indexing (schema.py)

This mixin manages the graph's structural definition and specialized RAG indexing.

### Agent-Facing Schema
To make the raw graph more accessible to AI agents, the manager can transform the schema:
*   **`:ENTITY` Label**: Adds a generic label to all relevant nodes (Functions, Classes, Files, etc.), allowing broad semantic searches across all types.
*   **Unified Vector Index**: Creates a single vector index on the `summaryEmbedding` property of the `:ENTITY` label.
*   **Synthetic IDs**: Ensures nodes that naturally use paths (like `FILE`) also have a standardized `id` property.

---

## 3. Data Purging Logic (purge.py)

Crucial for the `GraphUpdater`, this mixin provides surgical deletion methods:
*   **`purge_nodes_by_id`**: Resolves identity collisions. If a symbol migrates between files, this method removes the stale version.
*   **`purge_nodes_by_path`**: Removes all symbols and declarations associated with a specific file.
*   **`purge_files`**: Deletes `:FILE` nodes and iteratively removes any `:FOLDER` nodes that become empty, keeping the hierarchy clean.

---

## 4. Project Metadata (project.py)

Manages the `:PROJECT` root node, which stores the project's absolute path and the current Git `commit_hash` represented in the graph. This is used to verify that the graph matches the source code before an update begins.

---

## CLI Usage
Perform database maintenance from the command line:
```bash
# View human-readable schema
python3 -m neo4j_manager dump-schema

# Delete a property across the graph (e.g., to force RAG regeneration)
python3 -m neo4j_manager delete-property --key summary --all-labels
```
