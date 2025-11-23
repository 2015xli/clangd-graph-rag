# Algorithm Summary: `clangd_symbol_nodes_builder.py`

## 1. Role in the Pipeline

This script is responsible for **Passes 3 and 4** of the full ingestion pipeline (after symbols have been parsed and enriched by `SourceSpanProvider`). Its purpose is to build the structural foundation of the code graph in Neo4j. It creates:

*   The physical file system hierarchy (`:PROJECT`, `:FOLDER`, `:FILE`).
*   The logical code symbols (`:NAMESPACE`, `:FUNCTION`, `:METHOD`, `:CLASS_STRUCTURE`, `:DATA_STRUCTURE`, `:FIELD`, `:VARIABLE`).
*   All the crucial relationships connecting these nodes, such as `:CONTAINS`, `:DEFINES`, `:HAS_NESTED`, `:SCOPE_CONTAINS`, `:HAS_METHOD`, `:HAS_FIELD`, `:INHERITS`, and `:OVERRIDDEN_BY`.

It operates on the in-memory collection of enriched `Symbol` objects provided by the `clangd_index_yaml_parser` (after `SourceSpanProvider` has added `body_location` and `parent_id`).

## 2. Standalone Usage

The script can be run directly to perform a partial ingestion of file structure and symbol definitions, which is useful for debugging.

```bash
# Example: Ingest symbols using the default batched-parallel strategy
python3 clangd_symbol_nodes_builder.py /path/to/index.yaml /path/to/project/
```

**All Options:**

*   `index_file`: Path to the clangd index YAML file (or a `.pkl` cache file).
*   `project_path`: Root path of the project.
*   `--defines-generation`: Strategy for ingesting `:DEFINES` relationships (`unwind-sequential`, `isolated-parallel`, `batched-parallel`). Default: `batched-parallel`.
*   ... and other performance tuning arguments (`--num-parse-workers`, `--cypher-tx-size`, etc.).

---
*The following sections describe the library's internal logic.*

## 3. Path Management (`PathManager`)

The `PathManager` class provides utility functions for handling file paths consistently across the project:

*   `uri_to_relative_path(uri: str)`: Converts a file URI (e.g., `file:///path/to/project/src/file.cpp`) to a path relative to the project root (e.g., `src/file.cpp`).
*   `is_within_project(path: str)`: Checks if a given absolute path falls within the defined project root.

These utilities ensure that all paths stored in the graph are relative to the project root, which is crucial for graph operations and consistency.

## 4. Pass 3: Ingesting File & Folder Structure (`PathProcessor`)

This pass builds the graph representation of the physical file system hierarchy.

*   **Algorithm**:
    1.  **Path Discovery**: The `PathProcessor` consolidates all unique file paths from two sources:
        *   `_discover_paths_from_symbols()`: Iterates through every `Symbol` object's declaration and definition locations to find all files referenced by symbols within the project.
        *   `_discover_paths_from_includes()`: Retrieves all files involved in `#include` relationships from the `CompilationManager`.
        From this combined set of unique file paths, it then derives all unique parent folder paths using `_get_folders_from_files()`, ensuring the entire directory tree is captured.
    2.  **Batched Ingestion of Folders**:
        *   `_ingest_folder_nodes_and_relationships()`: Creates `:FOLDER` nodes for each unique folder path. Folders are sorted by depth to ensure parent folders are created before their children.
        *   It then creates `[:CONTAINS]` relationships:
            *   From the `:PROJECT` node to top-level `:FOLDER`s.
            *   From parent `:FOLDER`s to child `:FOLDER`s.
        This process uses efficient, batched Cypher `MERGE` queries.
    3.  **Batched Ingestion of Files**:
        *   `_ingest_file_nodes_and_relationships()`: Creates `:FILE` nodes for each unique file path.
        *   It then creates `[:CONTAINS]` relationships:
            *   From the `:PROJECT` node to top-level `:FILE`s.
            *   From parent `:FOLDER`s to child `:FILE`s.
        This also uses efficient, batched Cypher `MERGE` queries.

## 5. Pass 4: Ingesting Symbols and Relationships (`SymbolProcessor`)

This pass populates the graph with logical code constructs (symbols) and their interconnections.

### 5.1. Symbol Data Preparation

The `SymbolProcessor` first prepares the raw `Symbol` objects (enriched by `SourceSpanProvider`) for ingestion:

1.  **`_build_scope_maps()`**: Creates a lookup table (`qualified_namespace_to_id`) mapping fully qualified namespace names (e.g., `std::chrono::`) to their corresponding `Symbol.id`. This is used to link symbols to their containing namespaces.
2.  **`process_symbol(sym: Symbol, ...)`**: This crucial method transforms a raw `Symbol` object into a dictionary of properties suitable for Neo4j.
    *   **Filtering**: It filters out symbols not within the project path, with the exception of `NAMESPACE` symbols, which can be external but still relevant for scope.
    *   **Property Mapping**: It extracts common properties like `id`, `name`, `kind`, `scope`, `language`, `has_definition`.
    *   **Path Conversion**: Converts `file_uri` to `file_path` (relative to project root).
    *   **Location Data**: Populates `name_location` and `body_location` (if available from `SourceSpanProvider`).
    *   **Parent IDs**: Copies `parent_id` (lexical parent from `SourceSpanProvider`) and `namespace_id` (semantic parent from `_build_scope_maps`) if present on the `Symbol` object.
    *   **Node Label Assignment**: This is where `clangd`'s `sym.kind` is mapped to the appropriate Neo4j node label based on the refined C++ schema:
        *   `Namespace` -> `:NAMESPACE` (with `qualified_name`)
        *   `Function` -> `:FUNCTION`
        *   `InstanceMethod`, `StaticMethod`, `Constructor`, `Destructor`, `ConversionFunction` -> `:METHOD`
        *   `Class` -> `:CLASS_STRUCTURE`
        *   `Struct` -> `:CLASS_STRUCTURE` (if C++) or `:DATA_STRUCTURE` (if C)
        *   `Union`, `Enum` -> `:DATA_STRUCTURE`
        *   `Field`, `StaticProperty`, `EnumConstant` -> `:FIELD` (with `is_static` property)
        *   `Variable` -> `:VARIABLE`
    *   **Specific Properties**: Adds `signature`, `return_type`, `type` for functions/methods/variables, and `is_static` for fields.
