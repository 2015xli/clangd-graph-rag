#!/usr/bin/env python3
"""
Data structures and Clang node kind constants for the compilation engine.
"""
import clang.cindex
from dataclasses import dataclass, field
from typing import List, Optional, NamedTuple
from clangd_index_yaml_parser import RelativeLocation

# --- Data Structures (Models) ---

@dataclass(frozen=True, slots=True)
class SourceSpan:
    """Represents a lexically defined entity in the source code."""
    name: str
    kind: str
    lang: str
    name_location: RelativeLocation
    body_location: RelativeLocation
    id: str
    parent_id: Optional[str]
    original_name: Optional[str] = None
    expanded_from_id: Optional[str] = None
    member_ids: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> 'SourceSpan':
        return cls(
            name=data['Name'],
            kind=data['Kind'],
            lang=data['Lang'],
            name_location=RelativeLocation.from_dict(data['NameLocation']),
            body_location=RelativeLocation.from_dict(data['BodyLocation']),
            id=data['Id'],
            parent_id=data['ParentId'],
            original_name=data.get('OriginalName'),
            expanded_from_id=data.get('ExpandedFromId'),
            member_ids=data.get('MemberIds', [])
        )

@dataclass(frozen=True, slots=True)
class MacroSpan:
    """Represents a preprocessor #define directive."""
    id: str 
    name: str
    lang: str
    file_uri: str
    name_location: RelativeLocation
    body_location: RelativeLocation
    is_function_like: bool
    macro_definition: str

@dataclass(frozen=True, slots=True)
class TypeAliasSpan:
    """Represents a typedef or using alias."""
    id: str 
    file_uri: str 
    lang: str
    name: str
    name_location: RelativeLocation
    body_location: RelativeLocation
    aliased_canonical_spelling: str
    aliased_type_id: Optional[str]
    aliased_type_kind: Optional[str]
    is_aliasee_definition: bool
    scope: str
    parent_id: Optional[str]
    original_name: Optional[str] = None
    expanded_from_id: Optional[str] = None

class IncludeRelation(NamedTuple):
    source_file: str
    included_file: str

# --- Node Kind Groupings (Constants) ---

NODE_KIND_VARIABLES = {
    clang.cindex.CursorKind.VAR_DECL.name
}

NODE_KIND_FUNCTIONS = {
    clang.cindex.CursorKind.FUNCTION_DECL.name,
    clang.cindex.CursorKind.FUNCTION_TEMPLATE.name
}

NODE_KIND_CONSTRUCTOR = {
    clang.cindex.CursorKind.CONSTRUCTOR.name
}

NODE_KIND_DESTRUCTOR = {
    clang.cindex.CursorKind.DESTRUCTOR.name
}

NODE_KIND_CONVERSION_FUNCTION = {
    clang.cindex.CursorKind.CONVERSION_FUNCTION.name
}

NODE_KIND_CXX_METHOD = {
    clang.cindex.CursorKind.CXX_METHOD.name
}

NODE_KIND_METHODS = NODE_KIND_CXX_METHOD | NODE_KIND_CONSTRUCTOR | NODE_KIND_DESTRUCTOR | NODE_KIND_CONVERSION_FUNCTION
NODE_KIND_CALLERS = NODE_KIND_FUNCTIONS | NODE_KIND_METHODS

NODE_KIND_UNION = {
    clang.cindex.CursorKind.UNION_DECL.name
}

NODE_KIND_ENUM = {
    clang.cindex.CursorKind.ENUM_DECL.name
}

NODE_KIND_STRUCT = {
    clang.cindex.CursorKind.STRUCT_DECL.name
}

NODE_KIND_CLASSES = {
    clang.cindex.CursorKind.CLASS_DECL.name,
    clang.cindex.CursorKind.CLASS_TEMPLATE.name,
    clang.cindex.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION.name
}

NODE_KIND_FOR_COMPOSITE_TYPES = NODE_KIND_UNION | NODE_KIND_ENUM | NODE_KIND_STRUCT | NODE_KIND_CLASSES
NODE_KIND_FOR_BODY_SPANS = NODE_KIND_FUNCTIONS | NODE_KIND_METHODS | NODE_KIND_FOR_COMPOSITE_TYPES | NODE_KIND_VARIABLES

NODE_KIND_NAMESPACE = {
    clang.cindex.CursorKind.NAMESPACE.name
}

NODE_KIND_FOR_SCOPES = NODE_KIND_NAMESPACE | NODE_KIND_FOR_COMPOSITE_TYPES

NODE_KIND_TYPE_ALIASES = {
    clang.cindex.CursorKind.TYPE_ALIAS_TEMPLATE_DECL.name,
    clang.cindex.CursorKind.TYPE_ALIAS_DECL.name,
    clang.cindex.CursorKind.TYPEDEF_DECL.name
}

NODE_KIND_FOR_USER_DEFINED_TYPES = NODE_KIND_FOR_COMPOSITE_TYPES | NODE_KIND_TYPE_ALIASES

# Kinds that represent semantic members of a composite type
NODE_KIND_MEMBERS = NODE_KIND_CALLERS | {
    clang.cindex.CursorKind.FIELD_DECL.name,
    clang.cindex.CursorKind.ENUM_CONSTANT_DECL.name,
    clang.cindex.CursorKind.VAR_DECL.name, # For static members
} | NODE_KIND_FOR_USER_DEFINED_TYPES # For nested types
