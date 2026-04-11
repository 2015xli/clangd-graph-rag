# 000-high-level-plan.md

**Objective**: Transition the `summary_engine` to a "Structural Manifest" model to ensure 100% summary coverage for all nodes, while adding deep graph awareness for C++ template specializations and recursive inheritance cycles.

#### **Core Goals**
1.  **Universal Coverage**: [COMPLETED] Every logical and physical node (File, Folder, Class, Data Structure, Function, Namespace) now has a summary.
2.  **Specialization Intelligence**: [COMPLETED] Treat template specializations as independent citizens while using graph relationships to provide context from their primary blueprints.
3.  **Recursive Awareness**: [COMPLETED] Summarize inheritance cycles (SCCs) through persistent 2-step collective logic analysis.
4.  **Interface Understanding**: [COMPLETED] Summarize functions without bodies based on their signatures via deterministic generation.
5.  **Physical-Logical Hashing**: [COMPLETED] Implement robust staleness tracking (hashing) for all structural nodes, including logical containers like namespaces.

#### **Refactoring Roadmap**
- **010-sensing-and-ingestion**: [COMPLETED] Capturing template metadata and building the `[:SPECIALIZATION_OF]` relationship.
- **020-interface-summarization**: [COMPLETED] Handling body-less functions via deterministic string generation.
- **030-class-specialization-logic**: [COMPLETED] Integrating physical hashing and manifestation (code + members) for classes.
- **040-recursive-inheritance-sccs**: [COMPLETED] Cycle detection and 2-step SCC summarization with persistent group analysis.
- **050-file-folder-manifests**: [COMPLETED] Transitioning roll-ups to "Manifest" style inventories with physical code context.
- **060-prompt-engineering**: [COMPLETED] Updating the `PromptManager` for new structural and manifest contexts.
- **070-automatic-context-detection**: [COMPLETED] Implementing model-aware context window discovery via LiteLLM.
- **080-data-structure-summarization**: [COMPLETED] Extending structural manifests to Enums, Unions, and C-style structs.
- **090-namespace-membership-hashing**: [COMPLETED] Implementing logical staleness tracking for namespaces via membership hashing.
