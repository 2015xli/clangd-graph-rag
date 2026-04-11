# Algorithm Summary: `graph_update_scope_builder.py`

## 1. Purpose: The "Sufficient Subset" Strategy

The `GraphUpdateScopeBuilder` is the architectural "brain" of the incremental update process. In a complex C++ project, symbols are highly interdependent; a change in one file (e.g., adding a macro or a virtual method) can cascade across the graph. A naive update that only re-ingests modified files would result in "broken" or missing relationships.

The purpose of this module is to construct a **Sufficient Subset** of symbols—a self-contained "mini-index" that includes:
1.  **Seed Symbols**: Every symbol directly defined or declared in the "dirty" files.
2.  **Structural Neighbors**: All symbols required to correctly reconstruct every relationship (Calls, Inheritance, Macros, etc.) involving those seed symbols, even if the neighbors themselves reside in unchanged files.

By building this mini-parser, the system can surgically "patch" the Neo4j graph, ensuring perfect integrity without the multi-hour cost of a full rebuild.

---

## 2. Orchestration: The Build Pipeline

The class manages the transition from raw source changes to an enriched, dependency-aware symbol set through two primary public methods.

### 2.1. `build_miniparser_for_dirty_scope()`
This method identifies the set of symbols that need to be re-ingested. It follows a deterministic 4-pass sequence:

1.  **Local Source Parsing**: It invokes the `CompilationManager` to parse **only the dirty files**. 
    *   **Rationale**: This gathers fresh "ground truth" (function body spans, macro expansions, and include directives) for the changed files with minimal CPU overhead.
2.  **Full-Index Enrichment**: It uses `SourceSpanProvider` to enrich the **entire `full_symbol_parser`** using the fresh spans.
    *   **Rationale**: **This is the most critical step for correctness.** A change in a dirty file (like a specialized member function) might provide the identity for a "phony" parent or a macro-generated symbol that resides in a non-dirty header. By enriching the full set, we ensure dependency expansion uses the most accurate semantic view of the entire codebase.
3.  **Seed Identification**: It identifies all symbols whose `definition` or `declaration` coordinates reside within the dirty file URIs.
4.  **Subset Expansion**: It delegates to `_create_sufficient_subset()` to perform the 1-hop dependency expansion.

### 2.2. `rebuild_mini_scope()`
This method is called after the updater has purged stale data from the graph. Its role is to run the standard ingestion processors (`PathProcessor`, `SymbolProcessor`, `CallGraphBuilder`) on the mini-parser.
*   **Rationale**: Because the mini-parser is a valid `SymbolParser` object, we can reuse 100% of the high-fidelity ingestion logic from the full builder, ensuring the incremental update produces a graph identical in quality to a full build.

---

## 3. The Expansion Algorithm: `_create_sufficient_subset`

This internal method implements the logic for identifying the "Sufficient Subset." It is designed around the principle of **Bi-directional 1-Hop Expansion**.

### 3.1. Phase 1: Pre-computation (Registry Building)
Before expanding, the algorithm performs a single pass over the **full symbol set** to build high-performance reverse-lookup maps. This turns complex graph traversals into O(1) dictionary lookups.
*   **Containment Map**: `parent_id` -> `Set[child_id]`.
*   **Namespace Map**: `qualified_name` -> `Set[child_id]`.
*   **Macro Map**: `expanded_from_id` -> `Set[symbol_id]`.
*   **TypeAlias Map**: `aliased_type_id` -> `Set[aliaser_id]`.
*   **Bi-directional Relations**: Inheritance, Overrides, and Call Graphs are indexed in both directions.

### 3.2. Phase 2: Bi-directional Expansion
The algorithm iterates through every seed symbol and pulls its "neighbors" into the subset. To maintain graph integrity, every relationship type is expanded in both directions:

| Relationship | Upward Expansion (Child -> Parent) | Downward Expansion (Parent -> Child) |
| :--- | :--- | :--- |
| **Lexical** | Pull in `parent_id` symbol. | Pull in all children via `containment_graph`. |
| **Namespace** | Pull in parent Namespace via `scope`. | Pull in all children via `scope_to_children_ids`. |
| **Calls** | Pull in all `callers`. | Pull in all `callees`. |
| **Inheritance** | Pull in all `base_classes`. | Pull in all `derived_classes`. |
| **Overrides** | Pull in `overridden_methods`. | Pull in `overriding_methods`. |
| **Macros** | Pull in source `MACRO` via `expanded_from_id`. | Pull in all `expanded_symbols` (Redundant but safe). |
| **TypeAlias** | Pull in `aliaser_ids` (Up to the aliaser). | Pull in `aliased_type_id` (Down to the target). |

---

## 4. Design Rationales

### 4.1. Rationale: Bi-directionality
If a seed symbol (e.g., a Class) is re-ingested, we must pull in its neighbors from *both* directions. If we only pulled in its parent, we would lose the connections to its children (Methods) that reside in unchanged files. Bi-directional expansion ensures that no matter which "end" of a relationship changes, the link is correctly re-materialized in Neo4j.

### 4.2. Rationale: Redundancy in Macro Expansion
While downward expansion from a Macro to its expanded symbols is technically redundant (any change to a macro expansion would naturally mark the expanded symbol's file as "dirty"), the logic is included for **architectural completeness**. It ensures the subset is mathematically "sufficient" to describe the Macro's role in the system, even in edge cases involving complex header inclusions.

### 4.3. Rationale: One-Hop Limit
Expansion is strictly limited to **1-hop**. While dependencies can be deeper (e.g., a call chain A->B->C), a 1-hop expansion is sufficient because the `SymbolProcessor` and `CallGraphBuilder` only operate on direct relationships. By capturing the immediate neighbors of every changed entity, we provide enough context to reconstruct every "edge" that could have been affected by the change.
