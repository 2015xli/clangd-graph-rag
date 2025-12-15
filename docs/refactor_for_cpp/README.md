# C++ Refactoring: A Case Study

This directory contains the collection of planning documents that chronicle the step-by-step refactoring of the `clangd-graph-rag` project from a C-only analysis tool to a system with full, robust support for C++.

These documents are preserved as a detailed case study. They provide valuable insight into the process of evolving a complex data pipeline, from initial schema design to identifying and solving subtle challenges, and finally updating all downstream consumers of the new, richer data model. For any developer looking to understand the system's core design decisions or undertake a similar project, this serves as a comprehensive guide.

The refactoring process was broken down into three major phases.

---

### Phase 1: Foundational Schema Changes

This phase focused on incrementally evolving the graph schema to support core C++ concepts.

1.  **[The Blueprint](./000_plan_for_cpp_support.md)**: This document lays out the foundational goal: defining the new, richer graph schema required to model C++'s object-oriented and logical constructs. It is the blueprint for the entire refactoring effort.

2.  **[Adding Body Locations](./005_plan_body_location.md)**: A necessary preliminary step to ensure all data structures (`struct`, `enum`, etc.) have their full source code body location stored in the graph, enabling on-demand parsing by downstream tools.

3.  **[Modeling Fields](./010_plan_fields.md)**: The first step in modeling structure members, this plan details the introduction of `:FIELD` nodes and their connection to C-style data structures.

4.  **[Splitting C and C++ Constructs](./020_plan_class_method_split.md)**: A major step where the original `FUNCTION` and `DATA_STRUCTURE` nodes were split into `FUNCTION`/`METHOD` and `DATA_STRUCTURE`/`CLASS_STRUCTURE` respectively, formally distinguishing between C-style and C++ constructs.

5.  **[Implementing Inheritance](./030_plan_inheritance.md)**: With class nodes in place, this plan details the implementation of `:INHERITS` and method `:OVERRIDDEN_BY` relationships, capturing the core of object-oriented design.

6.  **[Updating the Call Graph](./040_plan_call_graph.md)**: This document outlines the necessary changes to the call graph builder to ensure it correctly creates `:CALLS` relationships for all four scenarios (function-to-function, function-to-method, etc.).

---

### Phase 2: Advanced Structural Modeling

This phase tackled the more subtle and complex challenges of accurately representing C++'s lexical and logical structure.

-   **[Logical Constructs & Lexical Nesting](./050_plan_logical_constructs.md)**: This is one of the most critical design documents. It details the **Problem** of why the `clangd` index's `scope` string is insufficient for building hierarchies, and the **Solution**: a robust, two-stage, span-based reconciliation process between the `CompilationParser` and `SourceSpanProvider` that correctly models lexical nesting even for anonymous structures.

---

### Phase 3: Updating Downstream Systems

This final phase details how the rest of the application was updated to leverage and support the new, richer graph schema.

-   **[Updating the RAG Engine](./060_plan_rag_update.md)**: Details the changes required to make the AI summary generation system aware of the new C++ node types like `:CLASS_STRUCTURE` and `:METHOD`.

-   **[Updating the Incremental Graph Updater](./070_plan_updater.md)**: Outlines the updates to the incremental updater's purging and rebuilding logic to make it fully compatible with the new schema.

-   **[Refactoring the "Sufficient Subset" Logic](./080_plan_mini_parser.md)**: A deep dive into the critical refactoring of the "mini-parser" creation for incremental updates. It explains why the old algorithm was insufficient for C++ and details the new, comprehensive graph expansion algorithm that ensures the updater has all the symbols it needs to correctly patch the graph.
