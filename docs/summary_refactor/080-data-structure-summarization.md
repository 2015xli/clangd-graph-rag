# 080-data-structure-summarization.md

**Goal**: Extend the summarization engine to cover `DATA_STRUCTURE` nodes (Enums, Unions, and C-style Structs) using a unified structural manifest approach.

---

### **Step 1: Unified Structural Pass (`summarize_class_structures`)**
**Operation**: Query for both `CLASS_STRUCTURE` and `DATA_STRUCTURE` nodes.
**Query**:
```cypher
MATCH (c) WHERE c:CLASS_STRUCTURE OR c:DATA_STRUCTURE RETURN c.id AS id
```
**Rationale**: In the updated pipeline, these labels are treated as "Structural Entities" that share the same manifest-based logic.

---

### **Step 2: Label-Agnostic Processor (`scope_processor.py`)**
**Operation**: Update the DAG level and worker logic to support dual labels.
1.  **Level Logic**: DATA_STRUCTURE nodes (which lack inheritance) are assigned to **Level 0**.
2.  **Context Query**: Matches `(c:CLASS_STRUCTURE|DATA_STRUCTURE)` to fetch member fields (`HAS_FIELD`) and their types.
3.  **Persistence**: The database `SET` query dynamically identifies the correct label (`node_label`) from the results to ensure accurate synchronization.

---

### **Step 3: Data-Centric Manifests (`node_summarizer.py`)**
**Operation**: Generate summaries focused on data organization.
1.  **Physical Hashing**: Enums and Structs calculate their `code_hash` from the `body_location`.
2.  **Field Inventory**: The prompt emphasizes the list of constants (Enums) or members (Unions/Structs).
3.  **Incremental support**: Update `_find_classes_of_symbol_ids` and related discovery methods in `incremental_summarizer.py` to use the `|` Cypher operator for dual-label matching.

---

### **Step 4: Summary of Benefits**
*   **Architectural Clarity**: C-style data definitions are now fully represented in File and Folder manifests.
*   **Searchability**: Enums and Unions now have vector embeddings, enabling semantic search for system states and data layouts.
