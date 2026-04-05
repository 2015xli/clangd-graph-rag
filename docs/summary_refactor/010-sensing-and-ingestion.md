# 010-sensing-and-ingestion.md

**Goal**: Capture C++ template specialization metadata and link specialized structures (including "synthetic" nodes) to their blueprints via the `[:SPECIALIZATION_OF]` relationship.

#### **1. Data Structure Updates**
*   **`source_parser/types.py` (`SourceSpan`)**:
    *   `primary_template_id: Optional[str]` (Hashed USR of the blueprint).
    *   `template_specialization_args: Optional[str]` (e.g., `<int>`).
    *   `is_synthetic: bool` (Flag for phony nodes that share the template's extent).
*   **`symbol_parser.py` (`Symbol`)**:
    *   Added matching fields: `primary_template_id`, `template_specialization_args`, and `is_synthetic`.

#### **2. Source Sensing Implementation (`source_parser`)**
*   **Recursive Parent Resolution (`worker.py`)**:
    *   In `_get_parent_id`, if a `semantic_parent` cursor has a valid USR but its ID is missing from `span_results`, the parser recurses by calling `_process_generic_node(parent)`. This ensures structural parents are always captured, even if parsed out-of-order.
*   **Template Metadata Extraction (`node_parser.py`)**:
    *   **Helper**: `_extract_template_metadata(node)`
        1. Calls `clang_getSpecializedCursorTemplate(node)`.
        2. Hashes the blueprint's USR to set `primary_template_id`.
        3. Parses `node.displayname` to extract the specialization string (e.g., `<int>`).
        4. **Synthetic Detection**: Compares the `extent` (start/end lines and columns) of the current node with the primary template cursor. If identical, sets `is_synthetic = True`.
*   **Scope Filtering**: Specializations of name `CLASS_TEMPLATE` are skipped for metadata extraction to avoid self-referencing blueprints.

#### **3. Semantic Enrichment (`symbol_enricher`)**
*   **Propagation**: `MatcherMixin` propagates `primary_template_id`, `template_specialization_args`, and `is_synthetic` from `SourceSpan` to `Symbol` across all three matching tiers (ID, Location, Context).
*   **Synthesis**: `HelpersMixin._create_synthetic_symbol` populates these fields for symbols discovered solely through implementation spans.

#### **4. Graph Construction (`graph_ingester`)**
*   **Property Mapping (`SymbolProcessor.process_symbol`)**:
    *   Strictly for `CLASS_STRUCTURE` nodes:
        *   `template_params`: Mapped from `sym.signature`.
        *   `specialization_args`: Mapped from `sym.template_specialization_args`.
        *   `primary_template_id`: Mapped from `sym.primary_template_id`.
        *   `is_synthetic`: Mapped from `sym.is_synthetic`.
*   **Node Ingestion**: Update the `SET` clause to include these properties while ensuring `primary_template_id` is removed from the direct node properties (it is for relationship use only).
*   **Relationship Ingestion (`_ingest_specialization_relationships`)**:
    *   Creates **`[:SPECIALIZATION_OF]`** edges between specialized classes and their blueprints.
    *   *Cypher*:
        ```cypher
        UNWIND $data AS d
        MATCH (spec:CLASS_STRUCTURE {id: d.id}) 
        MATCH (blue:CLASS_STRUCTURE {id: d.primary_template_id})
        MERGE (spec)-[:SPECIALIZATION_OF]->(blue)
        ```
