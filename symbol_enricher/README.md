# Symbol Enricher: The Semantic Bridge

The `symbol_enricher` package is the critical adapter that reconciles high-level index data with low-level implementation "ground truth." It attaches physical source code coordinates and lexical hierarchy information to the symbols discovered by Clangd.

## Table of Contents
1. [Role and Purpose](#1-role-and-purpose)
2. [Enrichment Orchestration (base.py)](#2-enrichment-orchestration-basepy)
3. [Identity Matching Tiers (matcher.py)](#3-identity-matching-tiers-matcherpy)
4. [Hierarchy Discovery (hierarchy.py)](#4-hierarchy-discovery-hierarchypy)
5. [Metadata and Synthesis (enrich_extras.py)](#5-metadata-and-synthesis-enrich_extraspy)

---

## 1. Role and Purpose

Clangd's index is semantically rich but physically sparse—it knows *that* a function exists, but often lacks its exact body boundaries. This package bridges the gap:
*   **Physical Grounding**: Attaches `body_location` coordinates to symbols, enabling AI agents to retrieve full function implementations.
*   **Structural Integrity**: Builds parent-child relationships (lexical nesting) that the index often omits, especially for template specializations and macro-generated code.
*   **Synthesis**: Creates "Synthetic Symbols" for entities ignored by Clangd (e.g., anonymous enums, internal macros) to ensure graph completeness.

---

## 2. Enrichment Orchestration (base.py)

The `SymbolEnricher` class executes a multi-tier state machine to resolve identities and hierarchies through progressive refinement.

### The Enrichment Lifecycle (`enrich_symbols`)
1.  **Index-Only Pass**: Preliminary hierarchy building using only Clangd's internal references.
2.  **Tier 1 & 2 Matching**: Authoritative matching using direct USR-hashes and source coordinates.
3.  **Tier 3 Reconciliation**: An iterative fallback pass that uses semantic context (Parent + Name + Kind) to match remaining macro-expanded symbols.
4.  **Synthesis**: Converts unmatched implementation spans into "Synthetic Symbols."
5.  **Final Propagation**: Completes the hierarchy for all remaining nodes.
6.  **Metadata Overlay**: Injects Type Aliases, Macros, and Static Call relations.

---

## 3. Identity Matching Tiers (matcher.py)

Matching is tiered to prioritize semantic certainty while maintaining robust fallbacks.

### Tier 1: Direct ID Matching
Uses USR-derived IDs. Since the `SourceParser` replicates Clangd's hashing logic, this allows for O(1) authoritative matching. When matched, the symbol is "anchored" to its implementation file.

### Tier 2: Location-Based Matching
A fallback for symbols lacking USRs (e.g., from syntactic parsers). It uses **Expansion Coordinates** (where code appears in the TU) to ensure unambiguous coordinate-based matching.

### Tier 3: Semantic Context Fallback
Designed for complex macro expansions where USRs and coordinates might diverge.
*   **Context Key**: `(parent_id, name, kind)`.
*   **Mechanism**: If exactly one Clangd symbol and one parser span share the same context within a scope, they are paired. This pass runs iteratively to "unfold" nested hierarchies level-by-level.

---

## 4. Hierarchy Discovery (hierarchy.py)

This module ensures every node is correctly linked to its parent, even across file boundaries.

### Strategies
*   **Reference Containers**: Uses Clangd's `container_id` from symbol declarations.
*   **Scope Bridging**: Uses qualified name strings (e.g., `Namespace::Class::`) to find parents.
*   **Geometric Containment**: For "bodyless" symbols (Fields, Variables), it scans for the physically smallest span that encloses the symbol's name location.
*   **USR-Bridging for Enums**: Mathematically derives the parent ID for `EnumConstant` symbols by hashing the parent USR stored in their `type` field.

---

## 5. Metadata and Synthesis (enrich_extras.py)

The final stage of enrichment adds specialized data that transforms the graph into a high-fidelity RAG source.

*   **Macro Discovery**: Injects `:MACRO` symbols with their full `#define` text and `is_function_like` metadata.
*   **TypeAlias Enrichment**: Resolves the target of `typedef` and `using` statements, ensuring they point to the correct canonical IDs (including anonymous types).
*   **Static Call Injection**: Injects internal-linkage calls (captured by the parser) into the symbols' reference lists, enabling a more accurate call graph.
*   **Semantic Ownership**: Uses the `member_ids` list from the AST to link children to parents, providing the "ultimate truth" that transcends file boundaries and macro injection.
