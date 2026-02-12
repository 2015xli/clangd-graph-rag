# 011: TypeAlias Enrichment - Detailed Implementation Plan

This document details the implementation steps for the TypeAlias enrichment stage: enriching existing `Symbol` objects and creating synthetic ones based on the `TypeAliasSpan` data extracted by the parser.

---

### Objective

To modify `source_span_provider.py` to integrate the `TypeAliasSpan` data, ensuring that all relevant type alias information is present in the `SymbolParser`'s collection of `Symbol` objects before ingestion into Neo4j.

### File: `source_span_provider.py`

#### 1. Update `SourceSpanProvider.__init__`

*   **Access `TypeAliasSpan`s:**
    *   Modify the `__init__` method to retrieve the `TypeAliasSpan` objects from the `compilation_manager`.
    *   Add:
        *   `self.type_alias_spans: Dict[str, TypeAliasSpan] = compilation_manager.get_type_alias_spans()`

#### 2. Modify `SourceSpanProvider.enrich_symbols_with_span`

*   **New Matching and Enrichment Logic for `TypeAlias`:**
    *   **Iterate and Match Existing `Symbol`s:**
        *   Iterate through `self.symbol_parser.symbols.values()`.
        *   If `sym.kind == 'TypeAlias'`:
            *   Use `sym.id` (which is USR-derived) to look up a matching `TypeAliasSpan` in `self.type_alias_spans`.
            *   If a match (`matched_tas`) is found:
                *   **Enrich `Symbol`:** Update the `sym` object with the detailed alias information:
                    *   `sym.aliased_canonical_spelling = matched_tas.aliased_canonical_spelling`
                    *   `sym.aliased_type_id = matched_tas.aliased_type_id`
                    *   `sym.aliased_type_kind = matched_tas.aliased_type_kind`
                    *   `sym.parent_id = matched_tas.parent_id`
                    *   `sym.scope = matched_tas.scope`
                    *   `sym.body_location = matched_tas.body_location`
                *   Remove `matched_tas` from `self.type_alias_spans` to mark it as processed.
    *   **Create Synthetic `Symbol`s for Unmatched `TypeAliasSpan`s:**
        *   After the matching loop, iterate through the remaining `TypeAliasSpan`s in `self.type_alias_spans` (these are aliases found by the parser but not present in the clangd YAML).
        *   For each `unmatched_tas`:
            *   Create a new `Symbol` object using the data from `unmatched_tas`.
            *   Set `new_sym.kind = 'TypeAlias'`.
            *   Set `new_sym.id = unmatched_tas.id`.
            *   Set `new_sym.name = unmatched_tas.name`.
            *   Set `new_sym.declaration = Location.from_relative_location(unmatched_tas.name_location, file_uri=unmatched_tas.file_uri)`.
            *   Set `new_sym.definition = Location.from_relative_location(unmatched_tas.name_location, file_uri=unmatched_tas.file_uri)`.
            *   Set `new_sym.aliased_canonical_spelling = unmatched_tas.aliased_canonical_spelling`.
            *   Set `new_sym.aliased_type_id = unmatched_tas.aliased_type_id`.
            *   Set `new_sym.aliased_type_kind = unmatched_tas.aliased_type_kind`.
            *   Set `new_sym.parent_id = unmatched_tas.parent_id`.
            *   Set `new_sym.scope = unmatched_tas.scope`.
            *   Set `new_sym.body_location = unmatched_tas.body_location`.
            *   Add this `new_sym` to `self.symbol_parser.symbols`.
            *   Update `synthetic_id_to_index_id` mapping if necessary.

#### 3. Update `_create_synthetic_symbol` (or similar helper)

*   The `_create_synthetic_symbol` helper in `source_span_provider.py` will be updated to handle `TypeAliasSpan` objects.
*   It will construct a `Symbol` object, setting its `kind` to `'TypeAlias'`, and populating the `aliased_canonical_spelling`, `aliased_type_id`, `aliased_type_kind`, `parent_id`, `scope`, and `body_location` properties from the `TypeAliasSpan`.

#### 4. Modularization Plan for `SourceSpanProvider`

The `SourceSpanProvider` class, particularly its `enrich_symbols_with_span` method, currently orchestrates several distinct enrichment passes. To improve modularity, readability, and maintainability, the `enrich_symbols_with_span` method will be refactored into a series of smaller, more focused private methods. Each method will be responsible for a specific aspect of the symbol enrichment process.

**Proposed Refactoring:**

1.  **`enrich_symbols_with_span(self)` (Orchestrator Method):**
    *   This method will become the main orchestrator, calling the new private methods in a logical sequence.
    *   It will manage the overall flow and logging of the enrichment process.

2.  **`_filter_symbols_by_project_path(self)`:**
    *   **Purpose:** Extracts the initial filtering logic that removes symbols whose definitions or declarations are outside the project path.
    *   **Current Location:** Currently at the beginning of `enrich_symbols_with_span`.

