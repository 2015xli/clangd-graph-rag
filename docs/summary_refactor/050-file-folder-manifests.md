# 050-file-folder-manifests.md

**Goal**: Transition File and Folder summaries from simple roll-ups to "Structural Manifests" with physical code context and robust staleness tracking via hashing.

---

### **Step 1: File Manifest Construction (`hierarchy_processor.py`)**
**Operation**: Identify inclusions and definitions, while capturing the physical state of the file.
1.  **Read Source**: Read the full file content from the filesystem.
2.  **Physical Hashing**: Calculate `current_code_hash = SHA1(file_body)`.
3.  **Discovery Query**: Fetch `[:INCLUDES]` paths and an inventory of `[:DEFINES|DECLARES]` symbols.

**Staleness Decision**:
*   A file is stale if `current_code_hash != db_code_hash`.
*   **OR** if any member of its symbol inventory has `summary_changed`.

---

### **Step 2: Manifest Summarization (`node_summarizer.py`)**
**Operation**: Implement the "Three-Stage Waterfall" for files.
1.  **DB Check**: If hashes match and context is clean, return `unchanged`.
2.  **Cache Check**: If DB is stale, check if the L1 cache matches the `current_code_hash`.
3.  **Regeneration**: If both fail, generate a manifest prompt including:
    *   List of relative include paths.
    *   List of defined symbols (with their individual summaries).
    *   **Source Code Context**: The literal text of the file (potentially truncated if exceeding token limits).

---

### **Step 3: Database Synchronization (`hierarchy_processor.py`)**
**Operation**: Ensure the physical state is persisted.
*   **Action**: The Neo4j `SET` query now updates both `n.summary` AND `n.code_hash` for the File node.
*   **Result**: Subsequent runs can perform O(1) staleness checks using only the database properties.

---

### **Step 4: Folder Manifest Consistency**
**Operation**: Apply similar manifest logic to folders and the project node.
*   **Empty Folders**: Explicitly summarized as *"This folder is empty or contains no recognized source files."*
*   **Incremental Propagation**: A change in a file's summary now correctly propagates staleness up to its containing folder and the root project.
