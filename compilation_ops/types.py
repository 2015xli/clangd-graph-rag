#!/usr/bin/env python3
"""
Data structures for the compilation parsing layer.
"""

from dataclasses import dataclass, field
from typing import List, Optional, NamedTuple
from clangd_index_yaml_parser import RelativeLocation

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
    id: str # Synthetic: hash(name + file_uri + name_location)
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
    id: str # USR-derived ID for the aliaser
    file_uri: str # File URI where this TypeAliasSpan was found
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
