# 020-interface-summarization.md

**Goal**: Provide meaningful summaries for function/method declarations and virtual functions.

#### **1. Pass 1 Logic Update**
*   **`summary_engine/function_processor.py`**:
    *   In `_get_functions_for_code_analysis`, remove the clause `AND n.body_location IS NOT NULL`.
    *   This ensures all `FUNCTION` and `METHOD` nodes enter the summarization pipeline.

#### **2. Node Processor Update**
*   **`summary_engine/node_summarizer.py`**:
    *   Update `get_function_code_analysis`.
    *   If `body_location` is null, skip source text extraction.
    *   Pass `signature` and `return_type` to the `PromptManager` to generate a "Declaration Only" prompt.

#### **3. Contextual Pass (Pass 2)**
*   **Prompt Strategy**:
    *   If the node has no body, the context (callers/callees) will be combined with the interface summary.
    *   **Result**: Even a virtual function can now have a contextual summary like *"This is a virtual interface for processing events, invoked by the EventBus when a new packet arrives."*
