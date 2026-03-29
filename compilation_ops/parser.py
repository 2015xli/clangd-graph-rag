#!/usr/bin/env python3
"""
High-level parser interface for extracting data from source code.
"""

import os
import logging
import subprocess
import sys
from typing import List, Dict, Set, Tuple, Any, Optional
from pathlib import Path
from collections import defaultdict

import clang.cindex
from .types import SourceSpan, MacroSpan, TypeAliasSpan, IncludeRelation
from .engine import _worker_initializer, _parallel_worker

logger = logging.getLogger(__name__)

class CompilationParser:
    """Abstract base class for source code parsers."""
    C_SOURCE_EXTENSIONS = ('.c',)
    CPP_SOURCE_EXTENSIONS = ('.cpp', '.cc', '.cxx')
    CPP20_MODULE_EXTENSIONS = ('.cppm', '.ccm', '.cxxm', '.c++m')
    ALL_CPP_SOURCE_EXTENSIONS = CPP_SOURCE_EXTENSIONS + CPP20_MODULE_EXTENSIONS
    ALL_SOURCE_EXTENSIONS = C_SOURCE_EXTENSIONS + ALL_CPP_SOURCE_EXTENSIONS

    VOLATILE_HEADER_EXTENSIONS = ('.inc', '.def')
    C_HEADER_EXTENSIONS = ('.h',) + VOLATILE_HEADER_EXTENSIONS
    CPP_HEADER_EXTENSIONS = ('.hpp', '.hh', '.hxx', '.h++') + C_HEADER_EXTENSIONS
    ALL_HEADER_EXTENSIONS = CPP_HEADER_EXTENSIONS
    ALL_C_CPP_EXTENSIONS = ALL_SOURCE_EXTENSIONS + ALL_HEADER_EXTENSIONS
    
    def __init__(self, project_path: str):
        self.project_path = project_path
        self.source_spans: Dict[str, Dict[str, SourceSpan]] = defaultdict(dict)
        self.include_relations: Set[IncludeRelation] = set()
        self.static_call_relations: Set[Tuple[str, str]] = set()
        self.type_alias_spans: Dict[str, TypeAliasSpan] = {}
        self.macro_spans: Dict[str, MacroSpan] = {}

    def parse(self, files_to_parse: List[str], num_workers: int = 1): raise NotImplementedError
    def get_source_spans(self): return self.source_spans
    def get_include_relations(self): return self.include_relations
    def get_static_call_relations(self): return self.static_call_relations
    def get_type_alias_spans(self): return self.type_alias_spans
    def get_macro_spans(self): return self.macro_spans
    
    @staticmethod
    def hash_usr_to_id(usr: str) -> str:
        import hashlib
        return hashlib.sha1(usr.encode()).digest()[:8].hex().upper()

    @classmethod
    def make_symbol_key(cls, name: str, kind: str, file_uri: str, line: int, col: int) -> str:
        return f"{kind}::{name}::{file_uri}:{line}:{col}"

    @classmethod
    def make_synthetic_id(cls, key: str) -> str:
        import hashlib
        return hashlib.md5(key.encode()).hexdigest()

    @classmethod
    def get_language(cls, file_name: str) -> str:
        ext = os.path.splitext(file_name)[1].lower()
        if ext in cls.CPP_SOURCE_EXTENSIONS or ext in cls.CPP_HEADER_EXTENSIONS or ext in cls.CPP20_MODULE_EXTENSIONS: return "Cpp"
        if ext in cls.C_SOURCE_EXTENSIONS or ext in cls.C_HEADER_EXTENSIONS: return "C"
        return "Unknown"

    def _parallel_parse(self, items_to_process: List, num_workers: int, desc: str, worker_init_args: Dict[str, Any] = None, batch_size: int = 1):
        from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
        from multiprocessing import get_context
        from itertools import islice
        from tqdm import tqdm
        
        all_spans, all_includes, all_static_calls, all_type_alias_spans, all_macro_spans = defaultdict(dict), set(), set(), {}, {}
        file_tu_hash_map = defaultdict(set)
        initargs = (worker_init_args or {})
        items_iterator, max_pending, futures = iter(items_to_process), num_workers * 2, {}
        ctx = get_context("spawn")
        
        with ProcessPoolExecutor(max_workers=num_workers, mp_context=ctx, initializer=_worker_initializer, initargs=(initargs,)) as executor:
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
                            r = future.result()
                            if r["include_relations"]: all_includes.update(r["include_relations"])
                            if r["static_call_relations"]: all_static_calls.update(r["static_call_relations"])
                            for k, v in r["type_alias_spans"].items():
                                if k not in all_type_alias_spans or (v.is_aliasee_definition and not all_type_alias_spans[k].is_aliasee_definition):
                                    all_type_alias_spans[k] = v
                            all_macro_spans.update(r.get("macro_spans", {}))
                            for (file_uri, tu_hash), id_to_span_dict in r["span_results"].items():
                                file_tu_hash_map[file_uri].add(tu_hash)
                                all_spans[file_uri].update(id_to_span_dict)
                        except Exception as e:
                            logger.error(f"Worker failure: {e}", exc_info=True)
                            
        self.source_spans, self.include_relations, self.static_call_relations, self.type_alias_spans, self.macro_spans = all_spans, all_includes, all_static_calls, all_type_alias_spans, all_macro_spans
        import gc
        gc.collect()