3.  **`_process_and_filter_symbols()`**: Groups all processed symbol data dictionaries by their assigned `node_label` (e.g., all `FUNCTION` data in one list, all `CLASS_STRUCTURE` data in another).

### 5.2. Node Ingestion

After preparation, the `ingest_symbols_and_relationships()` method orchestrates the creation of all symbol nodes in batches:

*   `_ingest_namespace_nodes()`: Creates `:NAMESPACE` nodes.
*   `_ingest_data_structure_nodes()`: Creates `:DATA_STRUCTURE` nodes.
*   `_ingest_class_nodes()`: Creates `:CLASS_STRUCTURE` nodes.
*   `_ingest_function_nodes()`: Creates `:FUNCTION` nodes.
*   `_ingest_method_nodes()`: Creates `:METHOD` nodes. Note that `parent_id` is removed from properties using `apoc.map.removeKey` before setting, as it's used for relationship creation, not as a node property.
*   `_ingest_field_nodes()`: Creates `:FIELD` nodes. Similar to methods, `parent_id` is removed.
*   `_ingest_variable_nodes()`: Creates `:VARIABLE` nodes.

All node ingestion methods use batched `MERGE` queries for efficiency.

### 5.3. Relationship Ingestion

This is the most complex phase, where all inter-symbol relationships are created:

1.  **`id_to_label_map`**: A quick in-memory lookup is built to map every symbol's ID to its Neo4j node label, enabling efficient filtering and dynamic Cypher query construction.
2.  **`SCOPE_CONTAINS` Relationships**:
    *   `_ingest_scope_contains_relationships()`: Creates `(parent:NAMESPACE)-[:SCOPE_CONTAINS]->(child)` relationships. These represent the logical containment of symbols (and nested namespaces) within namespaces.
3.  **`HAS_NESTED` Relationships**:
    *   `_ingest_nesting_relationships()`: Creates `(parent)-[:HAS_NESTED]->(child)` relationships. These represent the lexical nesting of structures (classes, structs, functions) as determined by `SourceSpanProvider`. Relationships are pre-grouped by `(parent_label, child_label)` combinations, allowing for highly optimized Cypher queries that specify node labels directly in the `MATCH` clause.
4.  **`FILE-[:DECLARES]->NAMESPACE` Relationships**:
    *   `_ingest_file_declarations()`: Links `:FILE` nodes to the `:NAMESPACE` nodes they contribute to.
5.  **The `:DEFINES` Relationship Challenge**:
    Creating `(f:FILE)-[:DEFINES]->(s:Symbol)` relationships (linking a file to the symbols it defines) is a major performance challenge due to the sheer volume. The script offers three strategies via the `--defines-generation` flag:
    *   **`batched-parallel` (Default)**:
        *   **Algorithm**: Uses `apoc.periodic.iterate` with `parallel: false` (due to potential deadlocks on file nodes if `parallel: true` is used without careful locking). This means it effectively runs sequentially but still benefits from APOC's batching within a single transaction.
        *   **Use Case**: Generally a good balance for performance and safety on clean builds.
    *   **`isolated-parallel` (Idempotent & Deadlock-Safe)**:
        *   **Algorithm**: Relationships are first grouped by their target `:FILE` node on the client side. These groups are then passed to `apoc.periodic.iterate` with `parallel: true`. Since all relationships for a given file are processed within a single group, no two parallel threads will contend for the same file node, eliminating deadlocks.
        *   **Use Case**: Safest for incremental updates or when there's a high risk of contention.
    *   **`unwind-sequential`**:
        *   **Algorithm**: A simple, idempotent, and sequential strategy using client-side batching with `UNWIND` and `MERGE`. It does not use the APOC library.
        *   **Use Case**: Extremely safe, easy to debug, and does not require APOC. Performance is comparable to `batched-parallel` when node labels are specified in the `MATCH` clause.
6.  **`HAS_FIELD` Relationships**:
    *   `_ingest_has_field_relationships()`: Creates `(parent:DATA_STRUCTURE|CLASS_STRUCTURE)-[:HAS_FIELD]->(child:FIELD)` relationships.
7.  **`HAS_METHOD` Relationships**:
    *   `_ingest_has_method_relationships()`: Creates `(parent:CLASS_STRUCTURE)-[:HAS_METHOD]->(child:METHOD)` relationships.
8.  **`INHERITS` Relationships**:
    *   `_ingest_inheritance_relationships()`: Creates `(child:CLASS_STRUCTURE)-[:INHERITS]->(parent:CLASS_STRUCTURE)` relationships based on data from `SymbolParser`.
9.  **`OVERRIDDEN_BY` Relationships**:
    *   `_ingest_override_relationships()`: Creates `(base_method:METHOD)-[:OVERRIDDEN_BY]->(derived_method:METHOD)` relationships based on data from `SymbolParser`.

All relationship ingestion methods utilize batched Cypher queries for optimal performance. 
