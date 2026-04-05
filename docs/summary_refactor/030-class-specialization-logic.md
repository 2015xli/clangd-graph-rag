# 030-class-specialization-logic.md

**Goal**: Accurately summarize template specializations (and phony classes) by using their primary blueprint's context.

#### **1. Hierarchy Update**
*   **`summary_engine/scope_processor.py`**:
    *   In `_process_one_class_summary`, add an `OPTIONAL MATCH (c)-[:SPECIALIZATION_OF]->(b:CLASS_STRUCTURE)`.
    *   Collect the blueprint's `summary` and `template_params`.

#### **2. Node Processor Enrichment**
*   **`summary_engine/node_summarizer.py`**:
    *   In `get_class_summary`, accept `blueprint_summary` and `is_synthetic` as optional parameters.
    *   Update the `class_name` to include its `specialization_args`.

#### **3. Summarization Strategy**
*   **Prompt Modification**: If the class is a specialization, the prompt should:
    1.  Include the blueprint's summary as "Base logic context."
    2.  Include its own specialized members and fields.
    3.  If `is_synthetic` is true, explicitly state that the provided code is the primary blueprint, while the node represents a specialized instance.
