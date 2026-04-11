# Plan: Improve Summarization Quality with Prompt Environment and Global Context

This plan aims to enhance the quality of AI-generated summaries by providing consistent project-level and node-level context to all LLM prompts. It also implements a persistence loop for the project summary.

## Objective
1.  Add a standard context header to all LLM prompts to eliminate ambiguity.
2.  Implement a multi-tier project info resolution strategy (Machine-generated -> User-provided -> Fallback).
3.  Persist the project summary to `.cache/project-summary.md` for reuse in future runs.

## Proposed Changes

### 1. `summary_engine/prompts.py`
- Define a `PromptEnv` dataclass to hold `project_name`, `project_info`, `file_path`, and `node_scope`.
- Add a private `_apply_env_header` method to `PromptManager` to prepend the context header.
- Update all `get_*_prompt` methods to accept `env: PromptEnv` as the first argument and call `_apply_env_header`.

### 2. `summary_engine/orchestrator.py` (`SummaryEngine`)
- **`initialize_run`**:
    - Query Neo4j for the `PROJECT` node's `name`.
    - Resolve `project_info` by searching:
        1.  `.cache/project-summary.md` (Machine-generated).
        2.  `project_path/project-info.md` (User-provided).
        3.  Fallback to `(N/A)`.
    - Store `project_name` and `project_info` as instance variables.
- **`finalize_run`**:
    - New method to be called after all summarization passes.
    - If `llm_api != 'fake'`, query Neo4j for the `PROJECT` summary.
    - If a summary exists, write it to `.cache/project-summary.md`.

### 3. `summary_engine/node_summarizer.py` (`NodeSummarizer`)
- Update the `__init__` to accept `project_name` and `project_info`.
- Add a helper method `_get_prompt_env(node_data)` to construct a `PromptEnv` instance.
- Update all `get_*_summary` and `get_*_analysis` methods to use `_get_prompt_env` and pass it to the `PromptManager`.

### 4. `summary_driver/full_summarizer.py` and `summary_driver/incremental_summarizer.py`
- Call `self.engine.finalize_run()` at the end of the summarization process.

## Detailed Logic for Prompt Header
```text
The C/C++ code snippet below is provided within the following context:
- Project Name: {project_name}
- Project Background: {project_info}
- File Relative Path: {file_path}
- Code Lexical Scope: {node_scope}
--------------------------------------------------
```

## Verification Plan
1.  **Unit Tests**: Verify that `PromptManager` correctly prepends the header.
2.  **Integration Test (Fake LLM)**:
    - Run `full_summarizer` with `--llm-api fake`.
    - Verify that `initialize_run` correctly loads `project-info.md` if it exists.
    - Verify that `finalize_run` is called but does NOT write `.cache/project-summary.md` (due to 'fake' API).
3.  **Integration Test (Real/Mock LLM)**:
    - Run summarization.
    - Verify that `.cache/project-summary.md` is created after the project summarization pass.
    - Verify that subsequent runs use this file as `project_info`.
