# 010: Parser Enhancement - Detailed Implementation Plan

This document details the implementation steps for the first stage of TypeAlias integration: enhancing the `ClangParser` to extract comprehensive alias information from source code. We will not touch anything related to Field/Variables before we are completely done with TypeAlias processing (stage 010,011,012). We intentionally decouple the processing to make the design modular. More importantly, we will only decide whether to process Field/Variables at all after we are done with TypeAlias whole processing.

---

### Objective

To modify `compilation_parser.py` to accurately identify `typedef` and `using` declarations and extract their aliaser and aliasee information.

### File: `compilation_parser.py`

#### 1. Define New Data Structures

*   **`TypeAliasSpan` Dataclass:**
    *   Create a new `dataclass` named `TypeAliasSpan` (similar to `SourceSpan`) to store the extracted information for each type alias.
    *   **Properties:**
        *   `id: str`: **USR-derived ID for the aliaser.** This will be the primary key for `TypeAliasSpan`s.
        *   `file_uri: str`: The file URI where this `TypeAliasSpan` was found. (For traceability, not for uniqueness).
        *   `lang: str`: The language of the alias (e.g., "Cpp", "C").
        *   `name: str`: The name of the aliaser (e.g., `MyInt`).
        *   `name_location: RelativeLocation`: The location of the aliaser's name.
        *   `body_location: RelativeLocation`: The extent of the entire alias declaration statement.
        *   `aliased_canonical_spelling: str`: The canonical string representation of the underlying type (e.g., `"int"`, `"std::vector<int>"`, `"struct MyStruct"`). This will be the `name` for the `:TYPE_EXPRESSION` node if the aliased type is a type expression.
        *   `aliased_type_id: Optional[str]`: The ID of the aliased type. This will be **USR-derived if the aliasee is another TypeAlias**, and **synthetic for all other aliasees** (`:CLASS_STRUCTURE`, `:DATA_STRUCTURE`, `:TYPE_EXPRESSION`, anonymous structures).
        *   `aliased_type_kind: Optional[str]`: The kind of the aliased type (e.g., "Class", "Struct", "TypeExpression", "TypeAlias").
        *   `is_aliasee_definition: bool`: Indicates if the aliased type's declaration is also a definition (e.g., `struct S { ... };` vs `struct S;`). Used for reconciliation.
        *   `scope: str`: The string representation of the parent scope (e.g., `MyNamespace::MyClass::`).
        *   `parent_id: Optional[str]`: The ID of the parent scope (class, struct, namespace, or file).
        

#### 2. Modify `_ClangWorkerImpl` Class

*   **Extend `NODE_KIND_TYPE_ALIAS`:**
    *   Add `clang.cindex.CursorKind.TYPE_ALIAS_DECL` and `clang.cindex.CursorKind.TYPEDEF_DECL` to the `NODE_KIND_TYPE_ALIAS` set in `ClangParser`. This will ensure `_walk_ast` processes these cursors.
