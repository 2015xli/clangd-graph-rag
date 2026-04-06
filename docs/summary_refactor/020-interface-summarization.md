# 020-interface-summarization.md

**Goal**: Provide 100% summary coverage for all functions and methods by integrating deterministic interface analysis for nodes without bodies into the existing Pass 1 pipeline.

#### **1. Driver Update (`summary_driver/full_summarizer.py`)**
*   **Action**: Modify the query used to seed the summarization process.
*   **Change**: Remove the filter `WHERE n.body_location IS NOT NULL` to ensure all `FUNCTION` and `METHOD` node IDs are passed to the processor.

#### **2. Integrated Processor Logic (`summary_engine/function_processor.py`)**
*   **Method**: `analyze_functions_individually_with_ids(self, function_ids)`
*   **Workflow**:
    1.  **Partition**: Split the incoming `function_ids` into two groups by querying Neo4j:
        *   `impl_nodes`: Nodes WHERE `body_location IS NOT NULL`.
        *   `interface_nodes`: Nodes WHERE `body_location IS NULL`.
    2.  **Implementation Pass**: Execute the standard `_process_one_function_for_code_analysis` (LLM-powered) for `impl_nodes`.
    3.  **Interface Pass**: Execute a new worker `_process_one_interface_for_analysis` (Deterministic) for `interface_nodes`.
*   **Worker**: `_process_one_interface_for_analysis`
    *   Set `code_hash = node_id`.
    *   Call `self.node_processor.get_interface_analysis(node_data)`.
    *   Update Neo4j: `SET n.code_analysis = $analysis, n.code_hash = $node_id`.

#### **3. Deterministic "LLM-Free" Analysis (`summary_engine/node_summarizer.py`)**
*   **Method**: `get_interface_analysis(self, node_data)`
*   **Logic**:
    1.  **Cache Check**: Use the `SummaryCacheManager` to check for an existing entry for this `id` with `code_hash == id`.
    2.  **Restore**: If found, return `status="code_analysis_restored"`.
    3.  **Generate**: If not found, construct the analysis string:
        *   `"This {kind}: {name} is an interface/declaration that has no code implementation. It's defined as: {return_type} {name}{signature}."`
        *   Return `status="code_analysis_regenerated"`, `data={"code_analysis": text, "code_hash": node_id}`.

#### **4. Cache Integrity & Transition Logic**
*   **Mechanism**: By using the `node_id` as the `code_hash` for interfaces, we leverage the existing L1 cache validation logic.
*   **Transition (Interface -> Implementation)**: If a body is added to a function, the new `code_hash` (a SHA1 of text) will not match the old `code_hash` (the ID string). This triggers a cache miss and switches the node to the LLM-powered implementation analysis.
*   **Transition (Implementation -> Interface)**: Similarly, if a body is removed, the new `code_hash` (the ID) will not match the old SHA1 hash, triggering a reset to the deterministic interface string.
