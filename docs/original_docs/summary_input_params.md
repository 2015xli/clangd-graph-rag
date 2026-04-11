# Algorithm Summary: `input_params.py` - Centralized Argument Parsing

## 1. Role in the Pipeline

As the project grew, multiple standalone Python scripts (`...builder.py`, `...updater.py`, etc.) began to share common command-line arguments. This led to duplicated code and potential inconsistencies. 

The `input_params.py` module was created to solve this problem. Its sole purpose is to centralize the definition of all shared command-line arguments in one place. This ensures that arguments like `--llm-api` or `--log-batch-size` are defined identically across all scripts that use them, following the Don't Repeat Yourself (DRY) principle.

## 2. Core Logic and Design

The design is simple and modular. The module contains a collection of functions, where each function is responsible for adding one logical group of arguments to a given `argparse.ArgumentParser` instance.

This allows each executable script to declaratively select which groups of arguments it needs, without having to redefine them locally.

### Argument Groups

The following functions are provided to add specific groups of arguments:

*   **`add_core_input_args(parser)`**: Adds the main positional arguments for the clangd index file and the project path.
*   **`add_worker_args(parser)`**: Adds arguments for controlling parallelism, such as `--num-parse-workers` and the local/remote workers for RAG.
*   **`add_batching_args(parser)`**: Adds arguments for performance tuning, like `--log-batch-size`, `--cypher-tx-size`, and `--ingest-batch-size`.
*   **`add_rag_args(parser)`**: Adds all arguments related to controlling the RAG generation process, such as `--generate-summary` and `--llm-api`.
*   **`add_ingestion_strategy_args(parser)`**: Adds arguments that control the specifics of the graph ingestion logic, like `--defines-generation`.
*   **`add_git_update_args(parser)`**: Adds arguments used exclusively by the incremental updater, such as `--old-commit` and `--new-commit`.
*   **`add_logistic_args(parser)`**: Adds arguments for controlling a script's mode of operation or output, such as `--output`, `--stats`, `--ingest`, and `--debug-memory`.

## 3. Usage Example

A typical `main()` function in a script now looks much cleaner. It simply imports the module and calls the functions for the argument groups it requires.

```python
import argparse
import input_params
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description='My script description.')
    
    # Add desired argument groups
    input_params.add_core_input_args(parser)
    input_params.add_rag_args(parser)
    input_params.add_worker_args(parser)
    
    args = parser.parse_args()

    # It is best practice to resolve paths immediately
    args.project_path = str(args.project_path.resolve())
    
    # ... rest of the script uses args ...
```

## 4. Benefits

This modular design provides several key benefits:
- **Consistency**: All arguments are defined identically everywhere.
- **Maintainability**: An argument's default value or help text only needs to be updated in one location.
- **Readability**: The main entry point of each script is now much shorter and more declarative.