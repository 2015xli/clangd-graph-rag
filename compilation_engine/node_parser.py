#!/usr/bin/env python3
"""
Mixin for processing individual AST nodes and extracting semantic metadata.
"""
import os
import logging
import sys
import clang.cindex
from urllib.parse import urlparse, unquote
from typing import Optional, Tuple, Protocol, Dict, List, Any

from clangd_index_yaml_parser import RelativeLocation
from utils import hash_usr_to_id, make_symbol_key, make_synthetic_id
from .types import *

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class ClangWorkerInterface(Protocol):
    """Explicit contract of what the host class must provide to the Mixin."""
    span_results: Dict[Tuple[str, str], Dict[str, SourceSpan]]
    macro_spans: Dict[str, MacroSpan]
    type_alias_spans: Dict[str, TypeAliasSpan]
    instantiations: Dict[str, List[Any]]
    _tu_hash: str
    lang: str
    project_path: str

    def _get_parent_id(self, node: clang.cindex.Cursor) -> Optional[str]: ...
    def _convert_node_kind_to_index_kind(self, node: clang.cindex.Cursor) -> str: ...
    def _get_symbol_name_location(self, node: clang.cindex.Cursor) -> Tuple[int, int]: ...
    def _get_source_text_for_extent(self, extent: Any, file_path: str) -> str: ...

