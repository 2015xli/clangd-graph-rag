# Algorithm Summary: `neo4j_manager.py`

## 1. Role in the Pipeline

This script is a vital library module that centralizes all interaction with the Neo4j database. It acts as a comprehensive data access layer (DAL), providing a clean, high-level API for all other scripts in the project to use when they need to read from or write to the graph.

It also functions as a standalone command-line tool for performing various database administration tasks, such as inspecting the schema or deleting properties.

## 2. Design and Architecture

The `Neo4jManager` class encapsulates the Neo4j Python driver and manages the connection lifecycle.

*   **Connection Management**: It is designed to be used as a context manager (`with Neo4jManager() as neo4j_mgr:`), which ensures that the database connection is automatically opened and closed safely.
*   **Core Methods**: It provides a set of methods that abstract away the specifics of running Cypher queries and handling transactions. Key methods include:
    *   `reset_database()`: Clears the entire database.
    *   `create_constraints()`: Sets up uniqueness constraints for key node labels.
    *   `execute_read_query()`: For running `MATCH` queries and returning results.
    *   `execute_autocommit_query()`: For running simple, single write queries.
    *   `process_batch()`: For executing a list of queries within a single transaction for performance.

## 3. Key Features and Subtleties

Beyond basic query execution, the manager has several important features.

### Database Purging Logic

The manager contains specific, non-trivial logic required by the incremental updater (`clangd_graph_rag_updater.py`).

*   **`purge_symbols_defined_in_files()`**: This takes a list of file paths and runs a query to find and `DETACH DELETE` all symbols that are defined in those files. This is crucial for clearing out old symbol nodes when a file is modified.
*   **`purge_files()`**: This method is more complex. It first deletes all specified `:FILE` nodes. Then, it enters a loop to iteratively find and delete any `:FOLDER` nodes that have become empty as a result of the file deletions. This ensures that no empty, orphaned folder structures are left behind in the graph.

### Schema and Index Management

The manager provides helpers for managing the graph's schema and vector indexes.

*   **`get_schema()`**: Uses the APOC library (`apoc.meta.graph` and `apoc.meta.schema`) to introspect the database and return a structured representation of all node labels, properties, and relationships.
*   **`create_vector_indexes()`**: Executes the Cypher commands to create the vector indexes required for semantic search on the `summaryEmbedding` property. It is designed to fail gracefully if the installed version of Neo4j does not support vector indexes (e.g., Community Edition).
*   **`delete_property()`**: A powerful helper function that can remove a specific property (e.g., `summaryEmbedding`) from all nodes of a certain label, or from all nodes in the entire graph.

## 4. Standalone CLI Tool

When run as a script, `neo4j_manager.py` provides a command-line interface for database administration.

*   **`dump-schema`**: Uses `get_schema()` to fetch and print a formatted, human-readable view of the graph schema, including node properties and relationships.
*   **`delete-property`**: Exposes the `delete_property` method to the command line, allowing an administrator to easily clean up data. For example, it can be used to delete all embeddings to force them to be regenerated on the next RAG run.
*   **`dump-schema-types`**: A debugging tool to inspect the raw Python types of the data returned by the schema introspection queries.
