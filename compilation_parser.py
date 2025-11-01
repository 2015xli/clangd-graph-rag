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
from typing import List, Dict, Set, Tuple, Callable, Any
from pathlib import Path
from collections import defaultdict
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor, as_completed
import gc

# Optional imports for concrete implementations
try:
    import clang.cindex
except ImportError:
    clang = None

try:
    import tree_sitter_c as tsc
    from tree_sitter import Language, Parser as TreeSitterParser
except ImportError:
    tsc = None
    TreeSitterParser = None

logger = logging.getLogger(__name__)

# --- Worker Implementations ---
# These classes encapsulate the logic for a single unit of work.

class _ClangWorkerImpl:
    """Contains the logic to parse one file using clang."""
    def __init__(self, project_path: str, clang_include_path: str):
        self.project_path = project_path
        self.clang_include_path = clang_include_path
        self.index = clang.cindex.Index.create()
        self.entry = None
        self.span_results = None
        self.include_relations = None
        self.processed_headers = None

    def run(self, entry: Dict[str, Any]) -> Tuple[List[Dict], Set[Tuple[str, str]]]:
        self.entry = entry
        self.span_results = defaultdict(list)
        self.include_relations = set()
        self.processed_headers = set()

        file_path = self.entry['file']
        original_dir = os.getcwd()
        try:
            os.chdir(self.entry['directory'])
            self._parse_translation_unit(file_path)
        except clang.cindex.TranslationUnitLoadError as e:
            logger.error(f"Clang worker failed to parse {file_path}: {e}")
        except Exception as e:
            logger.error(f"Clang worker had an unexpected error on {file_path}: {e}")
        finally:
            os.chdir(original_dir)

        span_results_list = [
            {"FileURI": file_uri, "Spans": spans}
            for file_uri, spans in self.span_results.items()
        ]
        return span_results_list, self.include_relations

    def _parse_translation_unit(self, file_path: str):
        args = self.entry['arguments']
        sanitized_args = []
        skip_next = False
        for a in args:
            if skip_next: skip_next = False; continue
            if a in {'-c', '-o', '-MMD', '-MF', '-MT', '-fcolor-diagnostics', '-fdiagnostics-color'}: skip_next = True; continue
            if a == file_path or os.path.basename(a) == os.path.basename(file_path): continue
            sanitized_args.append(a)

        if self.clang_include_path: sanitized_args.append(f"-I{self.clang_include_path}")

        tu = self.index.parse(file_path, args=sanitized_args, options=clang.cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
        
        for inc in tu.get_includes():
            if inc.source and inc.include:
                self.include_relations.add((os.path.abspath(inc.source.name), os.path.abspath(inc.include.name)))

        self._walk_ast(tu.cursor)

    def _walk_ast(self, node):
        file_name = node.location.file.name if node.location.file else node.translation_unit.spelling
        if not file_name or not file_name.startswith(self.project_path):
            return
        
        if node.kind == clang.cindex.CursorKind.FUNCTION_DECL and node.is_definition():
            self._process_function_node(node, file_name)
        elif node.kind in (clang.cindex.CursorKind.STRUCT_DECL, clang.cindex.CursorKind.UNION_DECL, clang.cindex.CursorKind.ENUM_DECL) and node.is_definition():
            self._process_structure_node(node, file_name)

        for c in node.get_children():
            self._walk_ast(c)

    def _process_function_node(self, node, file_name):
        is_header = file_name.endswith('.h')
        func_sig = (file_name, node.spelling, node.location.line, node.location.column)

        if is_header and func_sig in self.processed_headers:
            return
        if is_header: self.processed_headers.add(func_sig)
        
        name_start_line, name_start_col = self.get_symbol_name_location(node)
        body_start_line, body_start_col = (node.extent.start.line - 1, node.extent.start.column - 1)
        body_end_line, body_end_col = (node.extent.end.line - 1, node.extent.end.column - 1)
        
        span_data = {
            "Name": node.spelling, "Kind": "Function",
            "NameLocation": {"Start": {"Line": name_start_line, "Column": name_start_col}, "End": {"Line": name_start_line, "Column": name_start_col + len(node.spelling)}},
            "BodyLocation": {"Start": {"Line": body_start_line, "Column": body_start_col}, "End": {"Line": body_end_line, "Column": body_end_col}}
        }
        self.span_results[f"file://{os.path.abspath(file_name)}"].append(span_data)

    def _process_structure_node(self, node, file_name):
        name_start_line, name_start_col = self.get_symbol_name_location(node)
        body_start_line, body_start_col = (node.extent.start.line - 1, node.extent.start.column - 1)
        body_end_line, body_end_col = (node.extent.end.line - 1, node.extent.end.column - 1)
        
        kind_map = {
            clang.cindex.CursorKind.STRUCT_DECL: "Struct",
            clang.cindex.CursorKind.UNION_DECL: "Union",
            clang.cindex.CursorKind.ENUM_DECL: "Enum",
        }

        span_data = {
            "Name": node.spelling, "Kind": kind_map.get(node.kind, "Unknown"),
            "NameLocation": {"Start": {"Line": name_start_line, "Column": name_start_col}, "End": {"Line": name_start_line, "Column": name_start_col + len(node.spelling)}},
            "BodyLocation": {"Start": {"Line": body_start_line, "Column": body_start_col}, "End": {"Line": body_end_line, "Column": body_end_col}}
        }
        self.span_results[f"file://{os.path.abspath(file_name)}"].append(span_data)

    def get_symbol_name_location(self, node):
        """
        Returns (line, column) zero-based position of symbol's name token.
        Matches clangd indexer's behavior for functions, structs, enums, etc.
        """
        try:
            # For functions and methods, cursor.location may be on return type.
            if node.kind in (
                clang.cindex.CursorKind.FUNCTION_DECL,
                clang.cindex.CursorKind.CXX_METHOD,
                clang.cindex.CursorKind.CONSTRUCTOR,
                clang.cindex.CursorKind.DESTRUCTOR,
            ):
                for tok in node.get_tokens():
                    if tok.spelling == node.spelling:
                        loc = tok.location
                        if loc.file and loc.file.name.startswith(self.project_path):
                            return (loc.line - 1, loc.column - 1)
            # Fallback: for structs, enums, etc. this is already correct.
            loc = node.location
            return (loc.line - 1, loc.column - 1)
        except Exception:
            loc = node.location
            return (loc.line - 1, loc.column - 1)

class _TreesitterWorkerImpl:
    """Contains the logic to parse one file using tree-sitter."""
    def __init__(self):
        if not tsc or not TreeSitterParser: raise ImportError("tree-sitter not installed.")
        self.language = Language(tsc.language())
        self.parser = TreeSitterParser(self.language)

    def run(self, file_path: str) -> Tuple[List[Dict], Set]:
        try:
            with open(file_path, "rb") as f:
                source = f.read()
            tree = self.parser.parse(source)
            source_lines = source.decode("utf-8", errors="ignore").splitlines()
            
            functions = []
            stack = [tree.root_node]
            while stack:
                node = stack.pop()
                if node.type == "function_definition":
                    declarator = node.child_by_field_name("declarator")
                    ident_node = next((c for c in declarator.children if c.type == 'identifier'), None)
                    if not ident_node: continue
                    name = source_lines[ident_node.start_point[0]][ident_node.start_point[1]:ident_node.end_point[1]]
                    functions.append({
                        "Name": name, "Kind": "Function",
                        "NameLocation": {"Start": {"Line": ident_node.start_point[0], "Column": ident_node.start_point[1]}, "End": {"Line": ident_node.end_point[0], "Column": ident_node.end_point[1]}},
                        "BodyLocation": {"Start": {"Line": node.start_point[0], "Column": node.start_point[1]}, "End": {"Line": node.end_point[0], "Column": node.end_point[1]}}
                    })
                stack.extend(node.children)
            
            if not functions: return [], set()
            return [{"FileURI": f"file://{os.path.abspath(file_path)}", "Functions": functions}], set()
        except Exception as e:
            logger.error(f"Treesitter worker failed to parse {file_path}: {e}")
            return [], set()


# --- Process-local worker and initializer ---
_worker_impl_instance = None

def _worker_initializer(parser_type: str, init_args: Dict[str, Any]):
    """Initializes a worker implementation object for each process."""
    global _worker_impl_instance
    # Increase recursion limit for this worker process to handle deep ASTs
    sys.setrecursionlimit(3000)

    if parser_type == 'clang':
        _worker_impl_instance = _ClangWorkerImpl(**init_args)
    elif parser_type == 'treesitter':
        _worker_impl_instance = _TreesitterWorkerImpl(**init_args)
    else:
        raise ValueError(f"Unknown parser type: {parser_type}")

def _parallel_worker(data: Any) -> Tuple[List[Dict], Set]:
    """Generic top-level worker function that uses the process-local worker object."""
    global _worker_impl_instance
    if _worker_impl_instance is None:
        raise RuntimeError("Worker implementation has not been initialized in this process.")

    try:
        return _worker_impl_instance.run(data)
    except RecursionError:
        file_path = data if isinstance(data, str) else data.get('file', 'unknown')
        logger.error(f"Hit recursion limit while parsing {file_path}. The file's AST is likely too deep.")
        return [], set()


# --- Abstract Base Class ---

class CompilationParser:
    """An abstract base class for source code parsers."""
    def __init__(self, project_path: str):
        self.project_path = project_path
        self.source_spans: List[Dict] = []
        self.include_relations: Set[Tuple[str, str]] = set()

    def parse(self, files_to_parse: List[str], num_workers: int = 1):
        raise NotImplementedError

    def get_source_spans(self) -> List[Dict]:
        return self.source_spans

    def get_include_relations(self) -> Set[Tuple[str, str]]:
        return self.include_relations

    def _parallel_parse(self, items_to_process: List, parser_type: str, num_workers: int, desc: str, worker_init_args: Dict[str, Any] = None):
        """Generic parallel processing framework."""
        all_spans = []
        all_includes = set()
        
        initargs = (parser_type, worker_init_args or {})

        with ProcessPoolExecutor(
            max_workers=num_workers,
            initializer=_worker_initializer,
            initargs=initargs
        ) as executor:
            future_to_item = {executor.submit(_parallel_worker, item): item for item in items_to_process}
            
            for future in tqdm(as_completed(future_to_item), total=len(items_to_process), desc=desc):
                try:
                    spans, includes = future.result()
                    if spans: all_spans.extend(spans)
                    if includes: all_includes.update(includes)
                except Exception as e:
                    item = future_to_item[future]
                    file_path = item if isinstance(item, str) else item.get('file', 'unknown')
                    logger.error(f"A worker failed while processing {file_path}: {e}", exc_info=True)

        self.source_spans = all_spans
        self.include_relations = all_includes
        gc.collect()

# --- Concrete Implementations ---

class ClangParser(CompilationParser):
    """A parser that uses clang.cindex for semantic analysis."""
    def __init__(self, project_path: str, compile_commands_path: str):
        super().__init__(project_path)
        if not clang: raise ImportError("clang library is not installed.")
        
        db_dir = self._get_db_dir(compile_commands_path)
        try: 
            self.db = clang.cindex.CompilationDatabase.fromDirectory(db_dir)
        except clang.cindex.CompilationDatabaseError as e: 
            logger.critical(f"Error loading compilation database from '{db_dir}': {e}"); 
            raise

        self.clang_include_path = self._get_clang_resource_dir()

    def _get_db_dir(self, compile_commands_path: str) -> str:
        path = Path(compile_commands_path).resolve()
        if path.is_dir():
            if not (path / "compile_commands.json").exists():
                raise FileNotFoundError(f"No compile_commands.json found in directory {path}. Please put/link it there or use --compile-commands to specify the path.")
            return str(path)
        elif path.is_file():
            import tempfile, shutil
            if path.name != "compile_commands.json":
                tmpdir = tempfile.mkdtemp(prefix="clangdb_")
                shutil.copy(str(path), os.path.join(tmpdir, "compile_commands.json"))
                return tmpdir
            else:
                return str(path.parent)
        else:
            raise FileNotFoundError(f"{compile_commands_path} not found")

    def _get_clang_resource_dir(self):
        try:
            resource_dir = subprocess.check_output(['clang', '-print-resource-dir']).decode('utf-8').strip()
            return os.path.join(resource_dir, 'include')
        except (FileNotFoundError, subprocess.CalledProcessError): return None

    def parse(self, files_to_parse: List[str], num_workers: int = 1):
        self.source_spans.clear(); self.include_relations.clear()
        
        source_files = [f for f in files_to_parse if f.endswith(('.c', '.cpp', '.cc', '.cxx'))]
        if not source_files: logger.warning("ClangParser found no source files to parse."); return

        compile_entries = []
        for file_path in source_files:
            cmds = self.db.getCompileCommands(file_path)
            if not cmds: logger.warning(f"Could not get compile commands for {file_path}"); continue
            compile_entries.append({
                'file': file_path,
                'directory': cmds[0].directory,
                'arguments': list(cmds[0].arguments)[1:],
                'clang_include_path': self.clang_include_path,
                'project_path': self.project_path
            })

        if num_workers and num_workers > 1:
            logger.info(f"Parsing {len(compile_entries)} TUs with clang using {num_workers} workers...")
            init_args = {
                'project_path': self.project_path,
                'clang_include_path': self.clang_include_path
            }
            self._parallel_parse(compile_entries, 'clang', num_workers, "Parsing TUs (clang)", worker_init_args=init_args)
        else:
            logger.info(f"Parsing {len(compile_entries)} TUs with clang sequentially...")
            worker = _ClangWorkerImpl(project_path=self.project_path, clang_include_path=self.clang_include_path)
            for entry in tqdm(compile_entries, desc="Parsing TUs (clang)"):
                spans, includes = worker.run(entry)
                if spans: self.source_spans.extend(spans)
                if includes: self.include_relations.update(includes)

class TreesitterParser(CompilationParser):
    """A parser that uses Tree-sitter for syntactic analysis."""
    def __init__(self, project_path: str):
        super().__init__(project_path)
        if not tsc or not TreeSitterParser: raise ImportError("tree-sitter not installed.")

    def parse(self, files_to_parse: List[str], num_workers: int = 1):
        self.source_spans.clear(); self.include_relations.clear()

        valid_files = [f for f in files_to_parse if os.path.isfile(f)]

        if num_workers and num_workers > 1:
            logger.info(f"Parsing {len(valid_files)} files with tree-sitter using {num_workers} workers...")
            self._parallel_parse(valid_files, 'treesitter', num_workers, "Parsing spans (treesitter)", worker_init_args={})
        else:
            logger.info(f"Parsing {len(valid_files)} files with tree-sitter sequentially...")
            worker = _TreesitterWorkerImpl()
            for file_path in tqdm(valid_files, desc="Parsing spans (treesitter)"):
                spans, _ = worker.run(file_path)
                if spans: self.source_spans.extend(spans)

    def get_include_relations(self) -> Set[Tuple[str, str]]:
        logger.warning("Include relation extraction is not supported by TreesitterParser.")
        return set()
