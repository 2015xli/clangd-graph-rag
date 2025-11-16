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
from typing import List, Dict, Set, Tuple, Union, Any, Optional
from pathlib import Path
from collections import defaultdict
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor, as_completed
import gc
from dataclasses import dataclass, field

# Assuming RelativeLocation is defined in this file or imported
from clangd_index_yaml_parser import RelativeLocation

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
logger.setLevel(logging.DEBUG)

# ============================================================
# Data classes for span representation
# ============================================================
@dataclass(frozen=True)
class SourceSpan:
    name: str
    kind: str
    lang: str
    name_location: RelativeLocation
    body_location: RelativeLocation
    
    @classmethod
    def from_dict(cls, data: dict) -> 'SourceSpan':
        return cls(
            name=data['Name'],
            kind=data['Kind'],
            lang=data['Lang'],
            name_location=RelativeLocation.from_dict(data['NameLocation']),
            body_location=RelativeLocation.from_dict(data['BodyLocation'])
        )

@dataclass
class SpanTreeNode:
    """Represents a node in the hierarchical span tree."""
    span: SourceSpan
    children: List['SpanTreeNode'] = field(default_factory=list)

    def add_child(self, child: 'SpanTreeNode'):
        self.children.append(child)

# ============================================================
# Core Clang worker
# ============================================================

class _ClangWorkerImpl:
    """Parses a single compilation entry and extracts SourceSpans + include relations."""

    def __init__(self, project_path: str, clang_include_path: str):
        self.project_path = os.path.abspath(project_path)
        self.clang_include_path = clang_include_path
        self.index = clang.cindex.Index.create()
        self.entry = None
        self.span_results = None
        self.include_relations = None
        self.tu_hash = None

        # Object-level cache to avoid re-processing header nodes in identical TU contexts
        # Since we use since _ClangWorkerImpl as a singleton in a worker process, this cache is shared across all invocations
        # keys are tuples: (file_path, node_spelling, node_line, node_col, tu_hash)
        self._parsed_nodes_cache: Set[
            Union[
                Tuple[str, str], 
                Tuple[str, str, int, int, str]]
        ] = set()

    # --------------------------------------------------------
    # Main entry
    # --------------------------------------------------------
    def run(self, entry: Dict[str, Any]) -> Tuple[Dict[str, Set[SourceSpan]], Set[Tuple[str, str]]]:
        """
        Parse the entry and return:
          - list of (file_uri, [SourceSpan...])
          - set of include relations (src_abs_path, include_abs_path)
        """
        self.entry = entry
<<<<<<< HEAD
        self.span_results = defaultdict(set)   # file_uri → {SourceSpan}
        self.include_relations = set()          # set {(src_file, included_file)}
=======
        self.span_results = defaultdict(set)   # file_uri → [SourceSpan]
        self.include_relations = set()          # (src_file, included_file)
