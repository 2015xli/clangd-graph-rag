Node Properties:
  (CLASS_STRUCTURE)
    body_location: LIST
    file_path: STRING
    has_definition: BOOLEAN
    id: STRING (INDEXED) (UNIQUE)
    kind: STRING
    language: STRING
    name: STRING
    name_location: LIST
    node_label: STRING
    path: STRING
    scope: STRING
    summary: STRING
    summaryEmbedding: LIST
  (DATA_STRUCTURE)
    body_location: LIST
    file_path: STRING
    has_definition: BOOLEAN
    id: STRING (INDEXED) (UNIQUE)
    kind: STRING
    language: STRING
    name: STRING
    name_location: LIST
    node_label: STRING
    path: STRING
    scope: STRING
  (FIELD)
    file_path: STRING
    has_definition: BOOLEAN
    id: STRING (INDEXED) (UNIQUE)
    is_static: BOOLEAN
    kind: STRING
    language: STRING
    name: STRING
    name_location: LIST
    node_label: STRING
    path: STRING
    scope: STRING
    type: STRING
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
    node_label: STRING
    path: STRING
    return_type: STRING
    scope: STRING
    signature: STRING
    summary: STRING
    summaryEmbedding: LIST
    type: STRING
  (METHOD)
    body_location: LIST
    codeSummary: STRING
    file_path: STRING
    has_definition: BOOLEAN
    id: STRING (INDEXED) (UNIQUE)
    kind: STRING
    language: STRING
    name: STRING
    name_location: LIST
    node_label: STRING
    path: STRING
    return_type: STRING
    scope: STRING
    signature: STRING
    summary: STRING
    summaryEmbedding: LIST
    type: STRING
  (NAMESPACE)
    name: STRING
    qualified_name: STRING (INDEXED) (UNIQUE)
  (PROJECT)
    commit_hash: STRING
    name: STRING
    path: STRING
    summary: STRING
    summaryEmbedding: LIST
  (VARIABLE)
    file_path: STRING
    has_definition: BOOLEAN
    id: STRING (INDEXED) (UNIQUE)
    kind: STRING
    language: STRING
    name: STRING
    name_location: LIST
    node_label: STRING
    path: STRING
    scope: STRING
    type: STRING

Relationships:
  (CLASS_STRUCTURE) -[:HAS_FIELD]-> (FIELD)
  (CLASS_STRUCTURE) -[:HAS_METHOD]-> (METHOD)
  (CLASS_STRUCTURE) -[:INHERITS]-> (CLASS_STRUCTURE)
  (DATA_STRUCTURE) -[:HAS_FIELD]-> (FIELD)
  (FILE) -[:DECLARES]-> (NAMESPACE)
  (FILE) -[:DEFINES]-> (CLASS_STRUCTURE|DATA_STRUCTURE|FUNCTION|VARIABLE)
  (FILE) -[:INCLUDES]-> (FILE)
  (FOLDER) -[:CONTAINS]-> (FILE|FOLDER)
  (FUNCTION) -[:CALLS]-> (FUNCTION|METHOD)
  (METHOD) -[:CALLS]-> (FUNCTION|METHOD)
  (METHOD) -[:OVERRIDDEN_BY]-> (METHOD)
  (NAMESPACE) -[:CONTAINS]-> (CLASS_STRUCTURE|DATA_STRUCTURE|FUNCTION|NAMESPACE)
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
