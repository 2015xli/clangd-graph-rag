# Graph Ingester: Core Construction Logic

This package contains the core "implementation tier" of the ingestion pipeline. It is responsible for transforming raw data from the `SymbolParser` and `SourceParser` into structured nodes and relationships within the Neo4j graph. These components are used by both the `GraphBuilder` (for full builds) and the `GraphUpdater` (for incremental updates).

## Table of Contents
1. [File System Hierarchy (path.py)](#1-file-system-hierarchy-pathpy)
2. [Logical Symbol Ingestion (symbol.py)](#2-logical-symbol-ingestion-symbolpy)
3. [Call Graph Extraction (call.py)](#3-call-graph-extraction-callpy)
4. [Include Relationship Management (include.py)](#4-include-relationship-management-includepy)

---

## 1. File System Hierarchy (path.py)

This module builds the structural foundation of the project within Neo4j. It discovers all relevant file and folder paths and creates the corresponding `:PROJECT`, `:FOLDER`, and `:FILE` nodes, connecting them with `:CONTAINS` relationships.

### Component: `PathManager`
A utility class providing consistent path manipulation logic for the entire application.
*   **Responsibilities**:
    *   **Path Normalization**: Converts `file:///...` URIs into clean, project-relative paths (e.g., `src/main.c`).
    *   **Path Validation**: Checks if a given absolute path is located inside the project directory.
*   **Rationale**: Centralizing these functions ensures that all paths stored in the graph and used in queries are consistent and predictable.

### Component: `PathProcessor`
The main orchestrator for discovering and ingesting the file system hierarchy.

#### Path Discovery and Consolidation Logic
*   **Problem**: A naive approach of only looking at symbol locations from the `clangd` index is insufficient. It would miss header files that are included but contain no indexed symbol definitions, creating "invisible files" and breaking the include graph.
*   **Solution**: `PathProcessor` uses a two-source discovery process:
    1.  **Source 1: Symbols**: Extracts paths from all `Symbol` objects (declarations and definitions).
    2.  **Source 2: Includes**: Extracts paths from all include relations gathered by the `SourceParser`.
    3.  **Consolidation**: The final project file list is the **union** of these sets.
*   **Benefit**: Guaranteed completeness of the `:FILE` graph, solving the "invisible header" problem.

#### Ingestion Workflow
The `ingest_paths` method follows a "nodes-first, then-relationships" approach:
1.  **Derive Folders**: Identifies all unique parent directories.
2.  **Ingest Folders**: Sorts by path depth (shortest first) to ensure parents exist before children, then creates `:FOLDER` nodes and `:CONTAINS` links in batches.
3.  **Ingest Files**: Creates all `:FILE` nodes, then links them to their parents.

---

## 2. Logical Symbol Ingestion (symbol.py)

The `SymbolProcessor` class is responsible for the most complex part of the graph construction: **ingesting logical code symbols and their intricate web of relationships**.

### High-Level Workflow
1.  **Phase 1: Data Preparation**: Processes raw `Symbol` objects, resolves linking information, and groups data for optimized ingestion.
2.  **Phase 2: Node Ingestion**: Creates symbol nodes (`:FUNCTION`, `:CLASS_STRUCTURE`, `:MACRO`, `:TYPE_ALIAS`, etc.).
3.  **Phase 3: Relationship Ingestion**: Establishes links (`:HAS_METHOD`, `:EXPANDED_FROM`, `:ALIAS_OF`, `:DEFINES`, etc.) in an optimized order.

### Key Nuances
*   **Scope Maps**: Before ingestion, it builds a lookup table mapping qualified namespace names (e.g., `std::`) to IDs. This allows string-based `scope` properties to be converted into graph relationships.
*   **De-duplication**: Handles cases where a header is included by both C and C++ files, preferring the `:CLASS_STRUCTURE` label over `:DATA_STRUCTURE` for the same ID.
*   **Causality Tracking**: Captures macro causality by storing `original_name` and linking to `:MACRO` nodes via `:EXPANDED_FROM`.

#### Ingestion Strategies for `:DEFINES`
Creating the `(FILE)-[:DEFINES]->(Symbol)` link for every symbol is a bottleneck. The system supports two strategies:
1.  **`unwind-sequential`**: Simple, safe, and idempotent.
2.  **`isolated-parallel`**: Deadlock-safe parallelism. It groups relationships by source file on the client side. Since each file is handled by only one thread, parallel workers never contend for the same `:FILE` lock.

---

## 3. Call Graph Extraction (call.py)

The `ClangdCallGraphExtractor` identifies function call relationships (`:CALLS`). It automatically selects the best strategy based on the `symbol_parser` capabilities.

### Strategy 1: High-Speed Path (Metadata-based)
Used for modern `clangd` (v21+) providing a `Container` field.
*   **Mechanism**: Directly uses the `container_id` in symbol references to identify the caller.
*   **Benefit**: Extremely fast pure in-memory operation.

### Strategy 2: Legacy Fallback (Spatial-based)
Used for older `clangd` versions.
*   **Mechanism**: Relies on function spans provided by the `SymbolEnricher`. It builds a spatial index (`file_uri` -> `sorted_body_spans`) and maps call site coordinates to function bodies.
*   **Benefit**: Ensures call graph accuracy even without modern index features.

---

## 4. Include Relationship Management (include.py)

The `IncludeRelationProvider` centralizes all logic for `[:INCLUDES]` relationships.

### Dual Role
*   **Full Build**: Ingests the complete include graph. It performs absolute-to-relative path translation and filters out external (system) headers.
*   **Incremental Update**: Provides transitive dependency analysis. It queries the graph (`[:INCLUDES*]`) to find all source files impacted by a header change.

### The "Invisible Header" Problem
A header might not appear in the `clangd` index if the translation unit that defines a function doesn't include its own header. This provider ensures these headers are discovered and linked correctly by using data from the `SourceParser`.

---

## CLI Usage
This package can be accessed via the unified dispatcher:
```bash
# Symbol Ingestion
python3 -m graph_ingester symbol <index.yaml> <project_path>

# Call Graph Ingestion
python3 -m graph_ingester call <index.yaml> <project_path> --ingest

# Include Impact Analysis (Standalone)
python3 -m graph_ingester include <project_path> <header_files...>
```
