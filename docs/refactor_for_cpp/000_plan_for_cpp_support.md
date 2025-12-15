# Schema Specification for C/C++ Support

This document outlines the Neo4j graph schema designed to represent both C and C++ source code. The schema provides a rich, semantic understanding of the codebase, with explicit support for object-oriented constructs.

This is the original schema that only supports C language projects:

```
Node Labels and Properties:
  (DATA_STRUCTURE)   
    file_path: STRING
    has_definition: BOOLEAN
    id: STRING (INDEXED) (UNIQUE)
    kind: STRING  # struct, enum, union
    language: STRING
    name: STRING
    name_location: LIST
    path: STRING
    scope: STRING
  (FILE)
    name: STRING
    path: STRING (INDEXED) (UNIQUE)
    summary: STRING
    summaryEmbedding: LIST
  (FOLDER)
    name: STRING
    path: STRING (INDEXED) (UNIQUE)
    summary: STRING
    summaryEmbedding: LIST
  (FUNCTION)
    body_location: LIST
    codeSummary: STRING
    file_path: STRING
    has_definition: BOOLEAN
    id: STRING (INDEXED) (UNIQUE)
    kind: STRING
    language: STRING
    name: STRING
    name_location: LIST
    path: STRING
    return_type: STRING
    scope: STRING
    signature: STRING
    summary: STRING
    summaryEmbedding: LIST
    type: STRING
  (PROJECT)
    commit_hash: STRING
    name: STRING
    path: STRING
    summary: STRING
    summaryEmbedding: LIST

Relationships:
  (FILE) -[:DEFINES]-> (DATA_STRUCTURE|FUNCTION)
  (FILE) -[:INCLUDES]-> (FILE)
  (FOLDER) -[:CONTAINS]-> (FILE)
  (FUNCTION) -[:CALLS]-> (FUNCTION)
  (PROJECT) -[:CONTAINS]-> (FOLDER)

Property Explanations:
  body_location: Entity's body location in the file [start_line, start_column, end_line, end_column].
  codeSummary: LLM-generated summary of the code's literal function.
  file_path: Absolute path to the file containing the symbol.
  has_definition: Boolean indicating if a symbol has a definition.
  id: Unique identifier for the node.
  kind: Type of symbol (e.g., Function, Struct, Variable).
  language: Programming language of the source code.
  name: Name of the entity (e.g., function name, file name).
  name_location: Entity's name location in the file [line, column].
  path: Relative path to the project root if it is within the project folder. Otherwise, it is absolute path, including the PROJECT node
  return_type: Return type of a function.
  scope: Visibility scope (e.g., global, static).
  signature: Full signature of a function.
  summary: LLM-generated context-aware summary of the node's purpose.
  summaryEmbedding: Vector embedding of the 'summary' for similarity search.
  type: Data type of the symbol (e.g., int, void*).
```
For C++, this is the additional kinds of symbols in a small C++ project extracted by clangd-indexer. I don't necessarily support all of them for my purpose.
```
  Kind:            Class
  Kind:            Constructor
  Kind:            ConversionFunction
  Kind:            Destructor
  Kind:            Enum
  Kind:            EnumConstant
  Kind:            Field
  Kind:            Function
  Kind:            InstanceMethod
  Kind:            Namespace
  Kind:            StaticMethod
  Kind:            StaticProperty
  Kind:            Struct
  Kind:            TypeAlias
  Kind:            Union
  Kind:            Using
  Kind:            Variable
```


# New planned schema

## Node Types

### Infrastructure Nodes
*   **(PROJECT)**: Represents the entire source code project.
    *   Properties: `name`, `path`, `commit_hash`.
*   **(FOLDER)**: Represents a directory.
    *   Properties: `name`, `path` (unique).
*   **(FILE)**: Represents a single source or header file.
    *   Properties: `name`, `path` (unique).

### Logical & Structural Nodes
*   **(NAMESPACE)**: Represents a C++ namespace, forming a logical hierarchy.
    *   Properties: `name`, `qualified_name` (unique).
*   **(TYPE_ALIAS)**: Represents a C++ type alias created with `using`.
    *   Properties: `name`, `file_path`.

### Data & Type Definition Nodes
*   **(DATA_STRUCTURE)**: Represents enum/union data structures and C struct.
    *   Properties: `id` (unique), `name`, `kind` ('struct', 'enum', 'union'), `file_path`, `scope`, `lang`.