>>>>>>> 75a1ad53990d1738561375bfc5dd44ea8c75c3f1
        self.tu_hash = None

        file_path = self.entry['file']
        original_dir = os.getcwd()
        try:
            os.chdir(self.entry['directory'])
            args = self._sanitize_args(self.entry['arguments'], file_path)

            # compute TU hash (based on relevant preprocessor flags)
            self.tu_hash = self._get_tu_hash(args)
            self.lang = CompilationParser.get_language(file_path)

            # proceed to parse with args
            self._parse_translation_unit(file_path, args)
        except clang.cindex.TranslationUnitLoadError as e:
            logger.error(f"Clang worker failed to parse {file_path}: {e}")
        except Exception as e:
            logger.error(f"Clang worker had an unexpected error on {file_path}: {e}")
        finally:
            os.chdir(original_dir)


        return self.span_results, self.include_relations

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
                    self.include_relations.add((src_file, include_file))

        self._walk_ast(tu.cursor)

    # --------------------------------------------------------
    # AST walking
    # --------------------------------------------------------
    def _walk_ast(self, node):
        file_name = node.location.file.name if node.location.file else node.translation_unit.spelling
        file_name = os.path.abspath(file_name) if file_name else None
        
        if not file_name or not file_name.startswith(self.project_path):
            return
        
        if node.is_definition():
            if node.kind.name in ClangParser.NODE_KIND_FOR_BODY_SPANS:
                if not self._should_process_node(node, file_name):
                    return
                self._process_generic_node(node, file_name)

        for c in node.get_children():
            self._walk_ast(c)

    # --------------------------------------------------------
    # Span processing
    # --------------------------------------------------------
    def _process_generic_node(self, node, file_name):
        try:
            name_start_line, name_start_col = self._get_symbol_name_location(node)
        except Exception:
            name_start_line, name_start_col = node.location.line - 1, node.location.column - 1

        body_start_line, body_start_col = node.extent.start.line - 1, node.extent.start.column - 1
        body_end_line, body_end_col = node.extent.end.line - 1, node.extent.end.column - 1

        span = SourceSpan(
            name=node.spelling,
            kind=node.kind.name,
            lang=self.lang,
            name_location=RelativeLocation(name_start_line, name_start_col, name_start_line, name_start_col + len(node.spelling)),
            body_location=RelativeLocation(body_start_line, body_start_col, body_end_line, body_end_col)
        )

        file_uri = f"file://{os.path.abspath(file_name)}"
        self.span_results[file_uri].add(span)

        if False:
            if file_name.endswith("minja/minja.hpp"):
                if node.spelling == "ForTemplateToken":
                    logger.info(f"Processing {file_uri}, {span}")



    def _should_process_node(self, node, file_name) -> bool:
        """
        Avoid redundant node processing across identical TU contexts using TU hash and exact location.
        """
        node_sig = (file_name, node.spelling, node.location.line, node.location.column, node.extent.start.line, node.extent.start.column, node.extent.end.line, node.extent.end.column, self.tu_hash)
        cache = self._parsed_nodes_cache
        if node_sig in cache:
            return False
        cache.add(node_sig)
        return True

    # --------------------------------------------------------
    # Helpers
    # --------------------------------------------------------
    def _sanitize_args(self, args: List[str], file_path: str) -> List[str]:
        """Remove irrelevant flags from compilation arguments."""
        sanitized = []
        skip_next = False
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a in {'-c', '-o', '-MMD', '-MF', '-MT', '-fcolor-diagnostics', '-fdiagnostics-color'}:
                skip_next = True
                continue
            if a == file_path or os.path.basename(a) == os.path.basename(file_path):
                continue
            sanitized.append(a)
        return sanitized

    def _get_tu_hash(self, args: List[str]) -> str:
        """
        Compute a deterministic hash representing the TU preprocessing context.
        By default we include -D and -U flags (macro defines/undefs). You can extend
        this to include include paths (-I) or other flags if needed.
        """
        relevant = [a for a in args if a.startswith("-D") or a.startswith("-U")]
        # Sort for determinism across argument order variations
        relevant_sorted = sorted(relevant)
        # short md5 hex
        h = hashlib.md5(" ".join(relevant_sorted).encode("utf-8")).hexdigest()
        return h[:16]

    def _get_symbol_name_location(self, node):
        """Return zero-based (line, column) for symbol's name."""
        for tok in node.get_tokens():
            if tok.spelling == node.spelling:
                loc = tok.location
                try:
                    file, line, col, _ = loc.get_expansion_location()
                except AttributeError:
                    # older/newer libclang variations
                    continue
                if file and file.name.startswith(self.project_path):
                    return (line - 1, col - 1)
        loc = node.location
        try:
            file, line, col, _ = loc.get_expansion_location()
            return (line - 1, col - 1)
        except AttributeError:
            return (node.location.line - 1, node.location.column - 1)

