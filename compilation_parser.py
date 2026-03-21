#!/usr/bin/env python3
"""
This module defines the parser layer for extracting data from source code.

It provides an abstract base class `CompilationParser` and concrete implementations
like `ClangParser` and `TreesitterParser`.
"""

import os
import logging
import subprocess
import sys
import hashlib
from itertools import islice
from typing import List, Dict, Set, Tuple, Union, Any, Optional, NamedTuple
from pathlib import Path
from collections import defaultdict
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor, as_completed, wait, FIRST_COMPLETED
from multiprocessing import get_context
import gc
from dataclasses import dataclass, field
from urllib.parse import urlparse, unquote

# Assuming RelativeLocation is defined in this file or imported
from clangd_index_yaml_parser import RelativeLocation

import clang.cindex
import tree_sitter_c as tsc
from tree_sitter import Language, Parser as TreeSitterParser

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# ============================================================
# Data classes for span representation
# ============================================================
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
    # Added to support macro causality link: if a symbol name is generated via a macro expansion,
    # this field stores the original name before expansion
    original_name: Optional[str] = None
    # this field stores the ID of the macro that generates it
    expanded_from_id: Optional[str] = None
    # List of USR-derived IDs of immediate semantic children (members)
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

# ============================================================
# Include relations
# ============================================================
class IncludeRelation(NamedTuple):
    source_file: str
    included_file: str

# ============================================================
# Core Clang worker
# ============================================================

