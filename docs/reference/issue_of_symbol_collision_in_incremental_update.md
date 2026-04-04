# Comprehensive Analysis: Symbol Collisions and Relationship Discrepancies in Incremental Updates

## 1. The Milestone and the Mystery
After refactoring the `CompilationParser` and `SymbolEnricher` to use **USR-derived IDs** (aligning the parser's "sensing" layer with the `clangd` index's identity), the graph integrity reached a new milestone. Orphan nodes—previously a significant issue—dropped to nearly zero even in massive projects like the Linux kernel and LLVM.

However, a subtle discrepancy was discovered during a Tag 1 -> Tag 4 incremental update of the `Llama` project:
*   **Total Node Count**: Identical to a full build of Tag 4.
*   **Relationship Count**: The incremental build resulted in **6 more relationships** than the full build (e.g., 30,374 vs 30,368).
*   **The Paradox**: Debugging dumps of purged vs. added nodes showed a **zero diff**, implying all intended changes were accounted for, yet "ghost" relationships remained.

---

## 2. Root Cause: The "Semantic Migration" Discovery Gap

The discrepancy is rooted in C++ projects that violate the **One Definition Rule (ODR)** or contain symbols that migrate between files across commits.

### 2.1. The Example: Multiple `main()` Functions
In projects with multiple test entry points, several files may define a `main()` function. These functions share the **exact same USR ID**.
1.  **Tag 1 (State)**: The graph thinks `main()` (ID: `HEX_123`) is defined by `file_clean.cpp`.
    *   Relationship: `(file_clean.cpp)-[:DEFINES]->(main_node)`.
2.  **Tag 4 (Change)**: `file_dirty.cpp` is modified. It also contains a `main()` function. 
3.  **The Purge Gap**: The updater purges everything **currently anchored** to `file_dirty.cpp`. Since the graph thinks `main()` belongs to `file_clean.cpp`, the purge logic **misses it**. The stale relationship from `file_clean.cpp` survives.
4.  **The Ingestion Duplicate**: The updater re-ingests `file_dirty.cpp`. It sees `main()` and executes `MERGE (file_dirty.cpp)-[:DEFINES]->(main_node)`.
5.  **Result**: The `main_node` now has **two** incoming `[:DEFINES]` edges. The node count is correct (1), but the relationship count is +1.

### 2.2. The Three Conditions for Collision
The discrepancy of a single node (ID) being linked to multiple files only occurs when the following three conditions are met simultaneously:

1.  **ODR Violation in Source Code**: There is an inherent "bug" or pattern in the source code where the same semantic identity (USR) is defined in multiple files (e.g., multiple `main()` functions or duplicate global variables).
2.  **Lack of Cross-TU Reconciliation**: Neither the Clang parser nor the Clangd indexer can reconcile this inconsistency during an incremental update. While a full indexer can pick a "winner," the incremental parser only sees the current "dirty" file and has no awareness of the conflicting symbol sitting in a "clean" file in the graph.
3.  **Discovery Failure in Path-Only Purging**: If the updater only purges nodes anchored to the "dirty" file path, the colliding node survives because it is currently anchored to a "clean" file. The subsequent re-ingestion then adds a second ownership relationship to the existing node.

### 2.3. Core Nuances: Identity Independence
This discrepancy only occurs for relationships where the **identity of the participants is independent of the relationship itself**.
*   **USR-Dependent (Safe)**: `[:HAS_METHOD]`, `[:HAS_FIELD]`. The parent's name is part of the child's USR. If a method moves to a different class, its ID changes. It is a different node; no collision occurs.
*   **Identity-Independent (Unsafe)**: `[:DEFINES]`, `[:DECLARES]`, `[:CALLS]`, `[:INHERITS]`. The symbol's ID remains the same even if its file owner or its call/inheritance targets change.

---

## 3. Evolution of the Solution

### 3.1. Iteration 1: The "Surgical Divorce" (Discarded)
Initial thought: identify seed symbols and delete specific incoming/outgoing relationship types.
*   **Limitation**: Required maintaining a manual list of relationship types and was prone to missing schema changes.

### 3.2. Iteration 2: Absolute Identity Purge (Isolation Mode)
Strategy: `DETACH DELETE` every Node by ID if that ID is found in the fresh parse of dirty files (`seed_symbol_ids`).
*   **Outcome**: Solved the migration and collision problems perfectly.
*   **The New Discrepancy (Aggregation Loss)**: In the Llama project, `main()` in `file_clean` called 112 static functions. `main()` in `file_dirty` called 335 functions.
    *   A **Full Build** aggregates these into **447** `[:CALLS]`.
    *   The **ID Purge** destroyed the node, and the partial rebuild only re-created the **335** calls from the dirty file.
*   **Conclusion**: This mode is useful for debugging (1:1 parity with dirty source) but fails the "Full Build Parity" test for aggregated behavioral links.

### 3.3. Iteration 3: Surgical Aggregation (The Final Solution)
The final strategy distinguishes between **Non-Sharable (Ownership)** and **Sharable (Behavioral)** relationships for colliding nodes.

#### **Logic for Colliding Nodes**
A "Colliding Node" is an ID found in a dirty file that currently claims to belong to a clean file in the graph.
1.  **Purge Non-Sharable Links**: Surgical `DELETE` of `[:DEFINES]`, `[:DECLARES]`, `[:EXPANDED_FROM]`, and `[:ALIAS_OF]`.
    *   *Rationale*: These define the "origin" of a symbol. A symbol should only have one defining file and one macro source at a time.