*   **New Method: `_process_type_alias_node(self, node, file_name)`:**
    *   This method will be called by `_walk_ast` when an alias cursor is encountered.
    *   **Scope Filtering:**
        *   Check the `node.semantic_parent` to determine the alias's scope.
        *   **Crucially:** Only process aliases at global, namespace, or class/struct scopes. Ignore function-local aliases.
    *   **Extract Aliaser Info:**
        *   Get `name` from `node.spelling`.
        *   Get `name_location` using `_get_symbol_name_location(node)`.
        *   Get `body_location` from `node.extent`.
        *   **Generate `id` using `CompilationParser.make_usr_derived_id(node.get_usr())`.**
        *   Get `file_uri` from `file_name`.
    *   **Resolve Aliasee Info:**
        *   Get the underlying type: `underlying_type = node.underlying_typedef_type`.
        *   Get `aliased_canonical_spelling`: `aliased_canonical_spelling = underlying_type.get_canonical().spelling`.
        *   **Determine `aliased_type_id` and `aliased_type_kind`:**
            *   Initialize `aliased_type_id = None` and `aliased_type_kind = None`.
            *   Get the declaration cursor of the underlying type: `aliasee_decl_cursor = underlying_type.get_declaration()`.
            *   **Determine `is_aliasee_definition`:** `is_aliasee_definition = aliasee_decl_cursor.is_definition() if aliasee_decl_cursor else False`.
            *   If `aliasee_decl_cursor` is a `TYPE_ALIAS_DECL` or `TYPEDEF_DECL`:
                *   **Generate `aliased_type_id` using `CompilationParser.make_usr_derived_id(aliasee_decl_cursor.get_usr())`.**
                *   Set `aliased_type_kind` to "TypeAlias".
            *   Else (if `aliasee_decl_cursor` is `None` or an unsupported kind, or a user-defined type):
                *   **Generate `aliased_type_id` using `CompilationParser.make_synthetic_id`** based on a key derived from the `aliased_canonical_spelling` (e.g., `type://<hash_of_spelling>`) or the aliasee's name and location if it's a named user-defined type.
                *   Set `aliased_type_kind` to the corresponding kind (e.g., "Class", "Struct", "Enum", "Union", or "TypeExpression" if it's a generic type expression).
    *   **Determine `scope` and `parent_id`:**
        *   Get the semantic parent of the alias node: `semantic_parent = node.semantic_parent`.
        *   **Extract the fully qualified `scope` string:** This involves recursively traversing the `semantic_parent` chain up to the `TRANSLATION_UNIT` to build the complete scope string (e.g., `MyNamespace::MyClass::`). A helper function `_get_fully_qualified_scope(node)` will be added to `_ClangWorkerImpl` for this purpose.
        *   Get `parent_id` using `_get_parent_id(node)`.
    *   **Store `TypeAliasSpan`:** Create a `TypeAliasSpan` object and add it to a new dictionary within `_ClangWorkerImpl` (e.g., `self.type_alias_spans: Dict[str, TypeAliasSpan]`), keyed by the aliaser's USR-derived ID. Implement reconciliation logic: if an entry with the same ID already exists, keep the `TypeAliasSpan` whose `is_aliasee_definition` is `True`. If both or neither are definitions, randomly choose one.

#### 3. Update `ClangParser` Class

*   **New Static Method:** Add `make_usr_derived_id(usr: str) -> str` which will hash the USR to generate a consistent ID.
*   **New Property:** Add `self.type_alias_spans: Dict[str, TypeAliasSpan]` to the `__init__` method.
*   **Update `parse` Method:**
    *   The `_parallel_worker` function (and its `_parallel_worker` counterpart in the multiprocessing context) will now return a dictionary of results. The main `ClangParser.parse` method will collect and merge these `type_alias_spans` from all workers, applying the reconciliation logic.
*   **New Getter:** Add `get_type_alias_spans(self) -> Dict[str, TypeAliasSpan]` to return the collected alias spans.

#### 4. Update `CompilationManager` Class

*   **Update `_perform_parsing` Method:**
    *   Modify this method to return a dictionary of results.
*   **Update `CacheManager`:**
    *   Modify `CacheManager.save_git_cache` and `CacheManager.save_mtime_cache` to store the dictionary of results.
    *   Modify `CacheManager.find_and_load_git_cache` and `CacheManager.find_and_load_mtime_cache` to load the dictionary of results.
*   **New Getter:** Add `get_type_alias_spans(self) -> Dict[str, TypeAliasSpan]` to `CompilationManager` to expose the collected alias spans.

---

### Considerations

*   **External Headers:** Ensure that aliases and types originating from external headers (outside the project path) are filtered out, consistent with existing project-only filtering.
*   **Error Handling:** Implement robust error handling for cases where type resolution or ID generation fails.
*   **Qualified Name Construction:** The `qualified_name` for aliases will be constructed in the node ingestion phase by combining the `scope` string and the alias's `name`.
*   **`Location.from_relative_location` Helper:** A helper function will be needed in `clangd_index_yaml_parser.py` to convert `RelativeLocation` to `Location` for `declaration` and `definition` properties of new `Symbol` objects in the enrichment phase.

---
