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

# --- New SpanTree Data Structure ---
@dataclass
class SpanNode:
    """A node in the SpanTree, representing a nested code structure."""
    kind: str
    name: str
    name_span: RelativeLocation
    body_span: RelativeLocation
    children: List['SpanNode'] = field(default_factory=list)

# --- Worker Implementations ---
# These classes encapsulate the logic for a single unit of work.

class _ClangWorkerImpl:
    """Contains the logic to parse one file using clang and build a SpanTree."""

    _NODE_KIND_FOR_BODY_SPANS = {
        clang.cindex.CursorKind.FUNCTION_DECL,
        clang.cindex.CursorKind.CXX_METHOD,
        clang.cindex.CursorKind.CONSTRUCTOR,
        clang.cindex.CursorKind.DESTRUCTOR,
        clang.cindex.CursorKind.CONVERSION_FUNCTION,
        clang.cindex.CursorKind.FUNCTION_TEMPLATE,
        clang.cindex.CursorKind.STRUCT_DECL,
        clang.cindex.CursorKind.UNION_DECL,
        clang.cindex.CursorKind.ENUM_DECL,
        clang.cindex.CursorKind.CLASS_DECL,
        clang.cindex.CursorKind.CLASS_TEMPLATE,
        clang.cindex.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
    }

    def __init__(self, project_path: str, clang_include_path: str):
        self.project_path = project_path
        self.clang_include_path = clang_include_path
        self.index = clang.cindex.Index.create()
        self.entry = None
        self.include_relations = None
        self.processed_headers = None

    def run(self, entry: Dict[str, Any]) -> Tuple[Optional[Tuple[str, List[SpanNode]]], Set[Tuple[str, str]]]:
        self.entry = entry
        self.include_relations = set()
        self.processed_headers = set()

        file_path = self.entry['file']
        original_dir = os.getcwd()
        span_tree_forest = []
        try:
            os.chdir(self.entry['directory'])
            tu = self._parse_translation_unit(file_path)
            if tu:
                # The top-level cursor's children are the top-level declarations in the file.
                span_tree_forest = self._walk_ast(tu.cursor)
        except clang.cindex.TranslationUnitLoadError as e:
            logger.error(f"Clang worker failed to parse {file_path}: {e}")
        except Exception as e:
            logger.error(f"Clang worker had an unexpected error on {file_path}: {e}")
        finally:
            os.chdir(original_dir)

        if not span_tree_forest:
            return None, self.include_relations

        result = (f"file://{os.path.abspath(file_path)}", span_tree_forest)
        return result, self.include_relations

    def _parse_translation_unit(self, file_path: str) -> Optional[clang.cindex.TranslationUnit]:
        args = self.entry['arguments']
        sanitized_args = []
        skip_next = False
        for a in args:
            if skip_next: skip_next = False; continue
            if a in {'-c', '-o', '-MMD', '-MF', '-MT', '-fcolor-diagnostics', '-fdiagnostics-color'}: 
                skip_next = True; continue
            if a == file_path or os.path.basename(a) == os.path.basename(file_path): continue
            sanitized_args.append(a)

        if self.clang_include_path: sanitized_args.append(f"-I{self.clang_include_path}")

        tu = self.index.parse(file_path, args=sanitized_args, options=clang.cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
        
        for inc in tu.get_includes():
            if inc.source and inc.include:
                self.include_relations.add((os.path.abspath(inc.source.name), os.path.abspath(inc.include.name)))
        
        return tu

    def _walk_ast(self, node: clang.cindex.Cursor) -> List[SpanNode]:
        """
        Recursively walks the AST. For each node, it first gathers span nodes from its
        children, then checks if it is a structure itself to be turned into a SpanNode.
        Returns a list of SpanNodes found at the current level.
        """
        # This list will hold all SpanNodes found at the current level of the tree
        # (either direct children that are structures, or grandchildren passed up).
        found_spans = []

        # Recurse on children first
        for child in node.get_children():
            # Only process nodes within the project directory
            if child.location.file and child.location.file.name.startswith(self.project_path):
                found_spans.extend(self._walk_ast(child))

        # Now, process the current node
        if node.is_definition() and node.kind in self._NODE_KIND_FOR_BODY_SPANS:
            if self._should_process_node(node):
                name_span = self._get_symbol_name_span(node)
                body_span = RelativeLocation(
                    start_line=node.extent.start.line - 1,
                    start_column=node.extent.start.column - 1,
                    end_line=node.extent.end.line - 1,
                    end_column=node.extent.end.column - 1
                )
                
                # Create the node for the current structure
                current_node_span = SpanNode(
                    kind=node.kind.name,
                    name=node.spelling or '(anonymous)',
                    name_span=name_span,
                    body_span=body_span,
                    children=found_spans # Assign the collected children
                )
                # Since this node is a structure, it becomes the parent.
                # We return a list containing just this node.
                return [current_node_span]

        # If the current node is not a structure, we just pass the list of
        # spans found in its children up the call stack.
        return found_spans

    def _should_process_node(self, node) -> bool:
        file_name = node.location.file.name if node.location.file else "unknown"
        is_header = file_name.lower().endswith(CompilationParser.ALL_HEADER_EXTENSIONS)
        node_sig = (file_name, node.spelling, node.location.line, node.location.column)

        if is_header and node_sig in self.processed_headers:
            return False
        if is_header:
            self.processed_headers.add(node_sig)
        return True

    def _get_symbol_name_span(self, node) -> Optional[RelativeLocation]:
        """
        Returns a RelativeLocation for the symbol's name token (0-indexed, inclusive).
        Uses get_expansion_location() for macro-safe name resolution.
        Falls back to node.location if token parsing fails.

        Rules:
          - Lines/columns are 0-based (consistent with project convention).
          - The returned span is inclusive of both start and end columns.
          - Filters out spans that cross files (due to weird macros).
        """
        try:
            # Skip nodes without a spelling (e.g., anonymous structs/lambdas)
            if not getattr(node, "spelling", None):
                return None

            # Iterate over tokens within the node's extent
            for tok in node.get_tokens():
                # Only consider identifier tokens whose spelling matches the node's
                if tok.kind == clang.cindex.TokenKind.IDENTIFIER and tok.spelling == node.spelling:
                    start_loc = tok.extent.start
                    end_loc = tok.extent.end

                    # Resolve macro expansions — map to where the token was actually expanded
                    start_file, start_line, start_col, _ = start_loc.get_expansion_location()
                    end_file, end_line, end_col, _ = end_loc.get_expansion_location()

                    # Safety: ensure both ends of the token belong to the same file
                    if start_file and end_file and start_file.name == end_file.name:
                        return RelativeLocation(
                            start_line=start_line - 1,
                            start_column=start_col - 1,
                            end_line=end_line - 1,
                            end_column=end_col - 1
                        )

        except Exception as e:
            logger.warning(
                f"Could not parse tokens for symbol '{getattr(node, 'spelling', '?')}' "
                f"to get name span; falling back. Error: {e}"
            )

        # --- Fallback ---
        # Use node.location (less precise but reliable).
        # This path is taken if token parsing fails or doesn't find a match.
        loc = node.location
        
        # Corrected Fallback: Do not call get_expansion_location() here. If token
        # parsing failed, we can't reliably resolve macros anyway. Use direct properties.
        if not loc or not loc.file:
            return None

        line = loc.line
        col = loc.column
        name_len = len(getattr(node, "spelling", "") or "")

        return RelativeLocation(
            start_line=line - 1,
            start_column=col - 1,
            end_line=line - 1,
            end_column=(col - 1) + max(name_len, 1) - 1
        )

class _TreesitterWorkerImpl:
    """Contains the logic to parse one file using tree-sitter."""
    def __init__(self):
        if not tsc or not TreeSitterParser: raise ImportError("tree-sitter not installed.")
        self.language = Language(tsc.language())
        self.parser = TreeSitterParser(self.language)

    def run(self, file_path: str) -> Tuple[Optional[Tuple[str, List[SpanNode]]], Set]:
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

def _parallel_worker(data: Any) -> Tuple[Optional[Tuple[str, List[SpanNode]]], Set]:
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
        self.source_spans: Dict[str, List[SpanNode]] = {} # Changed to Dict[FileURI, List[SpanNode]]
        self.include_relations: Set[Tuple[str, str]] = set()

    def parse(self, files_to_parse: List[str], num_workers: int = 1):
        raise NotImplementedError

    def get_source_spans(self) -> Dict[str, List[SpanNode]]:
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
                    if span_result:
                        file_uri, span_forest = span_result
                        all_spans[file_uri] = span_forest
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