class _TreesitterWorkerImpl:
    """Contains the logic to parse one file using tree-sitter."""
    def __init__(self):
        if not tsc or not TreeSitterParser: raise ImportError("tree-sitter not installed.")
        self.language = Language(tsc.language())
        self.parser = TreeSitterParser(self.language)

    def run(self, file_path: str) -> Tuple[Optional[Dict[str, Set[SourceSpan]]], Set]:
        # Note: Tree-sitter parsing is not hierarchical and does not build a tree.
        # This implementation is kept for basic compatibility but does not support nesting.
        try:
            with open(file_path, "rb") as f:
                source = f.read()
            tree = self.parser.parse(source)
            source_lines = source.decode("utf-8", errors="ignore").splitlines()
            
            spans = set()
            stack = [tree.root_node]
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
                    spans.add(SourceSpan(name=name, kind="Function", lang="C", name_span=name_span, body_span=body_span))
                stack.extend(node.children)
            
            if not spans: return None, set()
            
            result = (f"file://{os.path.abspath(file_path)}", spans)
            return result, set()
        except Exception as e:
            logger.error(f"Treesitter worker failed to parse {file_path}: {e}")
            return None, set()


# --- Process-local worker and initializer ---
_worker_impl_instance = None

# Total number of processed TUs so far
_count_processed_tus = 0

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

def _parallel_worker(data: Any) -> Tuple[Optional[Tuple[str, Set[SourceSpan]]], Set]:
    """Generic top-level worker function that uses the process-local worker object."""
    global _worker_impl_instance
    global _count_processed_tus

    if _worker_impl_instance is None:
        raise RuntimeError("Worker implementation has not been initialized in this process.")

    try:
        _count_processed_tus += 1
        if _count_processed_tus % 1000 == 0: gc.collect()

        return _worker_impl_instance.run(data)
    except RecursionError:
        file_path = data if isinstance(data, str) else data.get('file', 'unknown')
        logger.error(f"Hit recursion limit while parsing {file_path}. The file's AST is likely too deep.")
        return None, set()


# --- Abstract Base Class ---