class ClangParser(CompilationParser):
    """Semantic parser implementation (USR-based identity)."""
    NODE_KIND_VARIABLES = {clang.cindex.CursorKind.VAR_DECL.name}
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
    NODE_KIND_FOR_BODY_SPANS = NODE_KIND_FUNCTIONS | NODE_KIND_METHODS | NODE_KIND_FOR_COMPOSITE_TYPES | NODE_KIND_VARIABLES
    NODE_KIND_NAMESPACE = {clang.cindex.CursorKind.NAMESPACE.name}
    NODE_KIND_FOR_SCOPES =  NODE_KIND_NAMESPACE | NODE_KIND_FOR_COMPOSITE_TYPES
    NODE_KIND_TYPE_ALIASES = {clang.cindex.CursorKind.TYPE_ALIAS_TEMPLATE_DECL.name, clang.cindex.CursorKind.TYPE_ALIAS_DECL.name, clang.cindex.CursorKind.TYPEDEF_DECL.name}
    NODE_KIND_FOR_USER_DEFINED_TYPES = NODE_KIND_FOR_COMPOSITE_TYPES | NODE_KIND_TYPE_ALIASES

    NODE_KIND_MEMBERS = NODE_KIND_CALLERS | {
        clang.cindex.CursorKind.FIELD_DECL.name,
        clang.cindex.CursorKind.ENUM_CONSTANT_DECL.name,
        clang.cindex.CursorKind.VAR_DECL.name,
    } | NODE_KIND_FOR_USER_DEFINED_TYPES

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

    @staticmethod
    def _identify_template_type(node) -> str:
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

    def parse(self, files_to_parse: List[str], num_workers: int = 1):
        self.source_spans.clear(); self.include_relations.clear()
        source_files = [f for f in files_to_parse if f.lower().endswith(CompilationParser.ALL_SOURCE_EXTENSIONS)]
        cmd_files = {self._get_cmd_file_realpath(cmd): cmd for cmd in self.db.getAllCompileCommands()}
        compile_entries = [{'file': f, 'directory': cmd_files[f].directory, 'arguments': list(cmd_files[f].arguments)[1:]} for f in source_files if f in cmd_files]
        self._parallel_parse(compile_entries, num_workers, "Parsing TUs (clang)", worker_init_args={'project_path': self.project_path, 'clang_include_path': self.clang_include_path})
