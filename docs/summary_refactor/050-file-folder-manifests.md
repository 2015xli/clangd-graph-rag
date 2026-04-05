# 050-file-folder-manifests.md

**Goal**: Provide 100% summary coverage for Files and Folders through a structural inventory (manifest) approach.

#### **1. Pass 5: File Inventory Summaries**
*   **`summary_engine/hierarchy_processor.py`**:
    *   In `_process_one_file_summary`, replace the child summary roll-up with a **manifest**.
*   **Logic**:
    *   **Includes**: Fetch list of paths from `[:INCLUDES]` relationship.
    *   **Logic Inventory**: Fetch list of all nodes with `[:DEFINES]` and `[:DECLARES]`.
    *   **Enrichment**: Include the summaries (if available) for the symbols in the inventory.
*   **Fallback**: If the inventory is empty, provide a summary like *"This file is an empty header or non-source asset."*

#### **2. Pass 6: Folder Logic Consistency**
*   **`summary_engine/hierarchy_processor.py`**:
    *   Update `_process_one_folder_summary`.
    *   Provide a "Folder Manifest" including subfolders and files.
    *   **AI Goal**: Describe the collective role of the components.
    *   **Fallback**: If the folder is empty, explicitly state *"The folder is empty or does not contain recognized source code."*