class CompilationParser:
    """An abstract base class for source code parsers."""

    C_SOURCE_EXTENSIONS = ('.c',)
    CPP_SOURCE_EXTENSIONS = ('.cpp', '.cc', '.cxx')
    CPP20_MODULE_EXTENSIONS = ('.cppm', '.ccm', '.cxxm', '.c++m')
    C_HEADER_EXTENSIONS = ('.h',)
    CPP_HEADER_EXTENSIONS = ('.hpp', '.hh', '.hxx', '.h++')

    ALL_SOURCE_EXTENSIONS = C_SOURCE_EXTENSIONS + CPP_SOURCE_EXTENSIONS + CPP20_MODULE_EXTENSIONS
    ALL_HEADER_EXTENSIONS = C_HEADER_EXTENSIONS + CPP_HEADER_EXTENSIONS
    ALL_C_CPP_EXTENSIONS = ALL_SOURCE_EXTENSIONS + ALL_HEADER_EXTENSIONS
    ALL_CPP_SOURCE_EXTENSIONS = CPP_SOURCE_EXTENSIONS + CPP20_MODULE_EXTENSIONS

    def __init__(self, project_path: str):
        self.project_path = project_path
        self.source_spans: Dict[str, Set[SourceSpan]] = defaultdict(set) # Changed to Dict[FileURI, Set[SpanNode]]
        self.include_relations: Set[Tuple[str, str]] = set()

    def parse(self, files_to_parse: List[str], num_workers: int = 1):
        raise NotImplementedError

    def get_source_spans(self) -> Dict[str, Set[SourceSpan]]:
        return self.source_spans

    def get_include_relations(self) -> Set[Tuple[str, str]]:
        return self.include_relations

    def parser_kind_to_index_kind(self, kind: str, lang: str) -> str:
        raise NotImplementedError

    @classmethod
    def get_language(cls, file_name: str) -> str:
        if file_name.endswith(cls.ALL_CPP_SOURCE_EXTENSIONS):
            lang = "Cpp"
        elif file_name.endswith(".c"):
            lang = "C"
        else:
            logger.error(f"Unknown language for file: {file_name}")
            lang = "Unknown"
        return lang

    def _parallel_parse(self, items_to_process: List, parser_type: str, num_workers: int, desc: str, worker_init_args: Dict[str, Any] = None):
        """Generic parallel processing framework."""
        all_spans = defaultdict(set)
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
                    span_result, includes = future.result()
                    if includes: all_includes.update(includes)
                    if not span_result: continue
                    for file_uri, spans in span_result.items():
                        all_spans[file_uri].update(spans)

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

    NODE_KIND_FUNCTIONS = {
        clang.cindex.CursorKind.FUNCTION_DECL.name,
        clang.cindex.CursorKind.FUNCTION_TEMPLATE.name,
    }

    NODE_KIND_METHODS = {
        clang.cindex.CursorKind.CXX_METHOD.name,
        clang.cindex.CursorKind.CONSTRUCTOR.name,
        clang.cindex.CursorKind.DESTRUCTOR.name,
        clang.cindex.CursorKind.CONVERSION_FUNCTION.name,
    }

    NODE_KIND_UNION = {
        clang.cindex.CursorKind.UNION_DECL.name,
    }

    NODE_KIND_ENUM = {
        clang.cindex.CursorKind.ENUM_DECL.name,
    }

    NODE_KIND_STRUCT = {
        clang.cindex.CursorKind.STRUCT_DECL.name,
    }

    NODE_KIND_CLASSES = {
        clang.cindex.CursorKind.CLASS_DECL.name,
        clang.cindex.CursorKind.CLASS_TEMPLATE.name,
        clang.cindex.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION.name,
    }

    NODE_KIND_NAMESPACE = { clang.cindex.CursorKind.NAMESPACE.name }

    NODE_KIND_FOR_BODY_SPANS = NODE_KIND_FUNCTIONS | NODE_KIND_METHODS | NODE_KIND_UNION | NODE_KIND_ENUM | NODE_KIND_STRUCT | NODE_KIND_CLASSES | NODE_KIND_NAMESPACE

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
        path = Path(compile_commands_path).expanduser().resolve()
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
        
        source_files = [f for f in files_to_parse if f.lower().endswith(CompilationParser.ALL_SOURCE_EXTENSIONS)]
        if not source_files: logger.warning("ClangParser found no source files to parse."); return

        compile_entries = []
        for file_path in source_files:
            cmds = self.db.getCompileCommands(file_path)
            if not cmds: logger.warning(f"Could not get compile commands for {file_path}"); continue
            compile_entries.append({
                'file': file_path,
                'directory': cmds[0].directory,
                'arguments': list(cmds[0].arguments)[1:],
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
                span_result, includes = worker.run(entry)
                if includes: self.include_relations.update(includes)
                if not span_result: continue
                for file_uri, spans in span_result.items():
                    self.source_spans[file_uri].update(spans)

        if False:        
            debug_file = "file:///home/xli/Public/llama.cpp/vendor/minja/minja.hpp"
            span_result = self.source_spans.get(debug_file)
            if span_result:
                logger.info(f"================ Processing {debug_file}=============")
                for span in span_result:
                    logger.info(f"Processing {span}")
        
    def parser_kind_to_index_kind(self, kind: str, lang: str) -> str:
        """Converts a Clang parser kind to a Clangd index kind."""

        if kind in ClangParser.NODE_KIND_FUNCTIONS:
            return "Function"
        elif kind in ClangParser.NODE_KIND_METHODS:
            return "Method"
        elif kind in ClangParser.NODE_KIND_STRUCT:
            return "Struct"
        elif kind in ClangParser.NODE_KIND_UNION:
            return "Union"
        elif kind in ClangParser.NODE_KIND_ENUM:
            return "Enum"
        elif kind in ClangParser.NODE_KIND_CLASSES:
            return "Class"
        elif kind in ClangParser.NODE_KIND_NAMESPACE:
            return "Namespace"
        else:
            logger.error(f"Unknown Clang parser kind: {kind}")
            return "Unknown"

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
                span_result, _ = worker.run(file_path)
                if span_result: self.source_spans.update(span_result)

    def parser_kind_to_index_kind(self, kind: str, lang: str) -> str:
        logger.warning("Node kind conversion is not supported by TreesitterParser.")
        return kind

    def get_include_relations(self) -> Set[Tuple[str, str]]:
        logger.warning("Include relation extraction is not supported by TreesitterParser.")
        return set()
