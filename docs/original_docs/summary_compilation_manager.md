# Algorithm Summary: `compilation_manager.py`

## 1. Role in the Pipeline

This module provides the `CompilationManager` class, which acts as the high-level orchestrator for the entire source code parsing process. It was created by refactoring the old `function_span_extractor.py` to have a broader and clearer set of responsibilities.

Its purpose is to serve as the single, unified interface for any other part of the system that needs to access source code information like function spans, **type aliases**, **macro definitions**, or include relationships. It decouples the main application logic from the low-level details of parsing and caching, providing a simple and robust API for both full-project and incremental parsing.

## 2. Core Responsibilities

The `CompilationManager` has two primary responsibilities:

1.  **Strategy Selection**: Based on user input (e.g., `--source-parser clang`), it instantiates the appropriate low-level parser strategy (`ClangParser` or `TreesitterParser`) from the `compilation_parser.py` module.
2.  **Caching**: It manages a sophisticated, context-aware caching layer to avoid re-running the expensive parsing process unnecessarily. All caching logic is centralized in this manager and its helper class, `CacheManager`.

## 3. Caching Architecture (`CacheManager`)

The caching logic was completely redesigned for scalability and reliability. It is now handled by a dedicated `CacheManager` class that uses a structured file-naming convention for fast cache discovery and a deep validation protocol to ensure correctness.

### Cache Naming Convention

The `CacheManager` generates predictable filenames that embed the context of the parse, allowing it to quickly find a potential cache file without opening and inspecting every file. All cache files are stored in a `.cache/` directory within the project root.

1.  **Git-based Naming**: Used when parsing a Git repository. The filename is keyed by commit hashes.
    *   **Format**: `parsing_[project_name]_hash_[new_commit_short]_[old_commit_short].pkl`
    *   **Example (Full Build)**: `parsing_my-project_hash_a1b2c3d4_.pkl`
    *   **Example (Update)**: `parsing_my-project_hash_f4e5d6c7_a1b2c3d4.pkl`

2.  **Time-based Naming**: The fallback for non-Git projects or ad-hoc file lists. The filename is keyed by the oldest and newest modification times among the files being parsed.
    *   **Format**: `parsing_[project_name]_time_[latest_mtime_hex]_[oldest_mtime_hex].pkl`

### Cache Validation

A two-step validation process ensures cache integrity:

1.  **Discovery**: The `CacheManager` first constructs the expected filename based on the context (commits or times) and checks if that single file exists.
2.  **Deep Validation**: If a candidate file is found, it is opened and its internal metadata is checked to prevent collisions and guarantee correctness:
    *   For **Git-based** caches, the full `new_commit` and `old_commit` hashes stored inside the file must exactly match the requested hashes.
    *   For **Time-based** caches, a SHA256 hash of the sorted list of file paths is stored in the cache. This hash is re-computed from the current file list and must match the stored hash, which prevents incorrect cache hits if two different file lists happen to share the same min/max modification times.

## 4. Public API and Workflows (Refactored)

The manager's public API has been refactored for clarity and power. All parsing and caching logic is now centralized in the `parse_files` method, and `parse_folder` acts as a convenient wrapper that delegates to it.

*   **`parse_files()`**: This is now the **central method for all parsing and caching**. It intelligently selects one of two caching strategies:
    1.  **Git-based Caching**: This path is triggered if a `new_commit` argument is provided. It handles both full builds (when `old_commit` is `None`) and incremental updates (when `old_commit` is also provided). It uses the Git-based naming and validation strategy.
    2.  **Mtime-based Caching**: This is the fallback used when no commit information is given. It is used for non-Git projects or any arbitrary list of files. It uses the time-based naming and file-list hash validation strategy.

*   **`parse_folder()`**: This method is now a convenience wrapper that translates a folder path into a file list and then delegates to `parse_files`.
    1.  **If in a Git repo**, it uses the fast `git ls-tree` command to get an accurate list of all files at a specific commit (`HEAD` by default). It then calls `parse_files` with this list and the commit hash, activating the Git-based caching path. This is highly scalable and avoids slow filesystem walks on large projects.
    2.  **If not in a Git repo**, it performs a traditional walk of the filesystem to gather all source files and calls `parse_files` without any commit info, activating the mtime-based caching path.

*   **`get_source_spans()` / `get_include_relations()` / `get_type_alias_spans()` / `get_macro_spans()`**: These methods are used to retrieve the extracted data after a `parse_*` method has been called.

## 5. Git Reference Resolution

To improve user-friendliness, both `parse_folder` and `parse_files` can accept user-friendly Git references (like tags or branch names) for the `commit_hash`, `new_commit`, and `old_commit` arguments.

This is handled internally by calling the `resolve_commit_ref_to_hash` helper function from `git_manager.py`. This function converts the user's input into a full, canonical commit hash before it is used by the caching system, ensuring that the cache keys remain consistent and reliable while the user interface is flexible.