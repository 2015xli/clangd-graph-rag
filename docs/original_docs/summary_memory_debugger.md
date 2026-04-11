# Algorithm Summary: `memory_debugger.py`

## 1. Role in the Pipeline

This script is a simple, self-contained debugging utility. It is not part of the main production pipeline but can be optionally enabled to help developers diagnose memory usage issues within the application.

It provides a `Debugger` class that acts as a wrapper around Python's built-in `tracemalloc` library, offering a convenient way to take snapshots of memory allocation at different points in the program's execution.

## 2. Core Logic

*   **Initialization (`__init__`)**: The `Debugger` class is initialized with a `turnon` boolean flag. If `True`, it immediately starts the `tracemalloc` service.
*   **Taking Snapshots (`memory_snapshot`)**: This is the main method. When called, it takes a snapshot of the current memory usage. 
    *   **Filtering Subtlety**: To reduce noise, it filters out allocations from Python's internal bootstrap modules and from the debugger script itself. This helps focus the output on the application's own memory usage.
    *   **Output**: It prints a formatted report to the console, showing the top memory-consuming lines of code and the total allocated size.
*   **Stopping (`stop`)**: Provides a method to stop the `tracemalloc` service cleanly.

## 3. Usage

The debugger is integrated into the main entry points of the application, such as `clangd_graph_rag_builder.py`, and can be activated via a command-line flag like `--debug-memory`.

A developer would use it like this:

1.  Run the application with the `--debug-memory` flag.
2.  The `Debugger` instance is created and starts tracing.
3.  At key points in the code (e.g., after a major pass), `debugger.memory_snapshot("Message here")` is called.
4.  The developer can then analyze the console output to see how memory usage grows and which parts of the code are responsible for the largest allocations.
