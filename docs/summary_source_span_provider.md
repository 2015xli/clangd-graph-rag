# Algorithm Summary: `source_span_provider.py`

## 1. Role in the Pipeline

The `SourceSpanProvider` acts as the **semantic adapter** that pairs high-level index data (Symbols) with low-level implementation data (SourceSpans). It ensures the final graph is not only accurate in terms of "what" exists but also "where" it lives and "how" it is structured.

## 2. Orchestration Logic: `enrich_symbols_with_span`

The enrichment process is organized into five logical steps to ensure maximum precision and connectivity.

### Step 1: Preliminary Index-Based Hierarchy
Before matching implementation data, the provider establishes parentage using Clangd's own metadata:
*   **Reference Containers**: Resolves `parent_id` using explicit Clangd reference container fields.
*   **Scope Bridging**: Infers `parent_id` by matching a symbol's `scope` string against the qualified names of known Classes, Structs, or Namespaces.

### Step 2: Multi-Tier Matching and Progressive Propagation
This is the "meat" of the enrichment. Matching is performed in tiers, with **Intermediate Propagation** steps to ensure that anchored parents can be used as context for matching their children.

*   **Tier 1: Direct ID Matching**: Authoritative O(1) matching using USR-derived IDs. Symbols are anchored to implemention files immediately.
*   **Tier 2: Location-Based Matching**: Fallback for syntactic parsers or USR divergence, using expansion coordinates.
*   **Intermediate Propagation**: `_assign_sym_parent_based_on_sourcespan_parent` runs after Tiers 1 & 2. 
    *   **Rationale**: Tier 3 (Semantic Fallback) relies on the `(sym.parent_id, sym.name, sym.kind)` context. By resolving parent IDs for anchored symbols early, we "unlock" more candidates for semantic matching in Tier 3.
*   **Tier 3: Iterative Semantic Fallback**: Handles complex macro-expansion sites where IDs and coordinates diverge. Matches symbols based on their parent context.

### Step 3: Synthesis and Final Propagation
*   **Synthesis**: Remaining unmatched spans are converted into **Synthetic Symbols**.
*   **Final Propagation**: A second pass of parent assignment ensures that any Clangd-indexed symbol with an anonymous/synthetic parent is correctly linked.

### Step 4: Lexical Scope Fallback (Bodyless Symbols)
Handles symbols that lack implementation spans (e.g., Fields, Variables, Pure Virtuals).
*   **Spatial Containment**: Uses coordinates to find the smallest enclosing span.
*   **USR Bridging for Enums**: Specifically handles anonymous enums with constants defined in separate files by matching the `type` property against parent USRs.

### Step 5: Metadata Enrichment
Finalizes the graph with **Type Aliases**, **Macros**, and **Static Call Relations**.

## 3. Key Design Rationales

### 3.1. Separation of Concerns (Matching vs. Propagation)
Parent assignment is intentionally deferred to dedicated propagation steps. 
*   **Stability**: If `parent_id` were assigned inside matching loops, the `synthetic_id_to_index_id` registry would be a "moving target," potentially leading to inconsistent or race-condition-dependent results.
*   **Clarity**: Keeping these roles separate makes the code easier to debug and ensures the registry remains the single source of truth for anchored identities.

### 3.2. Implementation Anchoring
Matched symbols are always "moved" to the implementation file coordinates. This is fundamental for the RAG pipeline; it ensures that when an agent requests code for a symbol, it reads from the implemention file (`.cpp`) rather than a header declaration where the implementation doesn't exist.

### 3.3. Unambiguity Guards
*   **Exact Location**: Unambiguous because expansion coordinates are unique within a TU.
*   **Semantic Fallback**: Employs strict bi-directional uniqueness checks. If a context maps to multiple symbols or multiple spans, it is marked as ambiguous and skipped to prevent mis-attribution.
