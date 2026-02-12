# High-Level Plan for TypeAlias Integration

**C/C++ types** C/C++ has basically two groups of data types: user-defined and non-user-defined. 
*   **Non-user-defined types**
    This includes **fundamental types** (sometimes called primitive types) like `int`, `float`, etc., and **derived types** like arrays, pointers, references, and function types.
*   **User-defined types**
    This includes **classes**, **structs**, **unions**, **enums**, and **type aliases** (using `typedef` and `using` keywords).

We have already implemented some support for **user-defined types** (`CLASS_STRUCTURE` and `DATA_STRUCTURE`) except for **type aliases**, which is important to understand the underlying types of variables and parameters that use aliases. This document outlines the high-level implementation plan for integrating C/C++ `TypeAlias` symbols (from `typedef` and `using` statements) into the code graph.


### Objective

To enrich the code graph with type alias information, enabling an AI agent to understand the underlying types of variables and parameters that use aliases. This involves identifying aliases, resolving what they point to, and representing this information as new nodes and relationships in Neo4j.

---

### High-Level Plan for Type Alias Integration

This plan incorporates the latest feedback and focuses on the "what" and "why" before we move to implementation.

**1. On Scoping and Discoverability**

*    1.1. We will only create `:TYPE_ALIAS` nodes for aliases defined at the global, namespace, and class/struct scopes. Function-local aliases will be ignored, since they are not accessible from outside of the function, while other kinds of aliases can be accessed with scope prefix.
    
*    1.2.  Every `:TYPE_ALIAS` node will have a `qualified_name` property (e.g., `my_namespace::MyClass::MyAlias`). This will be the primary lookup key for an agent. The node will also have a `name` property and `scope` property (e.g., `MyAlias` and `my_namespace::MyClass`). The `qualified_name` property is unique in the graph. It's `kind` property will be "TypeAlias". Dataclass Symbol does not need a `qualified_name` property, since the node's `qualified_name` can be constructed from the `name` and `scope` properties.

*    1.3.  The relationship from a scope to an alias will be `[:DEFINES_TYPE_ALIAS]`. The scope can be a `:NAMESPACE`, `:CLASS_STRUCTURE`, or `:DATA_STRUCTURE`. An agent can query `MATCH (scope)-[:DEFINES_TYPE_ALIAS]->(alias:TYPE_ALIAS)` to find where an alias is defined. They can also use `MATCH (alias:TYPE_ALIAS) WHERE alias.qualified_name = 'my_namespace::MyClass::MyAlias' OR alias.name = 'MyAlias'` to find an alias.

*    1.4. A `:TYPE_ALIAS` node can also be found from its defining `:FILE` node, using `MATCH (file:FILE)-[:DEFINES]->(alias:TYPE_ALIAS)`. This is because the `:FILE` node has a `[:DEFINES]` relationship to all the symbols defined in the file, including `:TYPE_ALIAS` nodes.

*    1.5. The `:TYPE_ALIAS` node will have `name_location` and `body_location` properties. The `body_location` property will store the location of the full alias definition, not just the body part. This is especially important for the templated type alias definition like `template <typename T> using MyVector = std::vector<T>;`.

**2. Representation of the aliased type (aliasee) of a TypeAlias (aliaser)**

*    2.1.  The aliased type of a `:TYPE_ALIAS` node will be represented by a `[:ALIAS_OF]` relationship to the type defintion.

*    2.2.  The target of this relationship can be a `:CLASS_STRUCTURE`, `:DATA_STRUCTURE`, or a new node type `:TYPE_EXPRESSION`. `:TYPE_EXPRESSION` is used when the aliasee is not a `:CLASS_STRUCTURE` or `:DATA_STRUCTURE`. It represents a type expression such as `std::string`, `std::vector<int>`, etc.. Its `name` property stores the type expression. Its `kind` property stores "TypeExpression". Since it is not a symbol, we need create synthetic symbol for `TYPE_EXPRESSION` node. Their `id` can be computed as a hash of the type expression with a prefix like `type://<type_expression>` to ensure the uniqueness across the graph. Same type expression from different type alias definitions should be merged to the same node. For example, `using Vec1 = std::vector<int>;` and `using Vec2 = std::vector<int>;`, they will lead to two different `:TYPE_ALIAS` nodes but point to the same `:TYPE_EXPRESSION` node.