class _ClangWorkerImpl:
    """Parses a single compilation entry and extracts SourceSpans + include relations."""

    def __init__(self, project_path: str, clang_include_path: str):
        self.project_path = os.path.abspath(project_path)
        if not self.project_path.endswith(os.sep):
            self.project_path += os.sep
            
        self.clang_include_path = clang_include_path
        self.index = clang.cindex.Index.create()
        self.entry = None
        # Data strucutures to return: 
        #   - {(file_uri, tu_hash) → {id → SourceSpan}}
        self.span_results: Dict[Tuple[str, str], Dict[str, SourceSpan]] = None
        #   - {(including_file, included_file)}
        self.include_relations: Set[IncludeRelation] = None
        #   - {(caller_id, callee_id)}
        self.static_call_relations: Set[Tuple[str, str]] = None
        #   - {file_uri → {id → TypeAliasSpan}}
        self.type_alias_spans: Dict[str, TypeAliasSpan] = {}
        #   - {id → MacroSpan}
        self.macro_spans: Dict[str, MacroSpan] = {}
        #   - {file_uri → [MACRO_INSTANTIATION cursors]}
        self.instantiations: Dict[str, List[Any]] = defaultdict(list)

        # file-level cache to avoid re-processing header nodes in identical TU contexts
        # Since we use since _ClangWorkerImpl as a singleton in a worker process, this cache is shared across all invocations
        # type: Dict[tu_hash, Set[header_filepath_hash]]
        self._global_header_cache:Dict[str, Set[str]] = defaultdict(set)
        # previously processed global headers
        self._processed_global_headers: Optional[Set[str]] = None

    # --------------------------------------------------------
    # Main entry
    # --------------------------------------------------------
    def run(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse the entry and return a dictionary of results.
        """
        self.entry = entry
        self.span_results = defaultdict(dict)
        self.include_relations = set()
        self.static_call_relations = set()
        self.type_alias_spans: Dict[str, TypeAliasSpan] = {}
        self.macro_spans: Dict[str, MacroSpan] = {}
        self.instantiations = defaultdict(list)
        self._tu_hash = None
        # Local per-TU header cache
        self._local_header_cache: Set[str] = set()

        file_path = self.entry['file']
        dir_path = self.entry['directory']
        args = self.entry['arguments']
        original_dir = os.getcwd()
        try:
            os.chdir(dir_path)
            args = self._sanitize_args(args, file_path)
            #logger.debug(f"{args}")

            # compute TU hash (based on relevant preprocessor flags)
            self._tu_hash = sys.intern(self._get_tu_hash(args))
            self._processed_global_headers = self._global_header_cache.get(self._tu_hash, None)

            self.lang = sys.intern(CompilationParser.get_language(file_path))

            # proceed to parse with args
            self._parse_translation_unit(file_path, args)

        except clang.cindex.TranslationUnitLoadError as e:
            logger.exception(f"Clang worker failed to parse {file_path}: {e}")
        except Exception as e:
            logger.exception(f"Clang worker had an unexpected error on {file_path}: {e}")
        finally:
            os.chdir(original_dir)

        # -----------------------------
        # Merge local header cache → global header cache
        # -----------------------------
        self._global_header_cache[self._tu_hash].update(self._local_header_cache)

        return {
            "span_results": self.span_results,
            "include_relations": self.include_relations,
            "static_call_relations": self.static_call_relations,
            "type_alias_spans": self.type_alias_spans,
            "macro_spans": self.macro_spans
        }

    # --------------------------------------------------------
    # TU Parsing and traversal
    # --------------------------------------------------------
    def _parse_translation_unit(self, file_path: str, args: List[str]):
        # Add additional include path if provided
        if self.clang_include_path:
            args = args + [f"-I{self.clang_include_path}"]

        tu = self.index.parse(
            file_path,
            args=args,
            options=clang.cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
        )

        # collect include relations
        for inc in tu.get_includes():
            if inc.source and inc.include:
                src_file = os.path.abspath(inc.source.name)
                include_file = os.path.abspath(inc.include.name)
                if src_file.startswith(self.project_path) and include_file.startswith(self.project_path):
                    # Create the NamedTuple instance
                    new_relation = IncludeRelation(
                        source_file=sys.intern(src_file), 
                        included_file=sys.intern(include_file)
                    )
                    self.include_relations.add(new_relation)


        self._walk_ast(tu.cursor)

    # --------------------------------------------------------
    # AST walking
    # --------------------------------------------------------
    def _walk_ast(self, node, current_caller_cursor=None):        
        # Determine the current file and check if it's part of the project
        loc_file = node.location.file
        if loc_file:
            file_name = os.path.abspath(loc_file.name)
        elif node.kind == clang.cindex.CursorKind.TRANSLATION_UNIT:
            file_name = os.path.abspath(node.spelling)
        else:
            # Skip nodes without a valid file (e.g., predefined macros, built-ins)
            return

        if not file_name.startswith(self.project_path):
            return

        # --- Update Caller Context ---
        # If this node is a function/method definition, it becomes the new caller context for its children.
        new_caller_cursor = current_caller_cursor
        if node.kind.name in ClangParser.NODE_KIND_CALLERS:
            new_caller_cursor = node

        # --- Process this node ---
        # 1. If it's a macro definition, process its span
        if node.kind == clang.cindex.CursorKind.MACRO_DEFINITION:
            if self._should_process_node(node, file_name):
                self._process_macro_definition(node, file_name)

        # 2. If it's a macro instantiation, record it for causality tracking
        elif node.kind == clang.cindex.CursorKind.MACRO_INSTANTIATION:
            self.instantiations[f"file://{file_name}"].append(node)

        # 3. If it's a definition, process its span
        elif node.is_definition() and node.kind.name in ClangParser.NODE_KIND_FOR_BODY_SPANS:
            if self._should_process_node(node, file_name):
                self._process_generic_node(node, file_name)

        # 4. If it's a TypeAlias declaration, process it
        elif node.kind.name in ClangParser.NODE_KIND_TYPE_ALIASES:
            if self._should_process_node(node, file_name):
                self._process_type_alias_node(node, file_name)

        # 5. If it's a call expression inside a function, process the static call
        elif node.kind == clang.cindex.CursorKind.CALL_EXPR and current_caller_cursor:
            callee_cursor = node.referenced
            # Check if the callee is a static function (internal linkage)
            if callee_cursor and callee_cursor.linkage == clang.cindex.LinkageKind.INTERNAL:
                caller_usr = current_caller_cursor.get_usr()
                callee_usr = callee_cursor.get_usr()
                if caller_usr and callee_usr:
                    caller_id = CompilationParser.hash_usr_to_id(caller_usr)
                    callee_id = CompilationParser.hash_usr_to_id(callee_usr)
                    self.static_call_relations.add((caller_id, callee_id))

        # --- Recurse to children ---
        for c in node.get_children():
            self._walk_ast(c, new_caller_cursor)

    # --------------------------------------------------------
    # Span processing
    # --------------------------------------------------------
    def _process_macro_definition(self, node, file_name):
        # file_name passed here was verified in _walk_ast
        name = node.spelling
        file_uri = f"file://{file_name}"

        # Use location directly for macro name
        loc = node.location
        name_line, name_col = loc.line - 1, loc.column - 1
        
        body_start_line, body_start_col = node.extent.start.line - 1, node.extent.start.column - 1
        body_end_line, body_end_col = node.extent.end.line - 1, node.extent.end.column - 1
        
        usr = node.get_usr()
        if usr:
            synthetic_id = CompilationParser.hash_usr_to_id(usr)
        else:
            # Fallback for nodes without USR (extremely rare for definitions).
            # These will naturally fail Tier 1 ID matching and use Tier 2 Location matching.
            node_key = CompilationParser.make_symbol_key(name, "Macro", file_uri, name_line, name_col)
            synthetic_id = CompilationParser.make_synthetic_id(node_key)
        
        # Check if function-like
        try:
            is_function_like = clang.cindex.conf.lib.clang_Cursor_isMacroFunctionLike(node)
        except Exception:
            is_function_like = False # Fallback
            
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
        
        # Macros are stored by synthetic_id to handle potential identical definitions
        self.macro_spans[synthetic_id] = span

    def _process_generic_node(self, node, file_name):
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
            synthetic_id = CompilationParser.hash_usr_to_id(usr)
        else:
            # Fallback for nodes without USR (extremely rare for definitions).
            # These will naturally fail Tier 1 ID matching and use Tier 2 Location matching.
            node_key = CompilationParser.make_symbol_key(node.spelling, kind, file_uri, name_start_line, name_start_col)
            synthetic_id = CompilationParser.make_synthetic_id(node_key)

        if synthetic_id == "AB049A4929FEAB02":
            pass

        # ANONYMITY HANDLING: Use USR as name for debug info when no spelling is available.
        is_anonymous = not node.spelling or "(unnamed" in node.spelling
        node_name = usr if (is_anonymous and usr) else node.spelling

        # Deduplication check using the final ID.
        if synthetic_id in self.span_results[(file_uri, self._tu_hash)]: 
            return  

        parent_id = self._get_parent_id(node)
        # TODO: When the structure's name is expanded from macro, but body is not, the logic cannot find its expanded_from relation.
        original_name, expanded_from_id = self._get_macro_causality(node, file_uri)

        # COLLECT MEMBER IDs for composite types
        # This allows bridging macro-generated members back to their semantic parents.
        member_ids = []
        if node.kind.name in ClangParser.NODE_KIND_FOR_COMPOSITE_TYPES:
            # Only iterate immediate children
            for child in node.get_children():
                # Filter for actual members (not access specifiers, attributes, etc.)
                if child.kind.name in ClangParser.NODE_KIND_MEMBERS:
                    child_usr = child.get_usr()
                    if child_usr:
                        member_ids.append(CompilationParser.hash_usr_to_id(child_usr))

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
        # Result dictionary is now keyed by ID.
        self.span_results[(sys.intern(file_uri), self._tu_hash)][synthetic_id] = span

    def _process_type_alias_node(self, node, file_name):
        """Processes typedef and using declarations."""
        # Scope Filtering: Only process aliases at global, namespace, or class/struct scopes.
        semantic_parent = node.semantic_parent
        if semantic_parent and semantic_parent.kind.name in ClangParser.NODE_KIND_CALLERS:
            return

        # Extract Aliaser Info
        name = node.spelling
        name_start_line, name_start_col = self._get_symbol_name_location(node)
        body_start_line, body_start_col = node.extent.start.line - 1, node.extent.start.column - 1
        body_end_line, body_end_col = node.extent.end.line - 1, node.extent.end.column - 1
        file_uri = f"file://{file_name}"
        aliaser_id = CompilationParser.hash_usr_to_id(node.get_usr())
        scope = self._get_fully_qualified_scope(semantic_parent)
        parent_id = self._get_parent_id(node)

        # Next, resolve Aliasee Info. 
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
                    aliased_type_id = CompilationParser.hash_usr_to_id(aliased_usr)
                else:
                    # Fallback for aliasee without USR (e.g., anonymous entities in some contexts)
                    aliasee_name_line, aliasee_name_col = self._get_symbol_name_location(aliasee_decl_cursor)
                    aliased_type_id = CompilationParser.make_synthetic_id(
                        CompilationParser.make_symbol_key(
                            aliasee_decl_cursor.spelling,
                            aliased_type_kind,
                            f"file://{os.path.abspath(aliasee_decl_cursor.location.file.name)}",
                            aliasee_name_line,
                            aliasee_name_col
                        )
                    )

                # Project path filtering
                file_path = os.path.abspath(aliasee_decl_cursor.location.file.name)
                if not file_path.startswith(self.project_path): 
                    aliased_type_id = None

        # Store TypeAliasSpan
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
        
        # Reconciliation logic: prioritize the span whose aliasee is a definition.
        existing_span = self.type_alias_spans.get(aliaser_id)
        if existing_span:
            if new_type_alias_span.is_aliasee_definition and not existing_span.is_aliasee_definition:
                self.type_alias_spans[aliaser_id] = new_type_alias_span
        else:
            self.type_alias_spans[aliaser_id] = new_type_alias_span

    def _should_process_node(self, node, file_name) -> bool:
        """Avoid redundant node processing across identical TU contexts."""
        if file_name != self.entry['file']:              
            if self._processed_global_headers and file_name in self._processed_global_headers:
                return False
            self._local_header_cache.add(file_name)
        return True

    # --------------------------------------------------------
    # Helpers
    # --------------------------------------------------------
    
    def _get_parent_id(self, node) -> Optional[str]:
        """
        Retrieves the parent_id for a node based on its semantic parent using USR hashing.
        
        This method also implements 'Parent Anchoring': if the parent is a composite type 
        (Class, Struct, Enum, Union) that hasn't been processed yet, it proactively
        triggers processing for that parent. This is essential for handling implicit
        template specializations of member functions (e.g., template <> void Box<int>::open(){...}).
        In this case the partial specialized class Box<int> does not explicitly exist. 
        We have to create a phony parent structure span for the class. 
        Note, the phony class does have correct USR and corresponding id (that can match clangd index id).
        In clangd yaml index, the member function has a symbol and also a reference container id (equal to this phony parent id).
        But clangd yaml does not have a symbol for that id. 
        """
        parent = node.semantic_parent
        
        # Base case: top-level or linkage specs (like extern "C") have no parent_id in our graph.
        if not parent or parent.kind == clang.cindex.CursorKind.TRANSLATION_UNIT or parent.kind == clang.cindex.CursorKind.LINKAGE_SPEC:
            return None
            
        file_name = parent.location.file.name if parent.location.file else parent.translation_unit.spelling
        if not file_name:
            return None
        
        # Verify if the parent is a kind we track as a body span.
        if parent.kind.name not in ClangParser.NODE_KIND_FOR_BODY_SPANS:
            # Namespaces are scope containers but currently don't have SourceSpans.
            if parent.kind.name not in "ClangParser.NODE_KIND_NAMESPACE": 
                logger.error(f"Parent {parent.kind.name} ({parent.spelling}) of node {node.spelling} is not in NODE_KIND_FOR_BODY_SPANS")
                return None

        # Determine the parent's semantic identity via USR.
        usr = parent.get_usr()
        if usr:
            parent_id = CompilationParser.hash_usr_to_id(usr)
            
            # PARENT ANCHORING: Ensure the parent has a SourceSpan record.
            # If it's a composite type and hasn't been processed in this TU context, process it now.
            if parent.kind.name in ClangParser.NODE_KIND_FOR_COMPOSITE_TYPES:
                parent_uri = f"file://{os.path.abspath(file_name)}"
                if parent_id not in self.span_results[(parent_uri, self._tu_hash)]:
                    # Recursive call ensures the phony parent is anchored first.
                    self._process_generic_node(parent, file_name)
            
            return parent_id
            
        # Fallback for parent without USR (extremely rare for definitions).
        file_uri = f"file://{os.path.abspath(file_name)}"
        line, col = self._get_symbol_name_location(parent)
        parent_kind = self._convert_node_kind_to_index_kind(parent)
        parent_key = CompilationParser.make_symbol_key(parent.spelling, parent_kind, file_uri, line, col)
        return CompilationParser.make_synthetic_id(parent_key)

    def _get_symbol_name_location(self, node):
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

    def _get_macro_causality(self, node, file_uri: str) -> Tuple[Optional[str], Optional[str]]:
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
            expanded_from_id = CompilationParser.hash_usr_to_id(usr)
        else:
            node_key = CompilationParser.make_symbol_key(macro_def_cursor.spelling, "Macro", f"file://{def_file}", def_loc.line - 1, def_loc.column - 1)
            expanded_from_id = CompilationParser.make_synthetic_id(node_key)

        file_path = unquote(urlparse(file_uri).path)
        original_name = self._get_source_text_for_extent(enclosing_inst.extent, file_path)

        return original_name, expanded_from_id

    def _get_source_text_for_extent(self, extent, file_path: str) -> str:
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

    def _get_fully_qualified_scope(self, node: clang.cindex.Cursor) -> str:
        """Builds the fully qualified scope string for a given cursor."""
        scope_parts = []
        current = node.semantic_parent

        while current and current.kind != clang.cindex.CursorKind.TRANSLATION_UNIT:
            if current.kind.name in ClangParser.NODE_KIND_FOR_SCOPES:
                name = current.spelling
                if name:
                    scope_parts.append(name)
                else:  
                    scope_parts.append(f"(anonymous {current.kind.name})")
            current = current.semantic_parent

        if not scope_parts:
            return ""
        return "::".join(reversed(scope_parts)) + "::"

    def _identify_template_type(self, node) -> str:
        """Distinguishes between 'Class', 'Struct', and 'Union' for templates."""
        tokens = islice(node.get_tokens(), 100)
        bracket_depth = 0
        found_params = False

        for token in tokens:
            s = token.spelling
            if s == '<':
                bracket_depth += 1
                found_params = True
            elif s == '>':
                bracket_depth -= 1
            elif found_params and bracket_depth == 0:
                tag = s.lower()
                if tag in ('struct', 'class', 'union'):
                    return tag.capitalize()
        return "Class"

    def _convert_node_kind_to_index_kind(self, node):
        """Converts a Clang parser kind to a Clangd index kind."""
        kind_name = node.kind.name

        if kind_name in ClangParser.NODE_KIND_FUNCTIONS:
            parent = node.semantic_parent
            if parent and parent.kind.name in ClangParser.NODE_KIND_FOR_COMPOSITE_TYPES:
                if node.is_static_method():
                    return "StaticMethod"
                return "InstanceMethod"
            return "Function"
        elif kind_name in ClangParser.NODE_KIND_CONSTRUCTOR:
            return "Constructor"
        elif kind_name in ClangParser.NODE_KIND_DESTRUCTOR:
            return "Destructor"
        elif kind_name in ClangParser.NODE_KIND_CONVERSION_FUNCTION:
            return "ConversionFunction"
        elif kind_name in ClangParser.NODE_KIND_CXX_METHOD:
            if node.is_static_method():
                return "StaticMethod"
            return "InstanceMethod"
        elif kind_name in ClangParser.NODE_KIND_STRUCT:
            return "Struct"
        elif kind_name in ClangParser.NODE_KIND_UNION:
            return "Union"
        elif kind_name in ClangParser.NODE_KIND_ENUM:
            return "Enum"
        elif kind_name in ClangParser.NODE_KIND_CLASSES:
            if kind_name != clang.cindex.CursorKind.CLASS_DECL.name:
                return self._identify_template_type(node)
            return "Class"
        elif kind_name in ClangParser.NODE_KIND_NAMESPACE:
            return "Namespace"
        elif kind_name in ClangParser.NODE_KIND_TYPE_ALIASES:
            return "TypeAlias"
        else:
            logger.error(f"Unknown Clang parser kind: {kind_name}")
            return "Unknown"

    def _sanitize_args(self, args: List[str], file_path: str) -> List[str]:
            sanitized = []
            skip_next = False
            for a in args:
                if skip_next:
                    skip_next = False
                    continue
                if a == '--': continue
                if a.startswith(('-W', '-O')): continue
                if a in {'-c', '-o', '-MMD', '-MF', '-MT', '-MQ', '-fcolor-diagnostics', '-fdiagnostics-color'}:
                    if a in {'-o', '-MF', '-MT', '-MQ'}:
                        skip_next = True
                    continue
                if a == file_path or os.path.basename(a) == os.path.basename(file_path):
                    continue
                sanitized.append(a)
            return sanitized

    def _get_tu_hash(self, args: List[str]) -> str:
        bucket_lang, bucket_macros, bucket_features, bucket_includes, bucket_other = [], [], [], [], []
        i = 0
        while i < len(args):
            a = args[i]
            if a.startswith(("-std=", "-x", "--driver-mode")): bucket_lang.append(a)
            elif a.startswith(("-D", "-U")): bucket_macros.append(a)
            elif a.startswith(("-f", "-m", "--target=")): bucket_features.append(a)
            elif a in ('-I', '-isystem', '-iquote', '-include'):
                bucket_includes.append(a)
                if i + 1 < len(args):
                    bucket_includes.append(args[i + 1])
                    i += 1 
            elif a.startswith(("-I", "-isystem", "-iquote")): bucket_includes.append(a)
            else: bucket_other.append(a)
            i += 1
        hash_input = " ".join(bucket_lang + bucket_macros + bucket_features + bucket_includes + bucket_other)
        return hashlib.md5(hash_input.encode("utf-8")).hexdigest()

class _TreesitterWorkerImpl:
    """Syntactic parser implementation (Location-based identity)."""
    def __init__(self):
        if not tsc or not TreeSitterParser: raise ImportError("tree-sitter not installed.")
        self.language = Language(tsc.language())
        self.parser = TreeSitterParser(self.language)

    def run(self, file_path: str) -> Tuple[Optional[Dict[str, SourceSpan]], Set]:
        try:
            with open(file_path, "rb") as f:
                source = f.read()
            tree = self.parser.parse(source)
            source_lines = source.decode("utf-8", errors="ignore").splitlines()
            
            spans = {}
            stack = [tree.root_node]
            file_uri = f"file://{os.path.abspath(file_path)}"
            while stack:
                node = stack.pop()
                if node.type == "function_definition":
                    declarator = node.child_by_field_name("declarator")
                    ident_node = next((c for c in declarator.children if c.type == 'identifier'), None)
                    if not ident_node: continue
                    
                    name = source_lines[ident_node.start_point[0]][ident_node.start_point[1]:ident_node.end_point[1]]
                    name_span = RelativeLocation(
                        start_line=ident_node.start_point[0], start_column=ident_node.start_point[1],
                        end_line=ident_node.end_point[0], end_column=ident_node.end_point[1]
                    )
                    body_span = RelativeLocation(
                        start_line=node.start_point[0], start_column=node.start_point[1],
                        end_line=node.end_point[0], end_column=node.end_point[1]
                    )
                    
                    # Syntactic fallback: use location-based identity. 
                    # Tree-sitter has no USR, so these spans will always match in Tier 2 (Location).
                    node_key = CompilationParser.make_symbol_key(name, "Function", file_uri, name_span.start_line, name_span.start_column)
                    synth_id = CompilationParser.make_synthetic_id(node_key)
                    spans[synth_id] = SourceSpan(name=name, kind="Function", lang="C", name_location=name_span, body_location=body_span, id=synth_id, parent_id=None)
                stack.extend(node.children)
            
            if not spans: return None, set()
            return (file_uri, spans), set()
        except Exception as e:
            logger.error(f"Treesitter worker failed to parse {file_path}: {e}")
            return None, set()

# --- Worker Orchestration ---
_worker_impl_instance = None
_count_processed_tus = 0

def _worker_initializer(parser_type: str, init_args: Dict[str, Any]):
    global _worker_impl_instance
    sys.setrecursionlimit(3000)
    if parser_type == 'clang': _worker_impl_instance = _ClangWorkerImpl(**init_args)
    elif parser_type == 'treesitter': _worker_impl_instance = _TreesitterWorkerImpl()
    else: raise ValueError(f"Unknown parser type: {parser_type}")

def _parallel_worker(data: Any) -> Dict[str, Any]:
    global _worker_impl_instance
    global _count_processed_tus
    if _worker_impl_instance is None: raise RuntimeError("Worker not initialized.")

    if len(data) == 1:
        entry = data[0]
        _count_processed_tus += 1
        if _count_processed_tus % 1000 == 0: gc.collect()
        try:
            result_dict = _worker_impl_instance.run(entry)
        except Exception:
            logger.exception(f"Worker failed on {entry}")
            return {"span_results": defaultdict(dict), "include_relations": set(), "static_call_relations": set(), "type_alias_spans": {}, "macro_spans": {}}
        return result_dict

    merged_span_results, merged_include_relations, merged_static_call_relations, merged_type_alias_spans, merged_macro_spans = defaultdict(dict), set(), set(), {}, {}
    for entry in data:
        _count_processed_tus += 1
        if _count_processed_tus % 1000 == 0: gc.collect()
        try:
            result_dict = _worker_impl_instance.run(entry)
        except Exception:
            continue
        merged_span_results.update(result_dict["span_results"])
        merged_include_relations.update(result_dict["include_relations"])
        merged_static_call_relations.update(result_dict["static_call_relations"])
        for alias_id, new_span in result_dict["type_alias_spans"].items():
            existing = merged_type_alias_spans.get(alias_id)
            if not existing or (new_span.is_aliasee_definition and not existing.is_aliasee_definition):
                merged_type_alias_spans[alias_id] = new_span
        merged_macro_spans.update(result_dict.get("macro_spans", {}))

    return {"span_results": merged_span_results, "include_relations": merged_include_relations, "static_call_relations": merged_static_call_relations, "type_alias_spans": merged_type_alias_spans, "macro_spans": merged_macro_spans}

# --- Base Class ---
class CompilationParser:
    """Abstract base class for source code parsers."""
    C_SOURCE_EXTENSIONS = ('.c',)
    CPP_SOURCE_EXTENSIONS = ('.cpp', '.cc', '.cxx')
    CPP20_MODULE_EXTENSIONS = ('.cppm', '.ccm', '.cxxm', '.c++m')
    C_HEADER_EXTENSIONS = ('.h',)
    CPP_HEADER_EXTENSIONS = ('.hpp', '.hh', '.hxx', '.h++', '.inc')
    ALL_SOURCE_EXTENSIONS = C_SOURCE_EXTENSIONS + CPP_SOURCE_EXTENSIONS + CPP20_MODULE_EXTENSIONS
    ALL_HEADER_EXTENSIONS = C_HEADER_EXTENSIONS + CPP_HEADER_EXTENSIONS
    ALL_C_CPP_EXTENSIONS = ALL_SOURCE_EXTENSIONS + ALL_HEADER_EXTENSIONS
    ALL_CPP_SOURCE_EXTENSIONS = CPP_SOURCE_EXTENSIONS + CPP20_MODULE_EXTENSIONS

    def __init__(self, project_path: str):
        self.project_path, self.source_spans, self.include_relations, self.static_call_relations, self.type_alias_spans, self.macro_spans = project_path, defaultdict(dict), set(), set(), {}, {}

    def parse(self, files_to_parse: List[str], num_workers: int = 1): raise NotImplementedError
    def get_source_spans(self): return self.source_spans
    def get_include_relations(self): return self.include_relations
    def get_static_call_relations(self): return self.static_call_relations
    def get_type_alias_spans(self): return self.type_alias_spans
    def get_macro_spans(self): return self.macro_spans
    
    def parser_kind_to_index_kind(self, kind: str, lang: str) -> str:
        """Converts a parser's native node kind string into a Clangd-compatible string."""
        raise NotImplementedError

    @staticmethod
    def hash_usr_to_id(usr: str) -> str:
        """Replicates clangd's ID generation (8 bytes SHA1 hex)."""
        return hashlib.sha1(usr.encode()).digest()[:8].hex().upper()

    @classmethod
    def make_symbol_key(cls, name: str, kind: str, file_uri: str, line: int, col: int) -> str:
        """Fallback location-based key for identity pluralism."""
        return f"{kind}::{name}::{file_uri}:{line}:{col}"

    @classmethod
    def make_synthetic_id(cls, key: str) -> str:
        """Fallback location-based ID for identity pluralism."""
        return hashlib.md5(key.encode()).hexdigest()

    @classmethod
    def get_language(cls, file_name: str) -> str:
        ext = os.path.splitext(file_name)[1].lower()
        if ext in cls.CPP_SOURCE_EXTENSIONS or ext in cls.CPP_HEADER_EXTENSIONS or ext in cls.CPP20_MODULE_EXTENSIONS: return "Cpp"
        if ext in cls.C_SOURCE_EXTENSIONS or ext in cls.C_HEADER_EXTENSIONS: return "C"
        return "Unknown"

    def _parallel_parse(self, items_to_process: List, parser_type: str, num_workers: int, desc: str, worker_init_args: Dict[str, Any] = None, batch_size: int = 1):
        all_spans, all_includes, all_static_calls, all_type_alias_spans, all_macro_spans, file_tu_hash_map = defaultdict(dict), set(), set(), {}, {}, defaultdict(set)
        initargs = (parser_type, worker_init_args or {})
        items_iterator, max_pending, futures = iter(items_to_process), num_workers * 2, {}
        ctx = get_context("spawn")
        with ProcessPoolExecutor(max_workers=num_workers, mp_context=ctx, initializer=_worker_initializer, initargs=initargs) as executor:
            def _next_batch(): return list(islice(items_iterator, batch_size))
            for _ in range(max_pending):
                batch = _next_batch()
                if not batch: break
                future = executor.submit(_parallel_worker, batch)
                futures[future] = len(batch)
            total_tus = len(items_to_process)
            with tqdm(total=total_tus, desc=desc) as pbar:
                while futures:
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        batch_count = futures.pop(future)
                        pbar.update(batch_count)
                        next_batch = _next_batch()
                        if next_batch:
                            nf = executor.submit(_parallel_worker, next_batch)
                            futures[nf] = len(next_batch)
                        try:
                            result_dict = future.result()
                            if result_dict["include_relations"]: all_includes.update(result_dict["include_relations"])
                            if result_dict["static_call_relations"]: all_static_calls.update(result_dict["static_call_relations"])
                            for alias_id, new_span in result_dict["type_alias_spans"].items():
                                existing = all_type_alias_spans.get(alias_id)
                                if not existing or (new_span.is_aliasee_definition and not existing.is_aliasee_definition):
                                    all_type_alias_spans[alias_id] = new_span
                            all_macro_spans.update(result_dict.get("macro_spans", {}))
                            for (file_uri, tu_hash), id_to_span_dict in result_dict["span_results"].items():
                                # TODO: We don't use file_tu_hash_map now, because a phony parent node span may be created after the header already being parsed. 
                                # In this case, since the already-parsed header file's hash is already in the map, the phony parent node cannot be added.
                                # Phony parent node is created when parsing a specialized member function, who does not have explicit partial specialized class defined.
                                # That leads to orphan members in the graph and finally be cleaned. 
                                if True: #tu_hash not in file_tu_hash_map[file_uri]:  
                                    file_tu_hash_map[file_uri].add(tu_hash)
                                    all_spans[file_uri].update(id_to_span_dict)
                        except Exception as e:
                            logger.error(f"Worker failure: {e}", exc_info=True)
        self.source_spans, self.include_relations, self.static_call_relations, self.type_alias_spans, self.macro_spans = all_spans, all_includes, all_static_calls, all_type_alias_spans, all_macro_spans
        gc.collect()

# --- Concrete Parsers ---
class ClangParser(CompilationParser):
    """Semantic parser implementation (USR-based identity)."""
    NODE_KIND_FUNCTIONS = {clang.cindex.CursorKind.FUNCTION_DECL.name, clang.cindex.CursorKind.FUNCTION_TEMPLATE.name}
    NODE_KIND_CONSTRUCTOR = {clang.cindex.CursorKind.CONSTRUCTOR.name}
    NODE_KIND_DESTRUCTOR = {clang.cindex.CursorKind.DESTRUCTOR.name}
    NODE_KIND_CONVERSION_FUNCTION = {clang.cindex.CursorKind.CONVERSION_FUNCTION.name}
    NODE_KIND_CXX_METHOD = {clang.cindex.CursorKind.CXX_METHOD.name}
    NODE_KIND_METHODS = NODE_KIND_CXX_METHOD | NODE_KIND_CONSTRUCTOR | NODE_KIND_DESTRUCTOR | NODE_KIND_CONVERSION_FUNCTION
    NODE_KIND_CALLERS = NODE_KIND_FUNCTIONS | NODE_KIND_METHODS
    NODE_KIND_UNION = {clang.cindex.CursorKind.UNION_DECL.name}
    NODE_KIND_ENUM = {clang.cindex.CursorKind.ENUM_DECL.name}
    NODE_KIND_STRUCT = {clang.cindex.CursorKind.STRUCT_DECL.name}
    NODE_KIND_CLASSES = {clang.cindex.CursorKind.CLASS_DECL.name, clang.cindex.CursorKind.CLASS_TEMPLATE.name, clang.cindex.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION.name}
    NODE_KIND_FOR_COMPOSITE_TYPES = NODE_KIND_UNION | NODE_KIND_ENUM | NODE_KIND_STRUCT | NODE_KIND_CLASSES
    NODE_KIND_FOR_BODY_SPANS = NODE_KIND_FUNCTIONS | NODE_KIND_METHODS | NODE_KIND_FOR_COMPOSITE_TYPES
    NODE_KIND_NAMESPACE = {clang.cindex.CursorKind.NAMESPACE.name}
    NODE_KIND_FOR_SCOPES =  NODE_KIND_NAMESPACE | NODE_KIND_FOR_COMPOSITE_TYPES
    NODE_KIND_TYPE_ALIASES = {clang.cindex.CursorKind.TYPE_ALIAS_TEMPLATE_DECL.name, clang.cindex.CursorKind.TYPE_ALIAS_DECL.name, clang.cindex.CursorKind.TYPEDEF_DECL.name}
    NODE_KIND_FOR_USER_DEFINED_TYPES = NODE_KIND_FOR_COMPOSITE_TYPES | NODE_KIND_TYPE_ALIASES

    # Kinds that represent semantic members of a composite type
    NODE_KIND_MEMBERS = NODE_KIND_CALLERS | {
        clang.cindex.CursorKind.FIELD_DECL.name,
        clang.cindex.CursorKind.ENUM_CONSTANT_DECL.name,
        clang.cindex.CursorKind.VAR_DECL.name, # For static members
    } | NODE_KIND_FOR_USER_DEFINED_TYPES # For nested types

    def parser_kind_to_index_kind(self, kind: str, lang: str) -> str:
        if kind in ClangParser.NODE_KIND_FUNCTIONS: return "Function"
        elif kind in ClangParser.NODE_KIND_CONSTRUCTOR: return "Constructor"
        elif kind in ClangParser.NODE_KIND_DESTRUCTOR: return "Destructor"
        elif kind in ClangParser.NODE_KIND_CONVERSION_FUNCTION: return "ConversionFunction"
        elif kind in ClangParser.NODE_KIND_CXX_METHOD: return "InstanceMethod"
        elif kind in ClangParser.NODE_KIND_STRUCT: return "Struct"
        elif kind in ClangParser.NODE_KIND_UNION: return "Union"
        elif kind in ClangParser.NODE_KIND_ENUM: return "Enum"
        elif kind in ClangParser.NODE_KIND_CLASSES: return "Class"
        elif kind in ClangParser.NODE_KIND_NAMESPACE: return "Namespace"
        elif kind in ClangParser.NODE_KIND_TYPE_ALIASES: return "TypeAlias"
        return "Unknown"

    def __init__(self, project_path: str, compile_commands_path: str):
        super().__init__(project_path)
        db_dir = self._get_db_dir(compile_commands_path)
        self.db = clang.cindex.CompilationDatabase.fromDirectory(db_dir)
        self.clang_include_path = self._get_clang_resource_dir()

    def _get_db_dir(self, path):
        p = Path(path).expanduser().resolve()
        if p.is_dir(): return str(p)
        if p.is_file():
            import tempfile, shutil
            tmpdir = tempfile.mkdtemp(prefix="clangdb_")
            shutil.copy(str(p), os.path.join(tmpdir, "compile_commands.json"))
            return tmpdir
        raise FileNotFoundError(path)

    def _get_clang_resource_dir(self):
        try: return os.path.join(subprocess.check_output(['clang', '-print-resource-dir']).decode('utf-8').strip(), 'include')
        except: return None

    def _get_cmd_file_realpath(self, cmd):
        f = cmd.filename
        if not os.path.isabs(f): f = os.path.join(cmd.directory, f)
        return os.path.realpath(f)

    def parse(self, files_to_parse: List[str], num_workers: int = 1):
        self.source_spans.clear(); self.include_relations.clear()
        source_files = [f for f in files_to_parse if f.lower().endswith(CompilationParser.ALL_SOURCE_EXTENSIONS)]
        cmd_files = {self._get_cmd_file_realpath(cmd): cmd for cmd in self.db.getAllCompileCommands()}
        compile_entries = [{'file': f, 'directory': cmd_files[f].directory, 'arguments': list(cmd_files[f].arguments)[1:]} for f in source_files if f in cmd_files]
        self._parallel_parse(compile_entries, 'clang', num_workers, "Parsing TUs (clang)", worker_init_args={'project_path': self.project_path, 'clang_include_path': self.clang_include_path})

class TreesitterParser(CompilationParser):
    """Syntactic parser implementation (Location-based identity)."""
    def __init__(self, project_path: str): super().__init__(project_path)
    def parse(self, files_to_parse: List[str], num_workers: int = 1):
        self.source_spans.clear(); self.include_relations.clear()
        valid = [f for f in files_to_parse if os.path.isfile(f)]
        self._parallel_parse(valid, 'treesitter', num_workers, "Parsing spans (treesitter)", worker_init_args={})
    def parser_kind_to_index_kind(self, kind: str, lang: str) -> str:
        return "Function" if kind == "function_definition" else "Unknown"
    def get_include_relations(self) -> Set[IncludeRelation]: return set()
