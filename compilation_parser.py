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
from typing import List, Dict, Set, Tuple, Callable, Any, Optional
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


# ============================================================
# Data classes for span representation
# ============================================================
@dataclass
class SourceSpan:
    name: str
    kind: str
    name_location: RelativeLocation
    body_location: RelativeLocation
    
    @classmethod
    def from_dict(cls, data: dict) -> 'SourceSpan':
        return cls(
            name=data['Name'],
            kind=data['Kind'],
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

    # class-level cache to avoid re-processing header nodes in identical TU contexts
    # keys are tuples: (file_path, node_spelling, node_line, node_col, tu_hash)
    _parsed_header_nodes_cache: Set[Tuple[str, str, int, int, str]] = set()

    _NODE_KIND_FUNCTIONS = {
        clang.cindex.CursorKind.FUNCTION_DECL,
        clang.cindex.CursorKind.CXX_METHOD,
        clang.cindex.CursorKind.CONSTRUCTOR,
        clang.cindex.CursorKind.DESTRUCTOR,
        clang.cindex.CursorKind.CONVERSION_FUNCTION,
        clang.cindex.CursorKind.FUNCTION_TEMPLATE,
    }

    _NODE_KIND_DATA = {
        clang.cindex.CursorKind.STRUCT_DECL,
        clang.cindex.CursorKind.UNION_DECL,
        clang.cindex.CursorKind.ENUM_DECL,
        clang.cindex.CursorKind.CLASS_DECL,
        clang.cindex.CursorKind.CLASS_TEMPLATE,
        clang.cindex.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
    }

    _NODE_KIND_NAMESPACES = { clang.cindex.CursorKind.NAMESPACE }

    _NODE_KIND_FOR_BODY_SPANS = _NODE_KIND_FUNCTIONS | _NODE_KIND_DATA | _NODE_KIND_NAMESPACES

    def __init__(self, project_path: str, clang_include_path: str):
        self.project_path = os.path.abspath(project_path)
        self.clang_include_path = clang_include_path
        self.index = clang.cindex.Index.create()
        self.entry = None
        self.span_results = None
        self.include_relations = None
        self.tu_hash = None

    # --------------------------------------------------------
    # Main entry
    # --------------------------------------------------------
    def run(self, entry: Dict[str, Any]) -> Tuple[Dict[str, List[SourceSpan]], Set[Tuple[str, str]]]:
        """
        Parse the entry and return:
          - list of (file_uri, [SourceSpan...])
          - set of include relations (src_abs_path, include_abs_path)
        """
        self.entry = entry
        self.span_results = defaultdict(list)   # file_uri → [SourceSpan]
        self.include_relations = set()          # (src_file, included_file)
        self.tu_hash = None

        file_path = self.entry['file']
        original_dir = os.getcwd()
        try:
            os.chdir(self.entry['directory'])
            args = self._sanitize_args(self.entry['arguments'], file_path)

            # compute TU hash (based on relevant preprocessor flags)
            self.tu_hash = self._get_tu_hash(args)

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
                self.include_relations.add(
                    (os.path.abspath(inc.source.name), os.path.abspath(inc.include.name))
                )

        self._walk_ast(tu.cursor)

    # --------------------------------------------------------
    # AST walking
    # --------------------------------------------------------
    def _walk_ast(self, node):
        file_name = node.location.file.name if node.location.file else node.translation_unit.spelling
        if not file_name or not file_name.startswith(self.project_path):
            return

        if node.is_definition():
            if node.kind in self._NODE_KIND_FOR_BODY_SPANS:
                self._process_generic_node(node, file_name, node.kind.name)

        for c in node.get_children():
            self._walk_ast(c)

    # --------------------------------------------------------
    # Span processing
    # --------------------------------------------------------
    def _process_generic_node(self, node, file_name, kind_str):
        if not self._should_process_node(node, file_name):
            return

        try:
            name_start_line, name_start_col = self.get_symbol_name_location(node)
        except Exception:
            name_start_line, name_start_col = node.location.line - 1, node.location.column - 1

        body_start_line, body_start_col = node.extent.start.line - 1, node.extent.start.column - 1
        body_end_line, body_end_col = node.extent.end.line - 1, node.extent.end.column - 1

        span = SourceSpan(
            name=node.spelling,
            kind=kind_str,
            name_location=RelativeLocation(name_start_line, name_start_col, name_start_line, name_start_col + len(node.spelling)),
            body_location=RelativeLocation(body_start_line, body_start_col, body_end_line, body_end_col)
        )

        file_uri = f"file://{os.path.abspath(file_name)}"
        self.span_results[file_uri].append(span)

    def _should_process_node(self, node, file_name) -> bool:
        """Avoid redundant header node processing across identical TU contexts."""
        is_header = file_name.lower().endswith(('.h', '.hpp', '.hh', '.hxx'))
        node_sig = (file_name, node.spelling, node.location.line, node.location.column, self.tu_hash)

        if is_header:
            if node_sig in _ClangWorkerImpl._parsed_header_nodes_cache:
                # already processed in this TU-hash context
                return False
            # record in cache so subsequent visits (in same TU or other workers with same tu_hash) skip
            _ClangWorkerImpl._parsed_header_nodes_cache.add(node_sig)

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

    def get_symbol_name_location(self, node):
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

# ============================================================
# Span forest construction utilities
# ============================================================

def build_span_forest(spans_per_file: List[Tuple[str, List[SourceSpan]]]) -> Dict[str, List[SpanTreeNode]]:
    """
    Build a hierarchical span forest (list of roots) for each file.

    Args:
        spans_per_file: Output from Clang worker [(file_uri, [SourceSpan, ...])]

    Returns:
        Dict[file_uri, List[SpanTreeNode]]
    """
    forests: Dict[str, List[SpanTreeNode]] = {}

    for file_uri, spans in spans_per_file:
        if not spans:
            forests[file_uri] = []
            continue

        # Sort spans by start position, and then by end descending (outer before inner)
        spans_sorted = sorted(
            spans,
            key=lambda s: (s.body_location.start_line, s.body_location.start_column,
                           -s.body_location.end_line, -s.body_location.end_column)
        )

        root_nodes: List[SpanTreeNode] = []
        stack: List[SpanTreeNode] = []

        for span in spans_sorted:
            node = SpanTreeNode(span)
            # pop stack until current node fits as child
            while stack and not span_is_within(span, stack[-1].span):
                stack.pop()

            if stack:
                stack[-1].add_child(node)
            else:
                root_nodes.append(node)

            stack.append(node)

        forests[file_uri] = root_nodes

    return forests


def span_is_within(inner: SourceSpan, outer: SourceSpan) -> bool:
    """Check if 'inner' span is fully inside 'outer' span."""
    s1, e1 = inner.body_location, outer.body_location
    # inner.start >= outer.start AND inner.end <= outer.end
    if (s1.start_line > e1.start_line or
        (s1.start_line == e1.start_line and s1.start_column >= e1.start_column)):
        if (s1.end_line < e1.end_line or
            (s1.end_line == e1.end_line and s1.end_column <= e1.end_column)):
            return True
    return False

class _TreesitterWorkerImpl:
    """Contains the logic to parse one file using tree-sitter."""
    def __init__(self):
        if not tsc or not TreeSitterParser: raise ImportError("tree-sitter not installed.")
        self.language = Language(tsc.language())
        self.parser = TreeSitterParser(self.language)

    def run(self, file_path: str) -> Tuple[Optional[Dict[str, List[SourceSpan]]], Set]:
        # Note: Tree-sitter parsing is not hierarchical and does not build a tree.
        # This implementation is kept for basic compatibility but does not support nesting.
        try:
            with open(file_path, "rb") as f:
                source = f.read()
            tree = self.parser.parse(source)
            source_lines = source.decode("utf-8", errors="ignore").splitlines()
            
            spans = []
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
                    spans.append(SpanNode(kind="Function", name=name, name_span=name_span, body_span=body_span))
                stack.extend(node.children)
            
            if not spans: return None, set()
            
            result = (f"file://{os.path.abspath(file_path)}", spans)
            return result, set()
        except Exception as e:
            logger.error(f"Treesitter worker failed to parse {file_path}: {e}")
            return None, set()


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

def _parallel_worker(data: Any) -> Tuple[Optional[Tuple[str, List[SourceSpan]]], Set]:
    """Generic top-level worker function that uses the process-local worker object."""
    global _worker_impl_instance
    if _worker_impl_instance is None:
        raise RuntimeError("Worker implementation has not been initialized in this process.")

    try:
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

    def __init__(self, project_path: str):
        self.project_path = project_path
        self.source_spans: Dict[str, List[SourceSpan]] = {} # Changed to Dict[FileURI, List[SpanNode]]
        self.include_relations: Set[Tuple[str, str]] = set()

    def parse(self, files_to_parse: List[str], num_workers: int = 1):
        raise NotImplementedError

    def get_source_spans(self) -> Dict[str, List[SourceSpan]]:
        return self.source_spans

    def get_include_relations(self) -> Set[Tuple[str, str]]:
        return self.include_relations

    def _parallel_parse(self, items_to_process: List, parser_type: str, num_workers: int, desc: str, worker_init_args: Dict[str, Any] = None):
        """Generic parallel processing framework."""
        all_spans = {}
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
                    if span_result: all_spans.update(span_result)
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
                if span_result:
                    file_uri, span_forest = span_result
                    self.source_spans[file_uri] = span_forest
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
                span_result, _ = worker.run(file_path)
                if span_result:
                    file_uri, span_forest = span_result
                    self.source_spans[file_uri] = span_forest

    def get_include_relations(self) -> Set[Tuple[str, str]]:
        logger.warning("Include relation extraction is not supported by TreesitterParser.")
        return set()
