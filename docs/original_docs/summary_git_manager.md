# Algorithm Summary: `git_manager.py`

## 1. Role in the Pipeline

This script is a dedicated library module that serves as a clean interface to the `git` command-line tool, using the `GitPython` library. Its sole purpose is to identify which source files have changed between two git commits and provide this information in a simple, categorized format for the `clangd_graph_rag_updater.py` script.

## 2. Core Algorithm

The manager uses a two-layer approach to provide a clean API while using powerful, low-level git features for accuracy.

### `_get_detailed_changed_files()` (The Low-Level Worker)

This private method is responsible for the actual git interaction. It is designed for precision.

*   **Mechanism**: Instead of using a simple `repo.diff()`, it shells out to the raw `git diff-tree` command. This is done to access powerful flags that are crucial for accuracy.
*   **Key Flags and Subtleties**:
    *   `-M100%` and `-C100%`: These flags instruct git to detect renames and copies, but only if the file content is **100% identical**. This is a critical design choice to avoid ambiguity. A file that was renamed *and* had a single line changed will be treated as a deletion of the old file and an addition of a new one, which is the desired behavior for the graph updater.
    *   `-z`: This uses null characters to terminate file names in the output. This ensures that file paths containing spaces or other special characters are parsed reliably.
*   **Output**: This method returns a detailed dictionary with five categories: `added`, `modified`, `deleted`, `renamed_exact`, and `copied_exact`.

### `get_categorized_changed_files()` (The Public API)

This is the public method used by the updater. Its job is to simplify the detailed output from the method above into the three simple categories the updater needs.

*   **Mechanism**:
    1.  It takes the `added`, `modified`, and `deleted` lists directly.
    2.  For every file in the `renamed_exact` list, it adds the *old path* to the `deleted` list and the *new path* to the `added` list.
    3.  For every file in the `copied_exact` list, it adds the *new path* to the `added` list.
*   **Output**: It returns a simple dictionary with three keys: `{'added': [...], 'modified': [...], 'deleted': [...]}`. This abstraction allows the graph updater to have a very clean and straightforward understanding of the changes, without needing to worry about the complexities of renames or copies.
