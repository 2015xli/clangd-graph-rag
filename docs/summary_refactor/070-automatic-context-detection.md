# 070-automatic-context-detection.md

**Goal**: Dynamically adapt the summarization chunking strategy to the specific LLM model being used, ensuring maximum context utilization without exceeding model limits.

---

### **Step 1: Model Metadata Retrieval (`llm_client.py`)**
**Operation**: Use LiteLLM to discover model-specific token limits.
1.  **API Selection**: Transition from `get_max_tokens` (total context) to `get_model_info` (granular limits).
2.  **Logic**:
    ```python
    info = litellm.get_model_info(self.model_name)
    max_input = info.get("max_input_tokens") or info.get("max_tokens")
    ```
3.  **Fallback**: Implement a multi-stage fallback for unregistered models (e.g., local Ollama):
    *   Try `get_model_info`.
    *   Fallback to `get_max_tokens`.
    *   Default to `128,000` tokens if all lookups fail.

---

### **Step 2: Orchestration Integration (`orchestrator.py`)**
**Operation**: Automatically configure the `SummaryEngine`.
1.  **Priority**: 
    *   Use `args.max_context_size` if explicitly provided by the user.
    *   Otherwise, query the `LlmClient` for the model's native limit.
2.  **Propagation**: Pass this value to the `NodeSummarizer` during initialization.

---

### **Step 3: Parameter Decoupling (`input_params.py`)**
**Operation**: Remove static defaults to enable dynamic discovery.
*   **Action**: Changed the default value of `--max-context-size` from `30000` to `None`.
*   **Result**: The `SummaryEngine` now recognizes `None` as a signal to trigger automatic detection.

---

### **Step 4: Resource Efficiency**
**Rationale**: By using the true model limit (often 128k+ for modern models like GPT-4o or DeepSeek), we minimize the need for expensive and lossy iterative summarization chunking, leading to higher quality roll-ups and faster overall execution.
