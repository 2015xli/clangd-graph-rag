#!/usr/bin/env python3
"""
Low-level multi-processing engine for the Clang parser.
"""

import os
import sys
import logging
import hashlib
import gc
from typing import List, Dict, Set, Tuple, Any, Optional
from collections import defaultdict
from urllib.parse import urlparse, unquote

import clang.cindex
from clangd_index_yaml_parser import RelativeLocation
from .types import SourceSpan, MacroSpan, TypeAliasSpan, IncludeRelation

logger = logging.getLogger(__name__)

# ============================================================
# Core Clang worker implementation
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
        self.span_results: Dict[Tuple[str, str], Dict[str, SourceSpan]] = None
        self.include_relations: Set[IncludeRelation] = None
        self.static_call_relations: Set[Tuple[str, str]] = None
        self.type_alias_spans: Dict[str, TypeAliasSpan] = {}
        self.macro_spans: Dict[str, MacroSpan] = {}
        self.instantiations: Dict[str, List[Any]] = defaultdict(list)

        # file-level cache to avoid re-processing header nodes in identical TU contexts
        self._global_header_cache:Dict[str, Set[str]] = defaultdict(set)
        self._processed_global_headers: Optional[Set[str]] = None

    def run(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        self.entry = entry
        self.span_results = defaultdict(dict)
        self.include_relations = set()
        self.static_call_relations = set()
        self.type_alias_spans: Dict[str, TypeAliasSpan] = {}
        self.macro_spans: Dict[str, MacroSpan] = {}
        self.instantiations = defaultdict(list)
        self._tu_hash = None
        self._local_header_cache: Set[str] = set()

        file_path = self.entry['file']
        dir_path = self.entry['directory']
        args = self.entry['arguments']
        original_dir = os.getcwd()
        try:
            os.chdir(dir_path)
            args = self._sanitize_args(args, file_path)
            self._tu_hash = sys.intern(self._get_tu_hash(args))
            self._processed_global_headers = self._global_header_cache.get(self._tu_hash, None)
            from .parser import CompilationParser
            self.lang = sys.intern(CompilationParser.get_language(file_path))
            self._parse_translation_unit(file_path, args)
        except Exception as e:
            logger.exception(f"Clang worker failed to parse {file_path}: {e}")
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
                    self.include_relations.add(IncludeRelation(sys.intern(src_file), sys.intern(include_file)))

        self._walk_ast(tu.cursor)

    def _walk_ast(self, root_node):
        from .parser import ClangParser
        stack = [(root_node, None)]
        while stack:
            node, current_caller_cursor = stack.pop()
            loc_file = node.location.file
            if loc_file: file_name = os.path.abspath(loc_file.name)
            elif node.kind == clang.cindex.CursorKind.TRANSLATION_UNIT: file_name = os.path.abspath(node.spelling)
            else: continue

            if not file_name.startswith(self.project_path): continue

            new_caller_cursor = current_caller_cursor
            if node.kind.name in ClangParser.NODE_KIND_CALLERS: new_caller_cursor = node

            self._process_node(node, file_name, current_caller_cursor)

            try:
                children = list(node.get_children())
                for c in reversed(children): stack.append((c, new_caller_cursor))
            except Exception: continue

    def _process_node(self, node, file_name, current_caller_cursor):
        from .parser import ClangParser, CompilationParser
        if node.kind == clang.cindex.CursorKind.MACRO_DEFINITION:
            if self._should_process_node(node, file_name): self._process_macro_definition(node, file_name)
        elif node.kind == clang.cindex.CursorKind.MACRO_INSTANTIATION:
            self.instantiations[f"file://{file_name}"].append(node)
        elif node.is_definition() and node.kind.name in ClangParser.NODE_KIND_FOR_BODY_SPANS:
            if node.kind.name in ClangParser.NODE_KIND_VARIABLES:
                parent = node.semantic_parent
                if parent and parent.kind.name in ClangParser.NODE_KIND_CALLERS: return
            if self._should_process_node(node, file_name): self._process_generic_node(node, file_name)
        elif node.kind.name in ClangParser.NODE_KIND_TYPE_ALIASES:
            if self._should_process_node(node, file_name): self._process_type_alias_node(node, file_name)
        elif node.kind == clang.cindex.CursorKind.CALL_EXPR and current_caller_cursor:
            callee_cursor = node.referenced
            if callee_cursor and callee_cursor.linkage == clang.cindex.LinkageKind.INTERNAL:
                caller_usr = current_caller_cursor.get_usr()
                callee_usr = callee_cursor.get_usr()
                if caller_usr and callee_usr:
                    self.static_call_relations.add((CompilationParser.hash_usr_to_id(caller_usr), CompilationParser.hash_usr_to_id(callee_usr)))

    def _process_macro_definition(self, node, file_name):
        from .parser import CompilationParser
        name, file_uri = node.spelling, f"file://{file_name}"
        loc = node.location
        name_line, name_col = loc.line - 1, loc.column - 1
        body_start_line, body_start_col = node.extent.start.line - 1, node.extent.start.column - 1
        body_end_line, body_end_col = node.extent.end.line - 1, node.extent.end.column - 1
        usr = node.get_usr()
        synthetic_id = CompilationParser.hash_usr_to_id(usr) if usr else CompilationParser.make_synthetic_id(CompilationParser.make_symbol_key(name, "Macro", file_uri, name_line, name_col))
        try: is_function_like = clang.cindex.conf.lib.clang_Cursor_isMacroFunctionLike(node)
        except Exception: is_function_like = False
        macro_definition = self._get_source_text_for_extent(node.extent, file_name)
        self.macro_spans[synthetic_id] = MacroSpan(
            id=synthetic_id, name=sys.intern(name), lang=self.lang, file_uri=sys.intern(file_uri),
            name_location=RelativeLocation(name_line, name_col, name_line, name_col + len(name)),
            body_location=RelativeLocation(body_start_line, body_start_col, body_end_line, body_end_col),
            is_function_like=bool(is_function_like), macro_definition=macro_definition
        )

    def _process_generic_node(self, node, file_name):
        from .parser import ClangParser, CompilationParser
        name_start_line, name_start_col = self._get_symbol_name_location(node)
        body_start_line, body_start_col = node.extent.start.line - 1, node.extent.start.column - 1
        body_end_line, body_end_col = node.extent.end.line - 1, node.extent.end.column - 1
        file_uri = f"file://{file_name}"
        kind = self._convert_node_kind_to_index_kind(node)
        usr = node.get_usr()
        synthetic_id = CompilationParser.hash_usr_to_id(usr) if usr else CompilationParser.make_synthetic_id(CompilationParser.make_symbol_key(node.spelling, kind, file_uri, name_start_line, name_start_col))
        if synthetic_id in self.span_results[(file_uri, self._tu_hash)]: return  
        parent_id = self._get_parent_id(node)
        original_name, expanded_from_id = self._get_macro_causality(node, file_uri)
        member_ids = []
        if node.kind.name in ClangParser.NODE_KIND_FOR_COMPOSITE_TYPES:
            for child in node.get_children():
                if child.kind.name in ClangParser.NODE_KIND_MEMBERS:
                    child_usr = child.get_usr()
                    if child_usr: member_ids.append(CompilationParser.hash_usr_to_id(child_usr))
        self.span_results[(sys.intern(file_uri), self._tu_hash)][synthetic_id] = SourceSpan(
            name=sys.intern(usr if (not node.spelling or "(unnamed" in node.spelling) and usr else node.spelling),
            kind=sys.intern(kind), lang=self.lang,
            name_location=RelativeLocation(name_start_line, name_start_col, name_start_line, name_start_col + len(node.spelling)),
            body_location=RelativeLocation(body_start_line, body_start_col, body_end_line, body_end_col),
            id=synthetic_id, parent_id=parent_id, original_name=original_name, expanded_from_id=expanded_from_id, member_ids=member_ids
        )

    def _process_type_alias_node(self, node, file_name):
        from .parser import ClangParser, CompilationParser
        semantic_parent = node.semantic_parent
        if semantic_parent and semantic_parent.kind.name in ClangParser.NODE_KIND_CALLERS: return
        name_start_line, name_start_col = self._get_symbol_name_location(node)
        body_start_line, body_start_col = node.extent.start.line - 1, node.extent.start.column - 1
        body_end_line, body_end_col = node.extent.end.line - 1, node.extent.end.column - 1
        file_uri = f"file://{file_name}"
        aliaser_id = CompilationParser.hash_usr_to_id(node.get_usr())
        underlying_type = node.underlying_typedef_type
        aliasee_decl_cursor = underlying_type.get_declaration()
        aliased_type_id, aliased_type_kind, is_aliasee_definition = None, None, False
        if aliasee_decl_cursor.kind.name not in {"NO_DECL_FOUND", "TEMPLATE_TEMPLATE_PARAMETER"}:
            is_aliasee_definition = aliasee_decl_cursor.is_definition()
            aliased_type_kind = self._convert_node_kind_to_index_kind(aliasee_decl_cursor)
            if aliasee_decl_cursor.location.file:
                aliased_usr = aliasee_decl_cursor.get_usr()
                aliased_type_id = CompilationParser.hash_usr_to_id(aliased_usr) if aliased_usr else CompilationParser.make_synthetic_id(CompilationParser.make_symbol_key(aliasee_decl_cursor.spelling, aliased_type_kind, f"file://{os.path.abspath(aliasee_decl_cursor.location.file.name)}", *self._get_symbol_name_location(aliasee_decl_cursor)))
                if not os.path.abspath(aliasee_decl_cursor.location.file.name).startswith(self.project_path): aliased_type_id = None
        original_name, expanded_from_id = self._get_macro_causality(node, file_uri)
        new_span = TypeAliasSpan(
            id=aliaser_id, file_uri=file_uri, lang=self.lang, name=node.spelling,
            name_location=RelativeLocation(name_start_line, name_start_col, name_start_line, name_start_col + len(node.spelling)),
            body_location=RelativeLocation(body_start_line, body_start_col, body_end_line, body_end_col),
            aliased_canonical_spelling=underlying_type.get_canonical().spelling,
            aliased_type_id=aliased_type_id, aliased_type_kind=aliased_type_kind, is_aliasee_definition=is_aliasee_definition,
            scope=self._get_fully_qualified_scope(semantic_parent), parent_id=self._get_parent_id(node),
            original_name=original_name, expanded_from_id=expanded_from_id
        )
        existing = self.type_alias_spans.get(aliaser_id)
        if not existing or (new_span.is_aliasee_definition and not existing.is_aliasee_definition):
            self.type_alias_spans[aliaser_id] = new_span

    def _should_process_node(self, node, file_name) -> bool:
        from .parser import CompilationParser
        if file_name != self.entry['file'] and not file_name.endswith(CompilationParser.VOLATILE_HEADER_EXTENSIONS):
            if self._processed_global_headers and file_name in self._processed_global_headers: return False
            self._local_header_cache.add(file_name)
        return True

    def _get_parent_id(self, node) -> Optional[str]:
        from .parser import ClangParser, CompilationParser
        parent = node.semantic_parent
        if not parent or parent.kind in (clang.cindex.CursorKind.TRANSLATION_UNIT, clang.cindex.CursorKind.LINKAGE_SPEC): return None
        file_name = parent.location.file.name if parent.location.file else parent.translation_unit.spelling
        if not file_name: return None
        if parent.kind.name not in ClangParser.NODE_KIND_FOR_BODY_SPANS:
            if parent.kind.name not in "ClangParser.NODE_KIND_NAMESPACE": return None
        usr = parent.get_usr()
        if usr:
            parent_id = CompilationParser.hash_usr_to_id(usr)
            if parent.kind.name in ClangParser.NODE_KIND_FOR_COMPOSITE_TYPES:
                parent_uri = f"file://{os.path.abspath(file_name)}"
                if parent_id not in self.span_results[(parent_uri, self._tu_hash)]: self._process_generic_node(parent, file_name)
            return parent_id
        return CompilationParser.make_synthetic_id(CompilationParser.make_symbol_key(parent.spelling, self._convert_node_kind_to_index_kind(parent), f"file://{os.path.abspath(file_name)}", *self._get_symbol_name_location(parent)))

    def _get_symbol_name_location(self, node):
        for tok in node.get_tokens():
            if tok.spelling == node.spelling:
                loc = tok.location
                try: file, line, col, _ = loc.get_expansion_location()
                except AttributeError: continue
                if file and file.name.startswith(self.project_path): return (line - 1, col - 1)
        loc = node.location
        try: file, line, col, _ = loc.get_expansion_location(); return (line - 1, col - 1)
        except AttributeError: return (node.location.line - 1, node.location.column - 1)

    def _get_macro_causality(self, node, file_uri: str) -> Tuple[Optional[str], Optional[str]]:
        from .parser import CompilationParser
        instantiations = self.instantiations.get(file_uri, [])
        if not instantiations: return None, None
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
                if enclosing_inst is None or extent_contains(inst.extent, enclosing_inst.extent): enclosing_inst = inst
        if not enclosing_inst: return None, None
        macro_def_cursor = enclosing_inst.referenced
        if not macro_def_cursor or not macro_def_cursor.location.file: return None, None
        def_file = os.path.abspath(macro_def_cursor.location.file.name)
        if not def_file.startswith(self.project_path) or not node.is_definition(): return None, None
        usr = macro_def_cursor.get_usr()
        expanded_from_id = CompilationParser.hash_usr_to_id(usr) if usr else CompilationParser.make_synthetic_id(CompilationParser.make_symbol_key(macro_def_cursor.spelling, "Macro", f"file://{def_file}", macro_def_cursor.location.line - 1, macro_def_cursor.location.column - 1))
        return self._get_source_text_for_extent(enclosing_inst.extent, unquote(urlparse(file_uri).path)), expanded_from_id

    def _get_source_text_for_extent(self, extent, file_path: str) -> str:
        try:
            with open(file_path, 'r', errors='ignore') as f: lines = f.readlines()
            s_line, s_col, e_line, e_col = extent.start.line - 1, extent.start.column - 1, extent.end.line - 1, extent.end.column - 1
            if s_line == e_line: return lines[s_line][s_col:e_col]
            else:
                res = [lines[s_line][s_col:]]
                for i in range(s_line + 1, e_line): res.append(lines[i])
                res.append(lines[e_line][:e_col])
                return "".join(res)
        except Exception as e:
            logger.error(f"Error reading source text: {e}")
            return ""

    def _get_fully_qualified_scope(self, node: clang.cindex.Cursor) -> str:
        from .parser import ClangParser
        scope_parts = []
        current = node.semantic_parent
        while current and current.kind != clang.cindex.CursorKind.TRANSLATION_UNIT:
            if current.kind.name in ClangParser.NODE_KIND_FOR_SCOPES:
                name = current.spelling
                scope_parts.append(name if name else f"(anonymous {current.kind.name})")
            current = current.semantic_parent
        return "::".join(reversed(scope_parts)) + "::" if scope_parts else ""

    def _convert_node_kind_to_index_kind(self, node):
        from .parser import ClangParser
        kind_name = node.kind.name
        if kind_name in ClangParser.NODE_KIND_FUNCTIONS:
            parent = node.semantic_parent
            if parent and parent.kind.name in ClangParser.NODE_KIND_FOR_COMPOSITE_TYPES: return "StaticMethod" if node.is_static_method() else "InstanceMethod"
            return "Function"
        elif kind_name in ClangParser.NODE_KIND_CONSTRUCTOR: return "Constructor"
        elif kind_name in ClangParser.NODE_KIND_DESTRUCTOR: return "Destructor"
        elif kind_name in ClangParser.NODE_KIND_CONVERSION_FUNCTION: return "ConversionFunction"
        elif kind_name in ClangParser.NODE_KIND_CXX_METHOD: return "StaticMethod" if node.is_static_method() else "InstanceMethod"
        elif kind_name in ClangParser.NODE_KIND_STRUCT: return "Struct"
        elif kind_name in ClangParser.NODE_KIND_UNION: return "Union"
        elif kind_name in ClangParser.NODE_KIND_ENUM: return "Enum"
        elif kind_name in ClangParser.NODE_KIND_CLASSES: return ClangParser._identify_template_type(node) if kind_name != clang.cindex.CursorKind.CLASS_DECL.name else "Class"
        elif kind_name in ClangParser.NODE_KIND_NAMESPACE: return "Namespace"
        elif kind_name in ClangParser.NODE_KIND_TYPE_ALIASES: return "TypeAlias"
        elif kind_name in ClangParser.NODE_KIND_VARIABLES:
            parent = node.semantic_parent
            return "StaticProperty" if parent and parent.kind.name in ClangParser.NODE_KIND_FOR_COMPOSITE_TYPES else "Variable"
        return "Unknown"

    def _sanitize_args(self, args: List[str], file_path: str) -> List[str]:
        sanitized = []
        skip_next = False
        for a in args:
            if skip_next: skip_next = False; continue
            if a == '--' or a.startswith(('-W', '-O')): continue
            if a in {'-c', '-o', '-MMD', '-MF', '-MT', '-MQ', '-fcolor-diagnostics', '-fdiagnostics-color'}:
                if a in {'-o', '-MF', '-MT', '-MQ'}: skip_next = True
                continue
            if a == file_path or os.path.basename(a) == os.path.basename(file_path): continue
            sanitized.append(a)
        return sanitized

    def _get_tu_hash(self, args: List[str]) -> str:
        b_lang, b_macros, b_features, b_includes, b_other = [], [], [], [], []
        i = 0
        while i < len(args):
            a = args[i]
            if a.startswith(("-std=", "-x", "--driver-mode")): b_lang.append(a)
            elif a.startswith(("-D", "-U")): b_macros.append(a)
            elif a.startswith(("-f", "-m", "--target=")): b_features.append(a)
            elif a in ('-I', '-isystem', '-iquote', '-include'):
                b_includes.append(a)
                if i + 1 < len(args): b_includes.append(args[i + 1]); i += 1 
            elif a.startswith(("-I", "-isystem", "-iquote")): b_includes.append(a)
            else: b_other.append(a)
            i += 1
        return hashlib.md5(" ".join(b_lang + b_macros + b_features + b_includes + b_other).encode("utf-8")).hexdigest()

# --- Worker Orchestration ---
_worker_impl_instance = None
_count_processed_tus = 0

def _worker_initializer(init_args: Dict[str, Any]):
    global _worker_impl_instance
    sys.setrecursionlimit(3000)
    _worker_impl_instance = _ClangWorkerImpl(**init_args)

def _parallel_worker(data: Any) -> Dict[str, Any]:
    global _worker_impl_instance
    global _count_processed_tus
    if _worker_impl_instance is None: raise RuntimeError("Worker not initialized.")
    if len(data) == 1:
        entry = data[0]
        _count_processed_tus += 1
        if _count_processed_tus % 1000 == 0: gc.collect()
        try: return _worker_impl_instance.run(entry)
        except Exception:
            logger.exception(f"Worker failed on {entry}")
            return {"span_results": defaultdict(dict), "include_relations": set(), "static_call_relations": set(), "type_alias_spans": {}, "macro_spans": {}}
    res_spans, res_inc, res_call, res_type, res_macro = defaultdict(dict), set(), set(), {}, {}
    for entry in data:
        _count_processed_tus += 1
        if _count_processed_tus % 1000 == 0: gc.collect()
        try:
            r = _worker_impl_instance.run(entry)
            res_spans.update(r["span_results"]); res_inc.update(r["include_relations"]); res_call.update(r["static_call_relations"]); res_macro.update(r.get("macro_spans", {}))
            for k, v in r["type_alias_spans"].items():
                if k not in res_type or (v.is_aliasee_definition and not res_type[k].is_aliasee_definition): res_type[k] = v
        except Exception: continue
    return {"span_results": res_spans, "include_relations": res_inc, "static_call_relations": res_call, "type_alias_spans": res_type, "macro_spans": res_macro}