2.  **Retain Sharable Links**: Keep `[:CALLS]`, `[:INHERITS]`, `[:OVERRIDDEN_BY]`.
    *   *Rationale*: These are behavioral aggregations. By keeping the old calls and merging the new ones, the incremental update matches the "Accumulator" behavior of a full build.
3.  **Update Properties**: Use `MERGE` during ingestion to update `body_location`, `path`, and summaries to the latest (dirty) version.

---

## 4. Implementation Details

### 4.1. Precision Terminology
*   **Symbol**: In-memory data object from the `ClangParser`.
*   **Node**: Actual physical record in Neo4j.
*   **Seed Symbol**: A symbol physically located in a dirty file in the new parse.
*   **Colliding Node**: A node in the graph matching a Seed Symbol but anchored to a clean file.

### 4.2. Dual-Mode Resolution (`purge_nodes_by_id`)
The `Neo4jManager` now supports two modes controlled by the `--debug-incremental` flag:
*   **Debug Mode (Isolation)**: Uses `DETACH DELETE`. Ensures the resulting graph only contains relationships physically present in the dirty files. Ideal for diffing and identifying ODR violations.
*   **Standard Mode (Aggregation)**: Uses surgical relationship removal. Achieves 100% parity with a full build by allowing behavioral relationships to aggregate.

### 4.3. Label-Aware Performance
To ensure scalability, the purge logic uses the `Symbol.get_node_label` helper. Queries are executed per-label:
`MATCH (n:FUNCTION {id: $sid}) WHERE NOT n.path IN $dirty_files ...`
This turns a full-graph scan into an **O(1) index lookup**, enabling high-speed incremental updates even on projects with millions of nodes.

---

## 5. Post-Debug Findings & The "Aggregation Conflict"

In projects that violate the One Definition Rule (ODR), such as those with multiple `main()` functions, incremental updates face a conflict between **Isolation** and **Aggregation**.

### 5.1. Symbol Aggregation (Full Build Behavior)
In a full build, symbols with colliding IDs (like `main`) act as **accumulators**. The final graph node represents the union of all definitions.
*   Example: `main` in `file_A` calls 112 functions; `main` in `file_B` calls 335.
*   Full Build Total: **447** `[:CALLS]` relationships.

### 5.2. Incremental Parity through Surgical Aggregation
By implementing the "Surgical Aggregation" strategy, the incremental updater now:
1.  **Purges Non-Sharable Links**: Removes stale `[:DEFINES]`, `[:DECLARES]`, `[:EXPANDED_FROM]`, and `[:ALIAS_OF]` links.
2.  **Retains Sharable Links**: Keeps `[:CALLS]`, `[:INHERITS]`, etc.
3.  **Updates Properties**: Forces the node's `path` and `body_location` to match the latest dirty file.

The Result: The incremental update now achieves **100% parity** with a full build regarding both property accuracy and relationship aggregation.

---

## 6. Debugging Methodology

To validate the incremental update, the system provides a specialized auditing suite enabled via the `--debug-incremental` flag.

### 6.1. The Audit Logs
*   **`updater_purged_scope.log`**: Standard symbols removed because they were anchored to a dirty file.
*   **`updater_colliding_symbols.log`**: Symbols physically found in dirty files but logically claiming to be in clean files (the "Migration Report").
*   **`combined_purged_scope.log`**: A deduplicated union of the above, representing the total "Wipe" before ingestion.
*   **`updater_updated_scope.log`**: All nodes and relationships actually created/tagged during the rebuild.

### 6.2. Case Study: Resolving the Llama Discrepancy
When we observed 6 extra relationships in the Llama project, we followed this workflow:

1.  **Diff the Combined Logs**:
    `diff combined_purged_scope.log updater_updated_scope.log`
2.  **Observation (Ownership)**: 
    The diff showed 6 `[:DEFINES]` relationships being removed from their old files and 6 new ones added to `test-backend-ops.cpp`. Since the diff was balanced (6 out, 6 in), the discrepancy wasn't in total ownership count.
3.  **Observation (Collisions)**:
    Inspecting `updater_colliding_symbols.log` revealed 9 symbols, including `main()` and 3 methods like `print_footer`.
4.  **The "Smoking Gun" (Behavioral Discrepancy)**:
    The diff also showed **112 lost CALLS** (when using the previous Isolation-mode solution).
    *   `combined_purged_scope.log` had 447 calls for `main`.
    *   `updater_updated_scope.log` had 335 calls for `main`.
    *   *Conclusion*: This confirmed that `main()` was acting as an accumulator in the full build, and the isolation mode was discarding the "clean" calls.

### 6.3. Interpreting Results
*   **Balanced Diff**: Perfect parity for that relationship type.
*   **Unbalanced Diff (Losses)**: Correctly shedding stale local relationships if in Isolation mode, or a sign of missing data in Aggregation mode.
*   **Unbalanced Diff (Gains)**: New relationships introduced by fresh logic in the dirty files.

---

## 7. Conclusion
The "Ghost Relationship" mystery was a masterclass in the difference between lexical and semantic identity. By implementing **Surgical Aggregation**, the incremental updater now produces a graph that is structurally identical to a full build while ensuring that symbol properties and ownership always reflect the most recent implementation ground truth.