*   **(CLASS_STRUCTURE)**: Represents a C++ `class` or C++ `struct`.
    *   Properties: `id` (unique), `name`, `kind` ('class', 'struct'), `file_path`, `scope`, `lang`

### Callable & Member Nodes
*   **(FUNCTION)**: Represents a standalone C-style or namespace-level function.
    *   Properties: `id` (unique), `name`, `signature`, `return_type`, `file_path`, `scope`, `body_location`.
*   **(METHOD)**: Represents a function bound to a `CLASS_STRUCTURE`.
    *   Properties: `id` (unique), `name`, `signature`, `return_type`, `file_path`, `scope`, `body_location`, `kind` ('Constructor', 'Destructor', etc.), `is_static`, `is_virtual`, `is_const`, `access` ('public', 'private', 'protected').
*   **(FIELD)**: Represents a data member of a `CLASS_STRUCTURE` or `DATA_STRUCTURE`.
    *   Properties: `name`, `type`, `is_static`, `access`.
*   **(VARIABLE)**: Represents a global or namespace-level variable.
    *   Properties: `name`, `type`, `file_path`, `scope`.

## Relationship Types

### Structural & Dependency Relationships
*   `(PROJECT) -[:CONTAINS]-> (FOLDER)`
*   `(FOLDER) -[:CONTAINS]-> (FILE)`
*   `(FILE) -[:INCLUDES]-> (FILE)`: Represents `#include` directives.
*   `(FILE) -[:DECLARES]-> (NAMESPACE)`: Links a file to the namespaces it contributes to (M:N).
*   `(FILE) -[:DEFINES]-> (Symbol)`: Generic relationship linking a file to the symbols it defines (e.g., `FUNCTION`, `CLASS_STRUCTURE`, `VARIABLE`).

### Logical & Inheritance Relationships
*   `(NAMESPACE) -[:CONTAINS]-> (NAMESPACE | FUNCTION | CLASS_STRUCTURE | ...)`: Forms the logical code hierarchy.
*   `(TYPE_ALIAS) -[:ALIASES_TYPE]-> (DATA_STRUCTURE | CLASS_STRUCTURE)`: Links an alias to its underlying type.
*   `(CLASS_STRUCTURE) -[:INHERITS {access: STRING}]-> (CLASS_STRUCTURE)`: Represents class inheritance.

### Member & Call Relationships
*   `(CLASS_STRUCTURE) -[:HAS_METHOD]-> (METHOD)`
*   `(CLASS_STRUCTURE) -[:HAS_FIELD]-> (FIELD)`
*   `(DATA_STRUCTURE) -[:HAS_FIELD]-> (FIELD)`
*   `(FUNCTION | METHOD) -[:CALLS]-> (FUNCTION | METHOD)`: Represents a function or method call.

## Key Design Decisions

1.  **`Struct` Handling**: A `struct` will be ingested as a `CLASS_STRUCTURE` in C++;  will be a `DATA_STRUCTURE` in C.
2.  **Namespace Modeling**: Namespaces are modeled as explicit `NAMESPACE` nodes to allow for powerful hierarchical queries. The M:N relationship between files and namespaces is handled via a `DECLARES` relationship.
3.  **`scope` vs. `qualified_name`**: The `scope` property will be stored on symbols for query convenience. The full `qualified_name` will **not** be stored on every symbol to reduce data redundancy and will be constructed at query time (`scope` + `::` + `name`). `NAMESPACE` nodes are an exception and will store a `qualified_name`.
4.  **Relationship Specificity**:
    *   For class/struct members, specific relationships (`HAS_METHOD`, `HAS_FIELD`) are used for semantic clarity and query performance.
    *   For file-level definitions, a generic `DEFINES` relationship is used to avoid a "relationship type explosion".
5.  **Granularity**: To keep the graph focused on high-level structure, certain implementation details are **not** modeled as nodes. This includes local variables within functions and `EnumConstant`s within `enum`s. Their definitions can be found via the `body_location` of their parent node.
6.  **`using` Directives**: These are not modeled, as their name resolution effects are already reflected in the `CALLS` graph provided by `clangd`. `TYPE_ALIAS` (e.g., `using T = ...`) is modeled as it creates a persistent symbol.