*    2.3.  We may use `:TYPE_EXPRESSION` for fundamental and derived types as well when we encounter them during type alias parsing. At the moment we want to keep their representation as simple as possible, although it is not very accurate to put them in `:TYPE_EXPRESSION` node. (Note, we don't create `:TYPE_EXPRESSION` nodes specifically for fundamental and derived types, unless they appear as the aliasee of a type alias.)

*    2.4. For alias chains (e.g., `using MyInt = int; using MyInt2 = MyInt;`), we allow to have relationship of `[:ALIAS_OF]` to another `:TYPE_ALIAS` node, if our compilation parser knows the right-hand-side aliasee is a type aliaser as well. For type expression that involves type alias such as `using MyVector = std::vector<MyInt>;`, the aliasee is not a direct type alias but an expression, so we create a `:TYPE_EXPRESSION` node for it.

*    2.5. When the agent needs to understand the type of a variable that is defined using a type alias, it follows the `[:ALIAS_OF]` relationship to find the aliased type with query, such as `MATCH (ta:TYPE_ALIAS {id: '<type_alias_id>'})-[:ALIAS_OF]->(type:CLASS_STRUCTURE|DATA_STRUCTURE|TYPE_EXPRESSION|TYPE_ALIAS) RETURN type.id, type.kind, type.name`.  

    *   2.5.1. If the kind is `TypeExpression`, the agent can use the `name` property to understand the type expression. If the expression involves unknown type names, it can query again for the `:TYPE_ALIAS` nodes with the unknown type names. 
    *   2.5.2. If the kind is `TypeAlias`, the agent can recursively follow the `[:ALIAS_OF]` relationship to find the aliasee nodes.
    *   2.5.3. If the kind is `Struct`, `Enum`, `Union`, or `Class`, the agent can get their members with `HAS_FIELD` and `HAS_METHOD` relationships. Or it can use `body_location` property to get the code of the structures.


**3. On Typing Relationships and Their Scope**

*   3.1.  The relationship will be named `[:OF_TYPE]`. The direction will be `(var)-[:OF_TYPE]->(type)`.

*   3.2.  We will create `[:OF_TYPE]` relationships for global/namespace `VARIABLE` nodes and for `FIELD` nodes within structs/classes, only if their type is of user-defined types (i.e., `Class`, `Struct`, `Enum`, `Union`, or an type alias). We will consider how to handle other types in the future. 

*   3.3.  The target of an `[:OF_TYPE]` relationship can be a `:TYPE_ALIAS`, `:CLASS_STRUCTURE`, or `:DATA_STRUCTURE` node. The target should not be a `:TYPE_EXPRESSION` node. 

*   3.4.  For variables/fields of non-user-defined types (int, char, etc.), we will not create `[:OF_TYPE]` relationships for them. There are two reasons:
         
    *   3.4.1. The fundamental type's string will be stored on the `:VARIABLE` or `:FIELD` node's existing `type` property, which is sufficient for an agent's analysis of simple types. Creating relationships for primitive types would clutter the graph and provide no additional value.
    *   3.4.2. More fundamentally, we skip other types (fundamental types and derived types based on fundamental types) for now because our target is to support agent to understand the type names met in source code. We are not developing a compiler infrastructure. The key difference is, agent can read source code, and understand it. For example, if a type name appearing in a function code is defined in another file, the agent cannot understand it by only reading the function's source code. On the other hand, the fundamental types (int, char, etc.) and derived types based on fundamental types are defined locally in the source code and are already well-understood by the agent reading the source code. 
    *   3.4.3. The exception is the derived types built upon type aliases such as `MyClass*`, in which case the agent cannot simply understand the actual type by reading the source code or the `type` property of the `:VARIABLE` or `:FIELD` node. In this case, it will have to query the graph for the unknown type names (`MyClass`) used in the derived types, via `MATCH (ta:TYPE_ALIAS {name: $unknown_type_name})-[:ALIAS_OF]->(td) RETURN td.id, td.kind, td.name`. (Ideally, we could build `:TYPE_EXPRESSION` nodes for these derived types, and create `[:OF_TYPE]` relationships for those variable/field nodes. But as we mentioned above, we only generate `:TYPE_EXPRESSION` nodes for type aliases for now. We don't have a plan to generate `:TYPE_EXPRESSION` nodes proactively for variables/fields.)

**3. On External and Anonymous Types**

*   3.1.  **External Aliases:** For now, we will explicitly ignore aliases from headers outside the project path (like `<fstream>`). The existing project-only filtering will be maintained. The reason is, if a developer uses an alias from an external header, the developer is expected to understand the alias's meaning. So does the agent. If in the future we need to support external aliases, we need to build a mechanism to track the external aliases and their meanings.
*   3.2.  **Anonymous Type Structure Aliases:** For a case like `typedef struct {...} aType;` (or `typedef class {...} aType;`, etc.), the `:TYPE_ALIAS` node for `aType` will have an `[:ALIAS_OF]` relationship pointing to the synthetic `:DATA_STRUCTURE|CLASS_STRUCTURE` node representing the anonymous structure. The project has already implemented this synthetic node generation.

**Notes**
*   We had thought to have a property `alias_definition` for `:TYPE_ALIAS` nodes. It stores the full alias definition as a string. For example, `using MyVector = std::vector<int>;`. Now we decide to remove this property, since we can follow the outgoing relationship `[:ALIAS_OF]` to a target node of the aliasee, or we can get the alias definition from the `body_location` property. Both ways work. In our original thought, the outgoing relationship `[:ALIAS_OF]` only went to target node of type `[:DATA_STRUCTURE|:CLASS_STRUCTURE]`, and the property `alias_definition` was used mainly for type expression. In our current plan, by introducing the new `[:TYPE_EXPRESSION]` nodes, the outgoing relationship `[:ALIAS_OF]` always works. The value of original `alias_definition` property can be found in the target node.So we remove the `alias_definition` property.

*   In the following design documents, we will intentionally decouple the type alias processing from the variable/field processing to make the design modular. That is, we will not touch anything related to Field/Variables before we are completely done with TypeAlias whole processing (stage 010,011,012). More importantly, we will only decide whether to process Field/Variables at all after we are done with TypeAlias whole processing. Only after we are done with TypeAlias whole processing, we will know if the design is good enough to process Field/Variables.