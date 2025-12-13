# Algorithm Summary: `path_processor.py`

## 1. Role in the Pipeline

This module is responsible for **Pass 3** of the full ingestion pipeline. Its purpose is to build the structural foundation of the project's file system within the Neo4j graph. It discovers all relevant file and folder paths and creates the corresponding `:PROJECT`, `:FOLDER`, and `:FILE` nodes, connecting them with `:CONTAINS` relationships.

This module was created by refactoring the path-related logic out of the larger `clangd_symbol_nodes_builder.py` to improve modularity and separation of concerns.

## 2. Component: `PathManager`

This is a simple but crucial utility class that provides consistent path manipulation logic for the entire application.

*   **Responsibilities**:
    *   **Path Normalization**: Converts `file:///...` URIs into clean, project-relative paths (e.g., `src/main.c`).
    *   **Path Validation**: Provides a reliable way to check if a given absolute path is located inside the project directory.
*   **Benefit**: By centralizing these functions, it ensures that all paths stored in the graph and used in queries are consistent and predictable.

## 3. Component: `PathProcessor`

This is the main orchestrator for discovering and ingesting the file system hierarchy. Its primary method is `ingest_paths`.

### 3.1. Deep Dive: The Path Discovery and Consolidation Logic

*   **Problem**: To build an accurate graph, every single relevant file must be represented by a `:FILE` node. A naive approach of only looking at where symbols are defined (from the `clangd` index) is insufficient. It would miss header files that are included but do not happen to contain any symbol definitions that `clangd` chooses to index, creating "invisible files" and breaking the include graph.

*   **Solution**: The `PathProcessor` uses a robust, two-source discovery process to build a master list of all files.
    1.  **Source 1: Symbols (`_discover_paths_from_symbols`)**: It first iterates through all `Symbol` objects from the `SymbolParser` and extracts all unique file paths from their declaration and definition locations.
    2.  **Source 2: Includes (`_discover_paths_from_includes`)**: It then gets all include relations from the `CompilationManager` (which parsed the source code) and extracts all unique file paths from both the "including" and "included" sides of the relationships.
    3.  **Consolidation**: The final list of project files is the **union** of these two sets.

*   **Benefit**: This two-source approach is a critical design feature. It guarantees that the graph contains a `:FILE` node for every file that is either part of the compilation (from includes) or referenced in the index (from symbols). This solves the "invisible header" problem and provides a complete and solid foundation for the `:INCLUDES` relationships that are built in a later pass.

### 3.2. Deep Dive: The Ingestion Workflow

Once the master file list is created, the `ingest_paths` method orchestrates a clean, multi-step ingestion process.

1.  **Derive Folders**: It first calls `_get_folders_from_files` to derive the set of all unique parent directories from the file paths.
2.  **Ingest Folders**: It calls `_ingest_folder_nodes_and_relationships`.
    *   **Sorting**: The folders are sorted by path depth (shortest first). This ensures that parent folders (e.g., `src/`) are created before their children (e.g., `src/utils/`), which is essential for creating the `:CONTAINS` relationships correctly.
    *   **Two-Phase Write**: It first creates all `:FOLDER` nodes in batches, then creates all `(parent)-[:CONTAINS]->(child)` relationships for the folders in separate batches.
3.  **Ingest Files**: It calls `_ingest_file_nodes_and_relationships`.
    *   **Two-Phase Write**: Similarly, it first creates all `:FILE` nodes in batches, then creates the `(parent)-[:CONTAINS]->(child)` relationships connecting files to their parent folders or the project root.

*   **Benefit**: This "nodes-first, then-relationships" approach, combined with sorting and batching, is an efficient and standard practice for bulk data loading in graph databases. It ensures the hierarchy is built correctly from the top down.