class NodeParserMixin:
    """Encapsulates the logic for identifying and extracting data from a specific AST node."""

    def _process_generic_node(self: ClangWorkerInterface, node, file_name):
        """Processes generic AST nodes like Functions, Classes, and Structs."""
        name_start_line, name_start_col = self._get_symbol_name_location(node)
        body_start_line, body_start_col = node.extent.start.line - 1, node.extent.start.column - 1
        body_end_line, body_end_col = node.extent.end.line - 1, node.extent.end.column - 1
        file_uri = f"file://{file_name}"

        kind = self._convert_node_kind_to_index_kind(node)
        
        # IDENTITY STRATEGY: Primary identity via USR-hash
        # This hash matches the Clangd index ID, enabling O(1) matching in Tier 1.
        usr = node.get_usr()
        if usr:
            synthetic_id = hash_usr_to_id(usr)
        else:
            # Fallback for nodes without USR (extremely rare for definitions).
            node_key = make_symbol_key(node.spelling, kind, file_uri, name_start_line, name_start_col)
            synthetic_id = make_synthetic_id(node_key)

        # ANONYMITY HANDLING: Use USR as name for debug info when no spelling is available.
        is_anonymous = not node.spelling or "(unnamed" in node.spelling
        node_name = usr if (is_anonymous and usr) else node.spelling

        # Deduplication check using the final ID.
        if synthetic_id in self.span_results[(file_uri, self._tu_hash)]: 
            return  

        parent_id = self._get_parent_id(node)
        original_name, expanded_from_id = self._get_macro_causality(node, file_uri)

        # COLLECT MEMBER IDs for composite types
        member_ids = []
        if node.kind.name in NODE_KIND_FOR_COMPOSITE_TYPES:
            for child in node.get_children():
                if child.kind.name in NODE_KIND_MEMBERS:
                    child_usr = child.get_usr()
                    if child_usr:
                        member_ids.append(hash_usr_to_id(child_usr))

        span = SourceSpan(
            name=sys.intern(node_name),
            kind=sys.intern(kind),
            lang=self.lang,
            name_location=RelativeLocation(name_start_line, name_start_col, name_start_line, name_start_col + len(node.spelling)),
            body_location=RelativeLocation(body_start_line, body_start_col, body_end_line, body_end_col),
            id=synthetic_id,
            parent_id=parent_id,
            original_name=original_name,
            expanded_from_id=expanded_from_id,
            member_ids=member_ids
        )
        self.span_results[(sys.intern(file_uri), self._tu_hash)][synthetic_id] = span

    def _process_macro_definition(self: ClangWorkerInterface, node, file_name):
        name = node.spelling
        file_uri = f"file://{file_name}"
        loc = node.location
        name_line, name_col = loc.line - 1, loc.column - 1
        
        body_start_line, body_start_col = node.extent.start.line - 1, node.extent.start.column - 1
        body_end_line, body_end_col = node.extent.end.line - 1, node.extent.end.column - 1
        
        usr = node.get_usr()
        if usr:
            synthetic_id = hash_usr_to_id(usr)
        else:
            node_key = make_symbol_key(name, "Macro", file_uri, name_line, name_col)
            synthetic_id = make_synthetic_id(node_key)
        
        try:
            is_function_like = clang.cindex.conf.lib.clang_Cursor_isMacroFunctionLike(node)
        except Exception:
            is_function_like = False
            
        macro_definition = self._get_source_text_for_extent(node.extent, file_name)

        span = MacroSpan(
            id=synthetic_id,
            name=sys.intern(name),
            lang=self.lang,
            file_uri=sys.intern(file_uri),
            name_location=RelativeLocation(name_line, name_col, name_line, name_col + len(name)),
            body_location=RelativeLocation(body_start_line, body_start_col, body_end_line, body_end_col),
            is_function_like=bool(is_function_like),
            macro_definition=macro_definition
        )
        self.macro_spans[synthetic_id] = span

    def _process_type_alias_node(self: ClangWorkerInterface, node, file_name):
        semantic_parent = node.semantic_parent
        if semantic_parent and semantic_parent.kind.name in NODE_KIND_CALLERS:
            return

        name = node.spelling
        name_start_line, name_start_col = self._get_symbol_name_location(node)
        body_start_line, body_start_col = node.extent.start.line - 1, node.extent.start.column - 1
        body_end_line, body_end_col = node.extent.end.line - 1, node.extent.end.column - 1
        file_uri = f"file://{file_name}"
        aliaser_id = hash_usr_to_id(node.get_usr())
        scope = self._get_fully_qualified_scope(semantic_parent)
        parent_id = self._get_parent_id(node)

        underlying_type = node.underlying_typedef_type
        aliased_canonical_spelling = underlying_type.get_canonical().spelling
        aliased_type_id = None
        aliased_type_kind = None
        is_aliasee_definition = False

        aliasee_decl_cursor = underlying_type.get_declaration()

        if aliasee_decl_cursor.kind.name not in  {"NO_DECL_FOUND", "TEMPLATE_TEMPLATE_PARAMETER"}:
            is_aliasee_definition = aliasee_decl_cursor.is_definition()
            aliased_type_kind = self._convert_node_kind_to_index_kind(aliasee_decl_cursor)

            if aliasee_decl_cursor.location.file is None:
                aliased_type_id = None
            else:
                aliased_usr = aliasee_decl_cursor.get_usr()
                if aliased_usr:
                    aliased_type_id = hash_usr_to_id(aliased_usr)
                else:
                    aliasee_name_line, aliasee_name_col = self._get_symbol_name_location(aliasee_decl_cursor)
                    aliased_type_id = make_synthetic_id(
                        make_symbol_key(
                            aliasee_decl_cursor.spelling,
                            aliased_type_kind,
                            f"file://{os.path.abspath(aliasee_decl_cursor.location.file.name)}",
                            aliasee_name_line,
                            aliasee_name_col
                        )
                    )

                file_path = os.path.abspath(aliasee_decl_cursor.location.file.name)
                if not file_path.startswith(self.project_path): 
                    aliased_type_id = None

        original_name, expanded_from_id = self._get_macro_causality(node, file_uri)
        
        new_type_alias_span = TypeAliasSpan(
            id=aliaser_id,
            file_uri=file_uri,
            lang=self.lang,
            name=name,
            name_location=RelativeLocation(name_start_line, name_start_col, name_start_line, name_start_col + len(name)),
            body_location=RelativeLocation(body_start_line, body_start_col, body_end_line, body_end_col),
            aliased_canonical_spelling=aliased_canonical_spelling,
            aliased_type_id=aliased_type_id,
            aliased_type_kind=aliased_type_kind,
            is_aliasee_definition=is_aliasee_definition,
            scope=scope,
            parent_id=parent_id,
            original_name=original_name,
            expanded_from_id=expanded_from_id
        )
        
        existing_span = self.type_alias_spans.get(aliaser_id)
        if existing_span:
            if new_type_alias_span.is_aliasee_definition and not existing_span.is_aliasee_definition:
                self.type_alias_spans[aliaser_id] = new_type_alias_span
        else:
            self.type_alias_spans[aliaser_id] = new_type_alias_span

    def _get_macro_causality(self: ClangWorkerInterface, node, file_uri: str) -> Tuple[Optional[str], Optional[str]]:
        """Determines if a symbol node was generated by a macro."""
        instantiations = self.instantiations.get(file_uri, [])
        if not instantiations:
            return None, None

        node_extent = node.extent

        def extent_contains(outer, inner):
            if inner.start.line < outer.start.line: return False
            if inner.end.line > outer.end.line: return False
            if inner.start.line == outer.start.line and inner.start.column < outer.start.column: return False
            if inner.end.line == outer.end.line and inner.end.column > outer.end.column: return False
            return True

        enclosing_inst = None
        for inst in instantiations:
            if extent_contains(inst.extent, node_extent):
                if enclosing_inst is None or extent_contains(inst.extent, enclosing_inst.extent):
                    enclosing_inst = inst

        if not enclosing_inst:
            return None, None

        macro_def_cursor = enclosing_inst.referenced
        if not macro_def_cursor:
            return None, None

        def_loc = macro_def_cursor.location
        if not def_loc.file:
            return None, None

        def_file = os.path.abspath(def_loc.file.name)
        if not def_file.startswith(self.project_path):
            return None, None

        if not node.is_definition():
            return None, None

        usr = macro_def_cursor.get_usr()
        if usr: 
            expanded_from_id = hash_usr_to_id(usr)
        else:
            node_key = make_symbol_key(macro_def_cursor.spelling, "Macro", f"file://{def_file}", def_loc.line - 1, def_loc.column - 1)
            expanded_from_id = make_synthetic_id(node_key)

        file_path = unquote(urlparse(file_uri).path)
        original_name = self._get_source_text_for_extent(enclosing_inst.extent, file_path)

        return original_name, expanded_from_id

    def _get_fully_qualified_scope(self: ClangWorkerInterface, node: clang.cindex.Cursor) -> str:
        """Builds the fully qualified scope string for a given cursor."""
        scope_parts = []
        current = node.semantic_parent

        while current and current.kind != clang.cindex.CursorKind.TRANSLATION_UNIT:
            if current.kind.name in NODE_KIND_FOR_SCOPES:
                name = current.spelling
                if name:
                    scope_parts.append(name)
                else:  
                    scope_parts.append(f"(anonymous {current.kind.name})")
            current = current.semantic_parent

        if not scope_parts:
            return ""
        return "::".join(reversed(scope_parts)) + "::"

    def _get_symbol_name_location(self: ClangWorkerInterface, node):
        """Return zero-based (line, column) for symbol's name."""
        for tok in node.get_tokens():
            if tok.spelling == node.spelling:
                loc = tok.location
                try:
                    file, line, col, _ = loc.get_expansion_location()
                except AttributeError:
                    continue
                if file and file.name.startswith(self.project_path):
                    return (line - 1, col - 1)
        loc = node.location
        try:
            file, line, col, _ = loc.get_expansion_location()
            return (line - 1, col - 1)
        except AttributeError:
            return (node.location.line - 1, node.location.column - 1)

    def _get_source_text_for_extent(self: ClangWorkerInterface, extent, file_path: str) -> str:
        """Reads the source text for a given Clang extent."""
        try:
            with open(file_path, 'r', errors='ignore') as f:
                lines = f.readlines()
            
            start_line = extent.start.line - 1
            start_col = extent.start.column - 1
            end_line = extent.end.line - 1
            end_col = extent.end.column - 1
            
            if start_line == end_line:
                return lines[start_line][start_col:end_col]
            else:
                result = [lines[start_line][start_col:]]
                for i in range(start_line + 1, end_line):
                    result.append(lines[i])
                result.append(lines[end_line][:end_col])
                return "".join(result)
        except Exception as e:
            logger.error(f"Error reading source text for extent in {file_path}: {e}")
            return ""