# Infrastructure: Shared Support Modules

This document describes the foundational modules that provide essential services like Git integration, database connectivity, LLM interaction, and system-wide configurations.

## Table of Contents
1. [Git Integration (git_manager.py)](#git-integration-git_managerpy)
3. [Centralized Argument Parsing (input_params.py)](#centralized-argument-parsing-input_paramspy)
4. [Logging System (log_manager.py)](#logging-system-log_managerpy)
5. [Memory Debugging (memory_debugger.py)](#memory-debugging-memory_debuggerpy)

---

## Git Integration (git_manager.py)

The `GitManager` provides a clean interface to the Git repository using the `GitPython` library.

### Core Responsibilities
*   **Change Detection**: Categorizes changes between two commits into `added`, `modified`, and `deleted` sets.
*   **Normalization**: Automatically converts Git renames and copies into a series of delete and add operations, simplifying the logic for the `GraphUpdater`.
*   **Reference Resolution**: Translates user-friendly references (tags, branch names) into canonical full commit hashes.

---

## Centralized Argument Parsing (input_params.py)

To ensure consistency across the many standalone scripts in the project, all command-line arguments are defined in this centralized module.

### Rationale
By grouping arguments (e.g., `add_worker_args`, `add_rag_args`), we ensure that flags like `--num-parse-workers` behave identically whether you are running the `GraphBuilder` or a standalone `CallGraphExtractor`.

---

## Logging System (log_manager.py)

The project uses an "opinionated" logging configuration designed for multi-process environments.

### Features
*   **Dual Handlers**: Simultaneous logging to the console (for user feedback) and to `latest_run.log` (for deep debugging).
*   **Multi-Process Safety**: Specifically prevents child worker processes from creating redundant file handlers, ensuring the log file remains clean and readable.
*   **Granular Filtering**: Uses custom filters to ensure the console remains "high-signal" while the log file captures the full "noisy" details.

---

## Memory Debugging (memory_debugger.py)

A dedicated utility for profiling the application's memory footprint, which is essential when processing massive codebases like the Linux kernel.

### Capabilities
*   **Snapshots**: Uses `tracemalloc` to capture snapshots of the Python heap at critical pipeline junctions.
*   **Delta Analysis**: Compares snapshots to identify exactly which pass or data structure is responsible for memory growth.
