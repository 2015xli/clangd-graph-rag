# 000-high-level-plan.md

**Objective**: Transition the `summary_engine` to a "Structural Manifest" model to ensure 100% summary coverage for all nodes, while adding deep graph awareness for C++ template specializations and recursive inheritance cycles.

#### **Core Goals**
1.  **Universal Coverage**: No node (File, Folder, Class, Function) should have a null summary.
2.  **Specialization Intelligence**: Treat template specializations as independent citizens while using graph relationships to provide context from their primary blueprints.
3.  **Recursive Awareness**: Summarize inheritance cycles (SCCs) through collective logic analysis.
4.  **Interface Understanding**: Summarize functions without bodies based on their signatures.

#### **Refactoring Roadmap**
- **010-sensing-and-ingestion**: [COMPLETED] Capturing template metadata and building the `[:SPECIALIZATION_OF]` relationship.
- **020-interface-summarization**: Handling body-less functions in Pass 1 & 2.
- **030-class-specialization-logic**: Integrating blueprint context into specialization and synthetic class summaries.
- **040-recursive-inheritance-sccs**: Cycle detection and 2-step SCC summarization.
- **050-file-folder-manifests**: Transitioning roll-ups to "Manifest" style inventories.
- **060-prompt-engineering**: Updating the `PromptManager` for new structural contexts.
