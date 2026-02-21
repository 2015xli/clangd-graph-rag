# Macro Ingestion: Definition Nodes

This phase focuses on identifying and ingesting the ground-truth definitions of macros (`#define`) into the Neo4j graph using the project's established "Span-Provider" architecture.

### 1. Data Structures

#### 1.1 New `MacroSpan` (compilation_parser.py)
A carrier for macro metadata extracted during source parsing.
```python
@dataclass(frozen=True, slots=True)
class MacroSpan:
    id: str # Synthetic: hash(kind + name + file_uri + name_location)
    name: str
    lang: str
    file_uri: str
    name_location: RelativeLocation
    body_location: RelativeLocation
    is_function_like: bool
    macro_definition: str # The full textual directive: #define ...
```

#### 1.2 Update `Symbol` (clangd_index_yaml_parser.py)
Holds macro-specific properties for database ingestion.
```python
@dataclass
class Symbol:
    # ...
    is_macro_function_like: bool = False
    macro_definition: Optional[str] = None
    original_name: Optional[str] = None
    expanded_from_id: Optional[str] = None
```

### 2. Parsing (Clang Parser)

#### 2.1 Worker Implementation (`_ClangWorkerImpl`)
*   **Source Text Extraction**: A new helper `_get_source_text_for_extent` reads the raw file content between Clang locations.
*   **Macro Processing**: `_process_macro_definition` uses the cursor's `extent` to capture the full `#define` directive as a string.
*   **Kind-Aware ID**: Uses `CompilationParser.make_symbol_key(name, "Macro", file_uri, line, col)`. This prefix ensures macro IDs never collide with symbols of other types at the same location.

#### 2.2 Compilation Manager
*   Updated `CacheManager` to store and restore `macro_spans` in both Git-based and mtime-based cache files.

### 3. The Bridge (`SourceSpanProvider`)

The provider injects macros into the `SymbolParser` so they can be treated as first-class symbols.
*   **Method**: `_discover_macros()`
*   **Logic**: Converts `MacroSpan` to `Symbol` with `kind = "Macro"`. It transfers the `macro_definition` text and the `is_macro_function_like` flag.

### 4. Ingestion (`SymbolProcessor`)

*   **Node Mapping**: Maps the `Macro` kind to the `:MACRO` label in Neo4j.
*   **Property Ingestion**: Explicitly adds `macro_definition` and `is_function_like` to the properties written to the database.
*   **Relationship**: Ingests the `(FILE)-[:DEFINES]->(MACRO)` relationship using the standard defines logic.

### 5. Constraint Management (`Neo4jManager`)
*   Creates a unique constraint: `CREATE CONSTRAINT FOR (m:MACRO) REQUIRE m.id IS UNIQUE`.
