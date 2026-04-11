# 030-class-summarization-logic.md

**Goal**: Implement physical staleness tracking for all `CLASS_STRUCTURE` nodes and transform their summaries into "Structural Manifests" that combine definition code with semantic member inventories.

#### **1. Data Retrieval Update (`summary_engine/scope_processor.py`)**
*   **Method**: `_process_one_class_summary`
*   **Action**: Ensure `n.code_hash` is fetched from Neo4j along with `kind`, `original_name`, `is_synthetic`, etc.

#### **2. Physical Staleness Tracking (`summary_engine/node_summarizer.py`)**
*   **Method**: `get_class_summary`
*   **Logic**:
    1.  **Calculate Current Hash**:
        *   If `body_location` is present: Read source text, `current_hash = SHA1(code)`.
        *   If no body (macro application or declaration): `current_hash = node_id`.
    2.  **Verify Staleness**:
        *   Compare `current_hash` with `db_code_hash` (from Neo4j) and `cache_code_hash` (from L1).
        *   If any hash differs OR if context (parents/methods) has changed, the node is stale.
    3.  **Return New State**: Return `current_hash` in the results dictionary.

#### **3. Database Synchronization (`summary_engine/scope_processor.py`)**
*   **Action**: Update the Neo4j `SET` query to persist both `n.summary` and `n.code_hash`.

#### **4. Conditional Context Building (`summary_engine/node_summarizer.py`)**
*   **Origin Selection**:
    *   **Macro**: Use `original_name` description.
    *   **Synthetic**: Use specialized container description.
    *   **Standard**: Use literal code block.
*   **Member Aggregation**: Combine member summaries/types into the structural inventory.

#### **5. Composite Prompting (`summary_engine/prompts.py`)**
*   **Template**: `CLASS_SUMMARY_MANIFEST_TEMPLATE`
*   **Structure**: {Kind} {Name} {TemplateMetadata} + {Definition/Origin} + {Member Inventory}.
