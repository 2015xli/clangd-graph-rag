#!/usr/bin/env python3
"""
Worker implementation for Clang-based AST parsing and data extraction.
"""
import os
import logging
import sys
import clang.cindex
from typing import List, Dict, Set, Tuple, Any, Optional
from collections import defaultdict

from symbol_parser import RelativeLocation, Location
from utils import hash_usr_to_id, make_symbol_key, make_synthetic_id, get_language, FileExtensions
from .node_parser import NodeParserMixin
from .types import *

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# ============================================================
# Core Clang worker
# ============================================================

class _ClangWorkerImpl(NodeParserMixin):
    """Parses a single compilation entry and extracts SourceSpans + include relations."""

    def __init__(self, project_path: str, clang_include_path: str):
        self.project_path = os.path.abspath(project_path)
        if not self.project_path.endswith(os.sep):
            self.project_path += os.sep
            
        self.clang_include_path = clang_include_path
        self.index = clang.cindex.Index.create()
        
        # Identity related state (returned per-run)
        self.span_results = None
        self.include_relations = None
        self.static_call_relations = None
        self.type_alias_spans = {}
        self.macro_spans = {}
        self.instantiations = defaultdict(list)
        
        # Internal state
        self._tu_hash = None
        self.lang = None
        self.entry = None

        # file-level cache to avoid re-processing header nodes in identical TU contexts
        self._global_header_cache: Dict[str, Set[str]] = defaultdict(set)
        self._processed_global_headers: Optional[Set[str]] = None

    def run(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Parse the entry and return a dictionary of results."""
        self.entry = entry
        self.span_results = defaultdict(dict)
        self.include_relations = set()
        self.static_call_relations = set()
        self.type_alias_spans = {}
        self.macro_spans = {}
        self.instantiations = defaultdict(list)
        self._local_header_cache: Set[str] = set()

        file_path = entry['file']
        dir_path = entry['directory']
        args = entry['arguments']
        original_dir = os.getcwd()
        
        try:
            os.chdir(dir_path)
            args = self._sanitize_args(args, file_path)
            self._tu_hash = sys.intern(self._get_tu_hash(args))
            self._processed_global_headers = self._global_header_cache.get(self._tu_hash, None)
            self.lang = sys.intern(get_language(file_path))

            self._parse_translation_unit(file_path, args)

        except Exception as e:
            logger.exception(f"Clang worker error on {file_path}: {e}")
        finally:
            os.chdir(original_dir)

        self._global_header_cache[self._tu_hash].update(self._local_header_cache)

        return {
            "span_results": self.span_results,
            "include_relations": self.include_relations,
            "static_call_relations": self.static_call_relations,
            "type_alias_spans": self.type_alias_spans,
            "macro_spans": self.macro_spans
        }

    def _parse_translation_unit(self, file_path: str, args: List[str]):
        if self.clang_include_path:
            args = args + [f"-I{self.clang_include_path}"]

        tu = self.index.parse(
            file_path,
            args=args,
            options=clang.cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
        )

        for inc in tu.get_includes():
            if inc.source and inc.include:
                src_file = os.path.abspath(inc.source.name)
                include_file = os.path.abspath(inc.include.name)
                if src_file.startswith(self.project_path) and include_file.startswith(self.project_path):
                    self.include_relations.add(IncludeRelation(
                        source_file=sys.intern(src_file), 
                        included_file=sys.intern(include_file)
                    ))

        self._walk_ast(tu.cursor)

    def _walk_ast(self, root_node):
        """Iteratively walks the AST using an explicit stack."""
        stack = [(root_node, None)]
        while stack:
            node, current_caller_cursor = stack.pop()
            loc_file = node.location.file
            if loc_file:
                file_name = os.path.abspath(loc_file.name)
            elif node.kind == clang.cindex.CursorKind.TRANSLATION_UNIT:
                file_name = os.path.abspath(node.spelling)
            else:
                continue

            if not file_name.startswith(self.project_path):
                continue

            new_caller_cursor = current_caller_cursor
            if node.kind.name in NODE_KIND_CALLERS:
                new_caller_cursor = node

            # The Mixin handles this
            self._process_node(node, file_name, current_caller_cursor)

            try:
                children = list(node.get_children())
                for c in reversed(children): 
                    stack.append((c, new_caller_cursor))
            except Exception:
                continue

    def _process_node(self, node, file_name, current_caller_cursor):
        if node.kind == clang.cindex.CursorKind.MACRO_DEFINITION:
            if self._should_process_node(node, file_name):
                self._process_macro_definition(node, file_name)
        elif node.kind == clang.cindex.CursorKind.MACRO_INSTANTIATION:
            self.instantiations[f"file://{file_name}"].append(node)
        elif node.is_definition() and node.kind.name in NODE_KIND_FOR_BODY_SPANS:
            if node.kind.name in NODE_KIND_VARIABLES:
                parent = node.semantic_parent
                if parent and parent.kind.name in NODE_KIND_CALLERS:
                    return 
            if self._should_process_node(node, file_name):
                self._process_generic_node(node, file_name)
        elif node.kind.name in NODE_KIND_TYPE_ALIASES:            
            if self._should_process_node(node, file_name):
                self._process_type_alias_node(node, file_name)
        elif node.kind == clang.cindex.CursorKind.CALL_EXPR and current_caller_cursor:
            callee_cursor = node.referenced
            if callee_cursor and callee_cursor.linkage == clang.cindex.LinkageKind.INTERNAL:
                caller_usr = current_caller_cursor.get_usr()
                callee_usr = callee_cursor.get_usr()
                if caller_usr and callee_usr:
                    self.static_call_relations.add((hash_usr_to_id(caller_usr), hash_usr_to_id(callee_usr)))

    def _should_process_node(self, node, file_name) -> bool:
        if file_name != self.entry['file'] and not file_name.endswith(FileExtensions.VOLATILE_HEADER):
            if self._processed_global_headers and file_name in self._processed_global_headers:
                return False
            self._local_header_cache.add(file_name)
        return True

    def _get_parent_id(self, node) -> Optional[str]:
        """Retrieves the parent_id for a node based on its semantic parent."""
        parent = node.semantic_parent
        if not parent or parent.kind == clang.cindex.CursorKind.TRANSLATION_UNIT or parent.kind == clang.cindex.CursorKind.LINKAGE_SPEC:
            return None
        file_name = parent.location.file.name if parent.location.file else parent.translation_unit.spelling
        if not file_name: return None
        if parent.kind.name not in NODE_KIND_FOR_BODY_SPANS:
            if parent.kind.name not in NODE_KIND_NAMESPACE: 
                logger.error(f"Parent {parent.kind.name} ({parent.spelling}) of node {node.spelling} is not valid.")
                return None
        usr = parent.get_usr()
        if usr:
            parent_id = hash_usr_to_id(usr)
            if parent.kind.name in NODE_KIND_FOR_COMPOSITE_TYPES:
                parent_uri = f"file://{os.path.abspath(file_name)}"
                if parent_id not in self.span_results[(parent_uri, self._tu_hash)]:
                    self._process_generic_node(parent, file_name)
            return parent_id
        file_uri = f"file://{os.path.abspath(file_name)}"
        line, col = self._get_symbol_name_location(parent)
        parent_kind = self._convert_node_kind_to_index_kind(parent)
        return make_synthetic_id(make_symbol_key(parent.spelling, parent_kind, file_uri, line, col))

    def _identify_template_type(self, node) -> str:
        from itertools import islice
        tokens = islice(node.get_tokens(), 100)
        bracket_depth, found_params = 0, False
        for token in tokens:
            s = token.spelling
            if s == '<': bracket_depth += 1; found_params = True
            elif s == '>': bracket_depth -= 1
            elif found_params and bracket_depth == 0:
                tag = s.lower()
                if tag in ('struct', 'class', 'union'): return tag.capitalize()
        return "Class"

    def _convert_node_kind_to_index_kind(self, node):
        kind_name = node.kind.name
        if kind_name in NODE_KIND_FUNCTIONS:
            parent = node.semantic_parent
            if parent and parent.kind.name in NODE_KIND_FOR_COMPOSITE_TYPES:
                if node.is_static_method(): return "StaticMethod"
                return "InstanceMethod"
            return "Function"
        elif kind_name in NODE_KIND_CONSTRUCTOR: return "Constructor"
        elif kind_name in NODE_KIND_DESTRUCTOR: return "Destructor"
        elif kind_name in NODE_KIND_CONVERSION_FUNCTION: return "ConversionFunction"
        elif kind_name in NODE_KIND_CXX_METHOD:
            if node.is_static_method(): return "StaticMethod"
            return "InstanceMethod"
        elif kind_name in NODE_KIND_STRUCT: return "Struct"
        elif kind_name in NODE_KIND_UNION: return "Union"
        elif kind_name in NODE_KIND_ENUM: return "Enum"
        elif kind_name in NODE_KIND_CLASSES:
            if kind_name != clang.cindex.CursorKind.CLASS_DECL.name: return self._identify_template_type(node)
            return "Class"
        elif kind_name in NODE_KIND_NAMESPACE: return "Namespace"
        elif kind_name in NODE_KIND_TYPE_ALIASES: return "TypeAlias"
        elif kind_name in NODE_KIND_VARIABLES:
            parent = node.semantic_parent
            if parent and parent.kind.name in NODE_KIND_FOR_COMPOSITE_TYPES: return "StaticProperty"
            return "Variable"
        return "Unknown"

    def _sanitize_args(self, args: List[str], file_path: str) -> List[str]:
        sanitized, skip_next = [], False
        for a in args:
            if skip_next: skip_next = False; continue
            if a == '--': continue
            if a.startswith(('-W', '-O')): continue
            if a in {'-c', '-o', '-MMD', '-MF', '-MT', '-MQ', '-fcolor-diagnostics', '-fdiagnostics-color'}:
                if a in {'-o', '-MF', '-MT', '-MQ'}: skip_next = True
                continue
            if a == file_path or os.path.basename(a) == os.path.basename(file_path): continue
            sanitized.append(a)
        return sanitized

    def _get_tu_hash(self, args: List[str]) -> str:
        import hashlib
        bucket_lang, bucket_macros, bucket_features, bucket_includes, bucket_other = [], [], [], [], []
        i = 0
        while i < len(args):
            a = args[i]
            if a.startswith(("-std=", "-x", "--driver-mode")): bucket_lang.append(a)
            elif a.startswith(("-D", "-U")): bucket_macros.append(a)
            elif a.startswith(("-f", "-m", "--target=")): bucket_features.append(a)
            elif a in ('-I', '-isystem', '-iquote', '-include'):
                bucket_includes.append(a)
                if i + 1 < len(args): bucket_includes.append(args[i + 1]); i += 1 
            elif a.startswith(("-I", "-isystem", "-iquote")): bucket_includes.append(a)
            else: bucket_other.append(a)
            i += 1
        hash_input = " ".join(bucket_lang + bucket_macros + bucket_features + bucket_includes + bucket_other)
        return hashlib.md5(hash_input.encode("utf-8")).hexdigest()
