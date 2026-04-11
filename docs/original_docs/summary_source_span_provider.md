# Algorithm Summary: `source_span_provider.py`

## 1. Role in the Pipeline: The Semantic Bridge

The `SourceSpanProvider` acts as the critical **adapter** and **enricher** that bridges the gap between two primary data sources:
1.  **High-Level Index (SymbolParser)**: Contains the "what" (Symbols, names, kinds, and references) derived from the Clangd index. This data is semantically rich but physically sparse.
2.  **Low-Level Ground Truth (CompilationManager)**: Contains the "where" (SourceSpans, physical coordinates, and AST-level ownership) derived from direct AST parsing.

Its purpose is to attach implementation reality to index entries. This enrichment enables:
*   **Implementation Retrieval**: AI agents can extract the full body of a function or class specialization.
*   **Structural Connectivity**: Building a fully linked graph hierarchy (Parent-Child relationships) even when tools disagree on symbol locations.
*   **Graph Completeness**: Creating "Synthetic Symbols" for entities the indexer ignored (e.g., anonymous enums, template specializations) to ensure no part of the code is a "blind spot."

---

## 2. Orchestration Logic: `enrich_symbols_with_span`

The enrichment process is organized as a **multi-tier state machine**. Instead of a single pass, it uses progressive refinement to resolve identities and hierarchies.