3.  **`_match_and_synthesize_source_spans(self)`:**
    *   **Purpose:** Encapsulates the logic for matching existing `Symbol` objects with `SourceSpan` data and creating synthetic `Symbol` objects for unmatched `SourceSpan`s (e.g., anonymous structures).
    *   **Current Location:** Covers the logic described as "Pass 1" and "Pass 2" in the existing `enrich_symbols_with_span` method.
    *   **Inputs:** `self.symbol_parser.symbols`, `self.compilation_manager.get_source_spans()`.
    *   **Outputs:** Updates `self.symbol_parser.symbols` in-place.

4.  **`_assign_parent_ids_lexically(self)`:**
    *   **Purpose:** Encapsulates the logic for assigning `parent_id`s based on lexical scope for symbols that do not already have a `parent_id` from `clangd` references.
    *   **Current Location:** Covers the logic described as "Pass 3" in the existing `enrich_symbols_with_span` method.
    *   **Inputs:** `self.symbol_parser.symbols`, `self.compilation_manager.get_source_spans()`.
    *   **Outputs:** Updates `self.symbol_parser.symbols` in-place.

5.  **`_enrich_with_type_alias_data(self)` (New Method):**
    *   **Purpose:** This new method will specifically handle the matching of existing `TypeAlias` `Symbol`s with `TypeAliasSpan` data and the creation of synthetic `TypeAlias` `Symbol`s for unmatched `TypeAliasSpan`s.
    *   **Current Location:** This is the new logic being designed in `011-typealias-enrichment.md`.
    *   **Inputs:** `self.symbol_parser.symbols`, `self.compilation_manager.get_type_alias_spans()`.
    *   **Outputs:** Updates `self.symbol_parser.symbols` in-place.

6.  **`_enrich_with_static_calls(self)`:**
    *   **Purpose:** This method already exists and will remain as is, responsible for injecting static call relations into `Symbol` objects.
    *   **Current Location:** Already a separate method in `source_span_provider.py`.

**Revised `enrich_symbols_with_span` Flow:**

```python
def enrich_symbols_with_span(self):
    if not self.symbol_parser:
        logger.warning("No SymbolParser provided; cannot enrich symbols.")
        return

    self._filter_symbols_by_project_path()
    self._match_and_synthesize_source_spans()
    self._assign_parent_ids_lexically()
    self._enrich_with_type_alias_data() # New pass for TypeAlias
    self._enrich_with_static_calls()
    # Cleanup and logging
```

This modular approach will make the `SourceSpanProvider` more organized and easier to extend in the future (e.g., for Variable/Field enrichment).

---

### Considerations

*   **Keying Consistency:** It is paramount that the `make_usr_derived_id` logic used in `ClangParser` for `TypeAliasSpan` and in `SourceSpanProvider` for matching `Symbol` objects is identical to ensure correct matching.
*   **Filtering External Symbols:** The initial filtering of symbols to be within the project path (already present in `enrich_symbols_with_span`) should apply to `TypeAlias` symbols as well, preventing external aliases from being processed.
*   **Memory Management:** Be mindful of memory usage, especially when dealing with large numbers of `TypeAliasSpan` objects and the creation of new `Symbol` objects.
*   **`Location.from_relative_location`:** This helper function has been added to `clangd_index_yaml_parser.py` to facilitate the creation of `Location` objects from `RelativeLocation` and `file_uri`.

### 5. `synthetic_id_to_index_id` Mapping Table

The `synthetic_id_to_index_id` dictionary is a crucial internal mapping used throughout the enrichment process. Its purpose is to map any synthetic ID generated by the `CompilationParser` (from `SourceSpan`s or `TypeAliasSpan`s) to the *canonical ID* of the corresponding `Symbol` object that ends up in `self.symbol_parser.symbols`.

*   **How it's Built:**
    *   When an existing `Symbol` (from `self.symbol_parser`) is matched with a `SourceSpan` or `TypeAliasSpan`, the span's synthetic ID is mapped to the `Symbol.id` (which could be a `clangd` USR).
    *   When a new synthetic `Symbol` is created from an unmatched `SourceSpan` or `TypeAliasSpan`, the span's synthetic ID (which becomes the new `Symbol.id`) is mapped to itself.
*   **How it's Used:**
    *   For `TypeAlias` `Symbol`s (both existing and synthetic), their `parent_id` and `aliased_type_id` properties (which are initially synthetic IDs from the `TypeAliasSpan`) will be *resolved* using this `synthetic_id_to_index_id` map. This ensures that these IDs consistently point to the correct, canonical `Symbol.id`s, regardless of their origin.

This centralized mapping ensures that all inter-symbol references consistently point to the correct, canonical `Symbol.id`s, regardless of their origin.

---