### 2.1. State Management
The provider maintains several key state structures during its execution:
*   **`all_remaining_spans`**: A flattened, global map of implementation data (`id -> SourceSpan`). Flattening across file boundaries is safe because USR-derived IDs are globally unique.
*   **`synthetic_id_to_index_id` (The Registry)**: The "Truth Bridge" that records every pairing between a parser ID and a Clangd ID. This is vital for translating synthetic parent links into canonical graph links.
*   **`sym_source_span_parent` (Propagation Queue)**: Temporary storage for parent-child relationships found during matching that cannot yet be assigned (e.g., if the parent hasn't been anchored to a canonical ID yet).

### 2.2. The High-Level Flow Logic
The `enrich_symbols_with_span` method executes the following deterministic sequence:

1.  **Index-Only Pass**: Preliminary parent assignment using only the metadata provided by Clangd (References and Scopes).
2.  **Implementation Matching (Tiers 1 & 2)**: Authoritative matching using direct IDs and source coordinates.
3.  **Intermediate Propagation**: Converts recorded parser-parent links into Clangd-parent links for all symbols anchored in Step 2.
4.  **Semantic Reconciliation (Tier 3)**: An iterative fallback pass that uses the parents found in Step 3 to match remaining macro-expanded symbols.
5.  **Synthesis**: Final conversion of all remaining implemention data into first-class graph nodes.
6.  **Final Propagation**: Completes the hierarchy for all remaining symbols (including synthetics).
7.  **Lexical Fallback**: The "last resort" geometric scan for symbols that lack implementation bodies entirely (Fields, Variables).
8.  **Metadata Overlay**: Final enrichment with Type Aliases, Macros, and Static Call relations.

### 2.3. Rationale for Separation of Concerns
A fundamental design choice in this module is the **separation of Matching from Propagation**. 
*   Matching passes (Tiers 1, 2, 3) focus purely on **Identity**: "Is Clangd Symbol A the same as Parser Span B?" 
*   Propagation passes (`_assign_sym_parent...`) focus purely on **Hierarchy**: "Now that we know A is B, update its children."
This separation prevents race conditions and ensures that the matching logic operates on a stable, predictable symbol table.


## 2.1. Step 1: Preliminary Index-Based Hierarchy

Before any implementation data (SourceSpans) is matched, the provider attempts to build the symbol hierarchy using the information already present in the Clangd index. This provides a baseline hierarchy that subsequent passes can build upon.

### 2.1.1. Reference Container Resolution (`_assign_parent_ids_from_symbol_ref_container`)
Clangd's index records "References" for every symbol. Some of these references are marked as `Definition` or `Declaration` and contain a `container_id`.
*   **The Logic**: The provider scans all references for a symbol. If it finds a reference that is a definition/declaration (not just a usage) and has a valid `container_id`, it assumes that container is the semantic parent.
*   **Refinement - The Namespace Guard**: A critical nuance in this pass is the handling of Namespaces. In large projects, a member (like a Method) might be reported as being contained in a Namespace if its actual parent class is anonymous. 
    *   *The Rule*: The provider explicitly blocks assigning a `Namespace` as the parent of member-level kinds (Fields, Methods, EnumConstants). 
    *   *Rationale*: Namespaces should only contain top-level entities (Classes, Functions, other Namespaces). If a Method appears to be in a Namespace, it's usually because the intervening Class is anonymous. We defer such assignments to later passes that can correctly identify the anonymous parent.

### 2.1.2. Scope Bridging (`_infer_parent_ids_from_scope`)
Many symbols in the index lack explicit container references but possess a `scope` string (e.g., `MyNamespace::MyClass::`). The provider uses this string as a semantic bridge.

#### **The Reconciliation Algorithm**
1.  **Global Qualified Name Map**: The provider first builds a map of every scope-defining symbol (Namespace, Class, Struct, Union, Enum). The key is the symbol's fully qualified name (e.g., `sym.scope + sym.name + sym.template_specialization_args + "::"`).
2.  **Ambiguity Protection**: If multiple symbols share the same qualified name (rare in valid C++ but possible with complex macros), the key is marked as ambiguous (`None`) and no bridging is performed for that scope.
3.  **Parent Assignment**: For any symbol still missing a `parent_id`, the provider looks up its `scope` string in the map. If a unique match is found, that symbol's ID is assigned as the `parent_id`.

#### **Template Specialization Support**
A key detail in this pass is the inclusion of `template_specialization_args` in the qualified name. 
*   *Problem*: `Box<int>` and `Box<double>` both have the name `Box` and the same scope. 
*   *Solution*: By including the specialization arguments (e.g., `<int>`) in the bridge key, the provider ensures that members of `Box<int>` link to the correct specialization ID rather than colliding with the generic template.

### 2.1.3. Impact on Downstream Tiers
Establishing these links early is vital for **Tier 3 (Semantic Fallback)**. Since Tier 3 matching relies on the `(parent_id, name, kind)` context, these preliminary assignments "prime" the system to successfully match macro-expanded members in later stages.


## 2.2. Step 2: Multi-Tier Matching (Tiers 1 & 2)

This step is the core of the enrichment process. It pairs Clangd Symbols with the implementation "ground truth" (SourceSpans) provided by the AST parsers. Matching is tiered to prioritize semantic certainty while maintaining a robust fallback for coordinate-based tools.

### 2.2.1. Tier 1: Direct ID Matching (`_match_symbols_by_id`)
This is the primary and most authoritative matching mechanism.
*   **The Logic**: The provider iterates through all Clangd symbols and checks if their ID exists in the flattened map of `SourceSpans`.
*   **The Rationale**: Because `ClangParser` generates IDs derived from USRs (the same source Clangd uses), Tier 1 allows for an O(1) semantic match. If the IDs match, the symbols are identical by definition, regardless of their location in the file.
*   **Enrichment and Anchoring**: Upon a match, the symbol is enriched with its `body_location` and causality metadata. Crucially, its `definition` coordinates are **overwritten** with the implementation file path and coordinates. This anchoring ensures the graph's `path` property always points to where the source code actually resides.

### 2.2.2. Tier 2: Location-Based Matching (`_match_symbols_by_location`)
This pass acts as a fallback for symbols that failed Tier 1 (e.g., results from the syntactic `TreesitterParser` which lacks USRs, or rare cases where USRs diverge).
*   **Reconstructing Keys**: Since the implementation map is keyed by ID, the provider first builds a temporary lookup map where keys are coordinate-based strings: `kind::name::file_uri:line:col`.
*   **Expansion Coordinates**: These keys are derived from **Expansion Locations**. In a single Translation Unit, it is physically impossible for two distinct implementation entities of the same kind and name to occupy the same expansion point. This ensures that coordinate matching is unambiguous.
*   **Uniqueness Guard**: A match only proceeds if the reconstructed key maps to **exactly one** synthetic span and **exactly one** candidate Clangd symbol.

### 2.2.3. Intermediate Parent Propagation
A critical detail in the orchestration is that parent assignment runs **after** Tiers 1 & 2, but **before** Tier 3.
*   **Registry Entry**: When a symbol matches in Tier 1 or 2, its synthetic-to-index mapping is recorded in `synthetic_id_to_index_id`.
*   **Recording Intent**: The parentage found in the AST (the parser's "idea" of the parent) is recorded in the `sym_source_span_parent` queue.
*   **Propagation (`_assign_sym_parent_based_on_sourcespan_parent`)**: This pass translates those synthetic parent IDs into canonical index IDs. 
*   **Strategic Rationale**: By resolving these parent IDs early, the provider "primes" the symbols for **Tier 3 (Semantic Fallback)**, which requires an anchored `parent_id` to function. This progressive refinement allows parentage discovered via coordinates to enable the matching of children via context.


## 2.3. Step 3: Iterative Semantic Fallback (Tier 3)

Tier 3 (`_match_symbols_by_context_fallback`) is the final and most sophisticated matching phase. It is specifically designed to handle symbols generated by complex macro expansions (e.g., GTest's `TestBody()` or LLVM's `AMDGPU_EVENT_ENUM`) where Clangd and the AST parser disagree on both the USR and the source coordinates.

### 2.3.1. The Reconciliation Strategy: Semantic Context
When coordinate and ID matching fail, the provider relies on the **semantic context** of the symbol.
*   **The Logic**: If a Clangd symbol `sym` and a parser span `span` share the same **Parent**, **Name**, and **Kind**, they are likely the same entity.
*   **Context Key**: `(parent_id, name, kind)`.
*   **Rational**: In C++, while multiple symbols can have the same name globally, a specific `name` and `kind` combination is usually unique within its immediate scope (Namespace, Class, or Struct).

### 2.3.2. Iterative Unfolding (Progressive Propagation)
Matching complex hierarchies (e.g., a macro that generates a Namespace, a Class inside it, and a Method inside that) requires multiple passes.
*   **The Loop**: The method runs in a `while True` loop, continuing as long as new matches are found.
*   **ID Resolution**: In each iteration, the provider rebuilds the lookup table for the remaining spans. It resolves each span's `parent_id` via the `synthetic_id_to_index_id` registry.
*   **The Mechanism**:
    1.  *Round 1*: The top-level parent (e.g., the Class) is matched and its synthetic ID is mapped to its index ID.
    2.  *Round 2*: The children (e.g., the Methods) now have a "resolved" parent ID that matches the Clangd index. This allows them to meet the context key criteria and be matched.
*   **Result**: This progressively "unfolds" the lexical tree level-by-level, ensuring that even deeply nested, macro-generated entities are anchored correctly.

### 2.3.3. Bi-Directional Uniqueness (The Safety Guard)
Semantic matching carries a higher risk of false positives (e.g., overloaded methods with the same name). To maintain data integrity, the provider employs a strict **one-to-one mapping rule**:
1.  **Candidate Symbol Grouping**: All unmatched Clangd symbols are grouped by their context key. If multiple symbols share the same `(parent_id, name, kind)`, that context is marked as ambiguous and excluded.
2.  **Candidate Span Grouping**: All unmatched parser spans are similarly grouped. If multiple spans share the same context, that context is also excluded.
3.  **The Match**: A pairing is only performed if **exactly one** symbol and **exactly one** span correspond to the context key.

### 2.3.4. Anchoring to Implementation
Consistent with Tier 1, any symbol matched via semantic fallback is immediately anchored to the implementation file. The symbol's `definition` is forced to the parser's coordinates, ensuring that downstream AI summary agents read from the actual source code rather than a macro definition in a header.

### 2.3.5. Final Global Propagation
After Tier 3 concludes and no more matches can be made, any remaining spans are converted into **Synthetic Symbols**. A final propagation step (`_assign_sym_parent...`) ensures that Clangd-indexed symbols can correctly link to these new synthetic parents, completing the final connected hierarchy of the graph.


## 2.4. Step 4: Lexical Scope Fallback

This step (`_assign_parent_ids_lexically`) is the final hierarchy fallback for symbols that are still missing a `parent_id` after all matching tiers and propagation passes. This pass is largely just for santity checking, because all the symbols that need a `parent_id` should have it assigned through preceding passes, unless they are intentionally skipped. For example, `TypeAlias` symbols will be processed in later pass separately. Even symbols like class fields, or pure virtual functions that may not have corresponding source spans should have `parent_id` assigned too. Although the source spans have been created for AST nodes that have a "body", the `parend_id` is mainly assigned based on symbol's reference container field, etc., which cover the "no body" symbols too. After we added the AST node span creation for variables, their `parent_id` if exists will be accurately provided by the span. 

This pass is primarily designed for the extremely rare cases (should be zero cases) that exist in the Clangd index but have no matching spans in the AST and at the same time the Clangd index has some inconsistencies in the symbol's parent relationship. We log all those cases that fail this pass to help us identify and fix the root cause. 

This pass was needed previously when we have not yet implemented AST node span creation for all symbols. Ultimately, our AST parsed spans provide the ground of truth of the symbols. For symbols without a span, the lexical scoping is only a fallback mechanism that may not be always reliable. 

### 2.4.1. Strategy for "Bodyless" Symbols
Entities like class fields (`int x;`), static properties, and pure virtual functions exist as declarations but lack a "body" span.
*   **The Problem**: Since they have no body, they are never matched in Tiers 1-1.5. 
*   **Geometric Containment**: For these symbols, the provider performs a spatial scan:
    1.  It creates a "dummy" 1-character span at the symbol's `name_location`.
    2.  It uses `_find_innermost_container` to scan all actual source spans in the same file. This is unreliable because the field might be generated via a macro expansion, and/or by including another file, and/or defined as specialized class member (with class specialization).
    3.  It identifies the physically smallest span that **fully encloses** the dummy coordinates. 
*   **Result**: This correctly identifies the parent (e.g., a Class or Struct) based on spatial geometry, linking the bodyless member to its lexical container.

### 2.4.2. Advanced Fallback: USR-Bridging for Enums
A critical nuance handles anonymous enums where constants are defined in separate files (e.g., using `#include "constants.def"` inside the enum body).
*   **The Problem**: Lexical containment fails because the `EnumConstant` coordinates are in a different file than the `Enum` declaration. 
*   **The Solution**: Clangd populates the `type` property of an `EnumConstant` with the parent enum's USR (e.g., `c:$@N@MyEnum@Ea@file_enum0`).
*   **The Logic**: 
    1.  The provider extracts this USR from the constant symbol.
    2.  It sanitizes the string (stripping the extra `$`).
    3.  It hashes the cleaned USR to mathematically derive the parent ID.
    4.  If that ID matches a synthetic enum symbol created in earlier passes, the link is established.
*   **Impact**: This is the most robust way to bridge macro-expanded or included constants back to their semantic parents, transcending physical file boundaries.

### 2.4.3. Sanity Check for "With Body" Symbols
This pass also contains a sanity-check block for symbols that *should* have had bodies (Methods, Functions). 
*   **Rationale**: If a method reaches this pass without a parent, something in the previous tiers failed. 
*   **Identity Pluralism**: The block implements a dual lookup: first trying to find the parent by `sym.id` (Semantic), then falling back to a coordinate-based loop (Lexical). 
*   **Goal**: This ensures that even if direct matching failed for an obscure edge case, the symbol is still linked to its parent, preserving graph connectivity.

### 2.4.4. Circular Reference Prevention
At the end of parent resolution, the provider performs a critical safety check: `assert parent_id != sym.id`. 
*   **Rationale**: In complex template specializations or recursive macro expansions, it is theoretically possible for a tool to report a symbol as its own parent. Detecting and blocking these prevents infinite loops during graph traversal.


## 2.5. Step 5: Semantic Ownership and Final Enrichment

After the core matching and synthesis passes are complete, the provider performs a series of high-level enrichment steps to capture complex relationships like semantic ownership, type aliasing, and cross-file macro definitions.

### 2.5.1. Semantic Ownership Pass (`_assign_parent_ids_from_member_lists`)
This is the "ultimate truth" pass for hierarchy building. It addresses the limitation of coordinate-based containment, particularly for **macro-injected members** (e.g., methods or constants defined by a macro outside the parent's file).

*   **The Logic**: The parser (during AST traversal) records a `member_ids` list for every composite type. This list contains the USR-derived IDs of its semantic children.
*   **The Resolution**:
    1.  The provider iterates through all `SourceSpans`.
    2.  It resolves the parent's ID to its canonical graph ID via the truth bridge.
    3.  For each ID in `member_ids`, it finds the corresponding symbol in the graph.
    4.  If that symbol is missing a `parent_id`, the provider assigns the parent ID.
*   **Rational**: This pass is authoritative because it uses the compiler's own semantic ownership model. If the AST says a Class owns a Method, they are linked, regardless of where they appear in the source code.

### 2.5.2. TypeAlias Enrichment (`_enrich_with_type_alias_data`)
Type aliases (`typedef`, `using`) require specific handling because they act as "pointers" to other types in the graph.
*   **Aliasee Resolution**: The provider uses the `synthetic_id_to_index_id` registry to resolve the `aliased_type_id`. This ensures that if an alias points to a synthetic anonymous struct, the link correctly targets the canonical ID of that struct.
*   **Synthesis**: Like generic nodes, any `TypeAliasSpan` not found in the Clangd index is synthesized as a first-class Symbol. This ensures that the graph captures the full vocabulary of the codebase.

### 2.5.3. Macro Discovery (`_discover_macros`)
Macros are preprocessor entities and are never indexed as symbols by Clangd. 
*   **First-Class Nodes**: The provider iterates through all `macro_spans` captured by the parser and injects them as `Macro` symbols.
*   **Metadata**: It preserves the full `macro_definition` (the source code of the `#define`) and the `is_function_like` flag, allowing AI agents to distinguish between constant-like and logic-like macros.

### 2.5.4. Static Call Injection (`_enrich_with_static_calls`)
Clangd's index is often incomplete regarding internal-linkage function calls (e.g., `static void foo()`). 
*   **The Bridge**: The parser captures these calls during AST traversal as `(caller_usr, callee_usr)` pairs.
*   **The Injection**: The provider injects these as synthetic `Reference` objects into the symbols. 
*   **Result**: This allows the Call Graph Builder to establish `[:CALLS]` relationships that would otherwise be missing from the index, providing a significantly more accurate view of the execution flow.

### 2.5.5. Memory Management and Finality
Upon completion of all passes, the provider:
1.  Calculates the final `assigned_parent_count` by summing the results of all tiers.
2.  Explicitly deletes its internal registries (`all_remaining_spans`, `synthetic_id_to_index_id`).
3.  Triggers `gc.collect()`.
*   **Rational**: This prevents memory bottlenecks when the pipeline transitions to the RAG generation phase, which can be very memory-intensive.
