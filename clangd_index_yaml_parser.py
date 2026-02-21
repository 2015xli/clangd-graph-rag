#!/usr/bin/env python3
"""
This module provides a parser for clangd's YAML index format.

It defines the common data classes for symbols, references, and locations,
and provides a SymbolParser class to read a clangd index file into an
in-memory collection of symbol objects.
"""

import yaml, pickle
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
import logging, os
import gc
import math
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from multiprocessing import get_context
from tqdm import tqdm

from memory_debugger import Debugger
from neo4j_manager import align_string # Import Debugger

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# --- YAML tag handling ---
def unknown_tag(loader, tag_suffix, node):
    return loader.construct_mapping(node)

yaml.SafeLoader.add_multi_constructor("!", unknown_tag)

# --- Common Data Classes ---

@dataclass(frozen=True, slots=True)
class Location:
    file_uri: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Location':
        return cls(
            file_uri=data['FileURI'],
            start_line=data['Start']['Line'],
            start_column=data['Start']['Column'],
            end_line=data['End']['Line'],
            end_column=data['End']['Column']
        )

    @classmethod
    def from_relative_location(cls, rel_loc: 'RelativeLocation', file_uri: str) -> 'Location':
        return cls(
            file_uri=file_uri,
            start_line=rel_loc.start_line,
            start_column=rel_loc.start_column,
            end_line=rel_loc.end_line,
            end_column=rel_loc.end_column
        )

@dataclass(frozen=True, slots=True)
class RelativeLocation:
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    @classmethod
    def from_dict(cls, data: dict) -> 'RelativeLocation':
        return cls(
            start_line=data['Start']['Line'],
            start_column=data['Start']['Column'],
            end_line=data['End']['Line'],
            end_column=data['End']['Column']
        )

@dataclass(frozen=True, slots=True)
class Reference:
    kind: int
    location: Location
    container_id: Optional[str] = None
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Reference':
        return cls(
            kind=data['Kind'],
            location=Location.from_dict(data['Location']),
            container_id=data.get('Container', {}).get('ID')
        )

@dataclass
class Symbol:
    id: str
    name: str
    kind: str
    declaration: Optional[Location]
    definition: Optional[Location]
    references: List[Reference]
    scope: str = ""
    language: str = ""
    signature: str = ""
    return_type: str = ""
    type: str = ""
    body_location: Optional[RelativeLocation] = None
    parent_id: Optional[str] = None
    is_macro_function_like: bool = False
    macro_definition: Optional[str] = None
    original_name: Optional[str] = None
    expanded_from_id: Optional[str] = None
    # New fields for TypeAlias
    aliased_canonical_spelling: Optional[str] = None
    aliased_type_id: Optional[str] = None
    aliased_type_kind: Optional[str] = None
    
    def is_function(self) -> bool:
        return self.kind in ('Function', 'InstanceMethod', 'StaticMethod', 'Constructor', 'Destructor', 'ConversionFunction')

@dataclass
class CallRelation:
    caller_id: str
    caller_name: str
    callee_id: str
    callee_name: str
    call_location: Location

# --- Symbol Parser ---

class SymbolParser:
    """A high-performance parser for clangd index YAML files with built-in caching."""
    def __init__(self, index_file_path: str, log_batch_size: int = 1000, debugger: Optional[Debugger] = None):
        self.index_file_path = index_file_path
        self.log_batch_size = log_batch_size
        self.debugger = debugger
        
        # These fields will be populated by parsing or loading from cache
        self.symbols: Dict[str, Symbol] = {}
        self.functions: Dict[str, Symbol] = {}
        self.has_container_field: bool = False
        self.has_call_kind: bool = False
        self.inheritance_relations: List[Tuple[str, str]] = []
        self.override_relations: List[Tuple[str, str]] = []
        
        # These fields are transient and only used during YAML parsing
        self.unlinked_refs: List[Dict] = []
        self.unlinked_relations: List[Dict] = []

    def parse(self, num_workers: int = 1):
        """
        Main entry point for parsing. Handles cache loading/saving.
        """
        cache_path = os.path.splitext(self.index_file_path)[0] + ".pkl"

        # Determine if we should load from cache
        if self.index_file_path.endswith('.pkl'):
            self._load_cache_file(self.index_file_path)
            return # Loading complete
        elif os.path.exists(cache_path) and os.path.getmtime(cache_path) > os.path.getmtime(self.index_file_path):
            logger.info(f"Found valid cache file: {cache_path}")
            logger.info("To force re-parsing the YAML, delete the .pkl file or touch the YAML file and run again.")
            self._load_cache_file(cache_path)
            return # Loading complete

        # --- Cache not found or is outdated, proceed with YAML parsing ---
        if num_workers > 1:
            logger.info(f"Using parallel parser with {num_workers} workers.")
            self._parallel_parse(num_workers)
        else:
            logger.info("Using standard parser in single-threaded mode.")
            self._parse_yaml_file()
        
        self.build_cross_references()

        # --- Save to cache for future runs ---
        self._dump_cache_file(cache_path)

    def _load_cache_file(self, cache_path: str):
        logger.info(f"Loading parsed symbols from cache: {cache_path}")
        try:
            with open(cache_path, 'rb') as f:
                cache_data = pickle.load(f)
            self.symbols = cache_data['symbols']
            self.functions = cache_data['functions']
            self.has_container_field = cache_data['has_container_field']
            self.has_call_kind = cache_data['has_call_kind']
            self.inheritance_relations = cache_data.get('inheritance_relations', []) # Use .get for backward compatibility
            self.override_relations = cache_data.get('override_relations', [])       # Use .get for backward compatibility
            logger.info("Successfully loaded symbols from cache.")
        except (pickle.UnpicklingError, EOFError, KeyError) as e:
            logger.error(f"Cache file {cache_path} is corrupted or invalid: {e}. Please delete it and re-run.", exc_info=True)
            raise

    def _dump_cache_file(self, cache_path: str):
        logger.info(f"Saving parsed symbols to cache: {cache_path}")
        try:
            cache_data = {
                'symbols': self.symbols,
                'functions': self.functions,
                'has_container_field': self.has_container_field,
                'has_call_kind': self.has_call_kind,
                'inheritance_relations': self.inheritance_relations,
                'override_relations': self.override_relations
            }
            with open(cache_path, 'wb') as f:
                pickle.dump(cache_data, f)
            logger.info("Successfully saved symbols to cache.")
        except Exception as e:
            logger.error(f"Failed to save cache to {cache_path}: {e}", exc_info=True)

    

    def _parse_yaml_file(self):
        """Phase 1: Reads and sanitizes a YAML file, then loads the data."""
        logger.info(f"Reading and sanitizing index file: {self.index_file_path}")
        # Read file and sanitize content into an in-memory string
        with open(self.index_file_path, 'r', errors='ignore') as f:
            yaml_content = f.read().replace('\t', '  ')
        
        self._load_from_string(yaml_content)

    def _load_from_string(self, yaml_content: str):
        """Loads symbols and unlinked refs from a YAML content string."""
        documents = list(yaml.safe_load_all(yaml_content))
        for doc in documents:
            if not doc:
                continue
            if 'ID' in doc and 'SymInfo' in doc:
                symbol = self._parse_symbol_doc(doc)
                self.symbols[symbol.id] = symbol
            elif 'ID' in doc and 'References' in doc:
                self.unlinked_refs.append(doc)
            elif 'Subject' in doc and 'Predicate' in doc and 'Object' in doc:
                self.unlinked_relations.append(doc)

    def build_cross_references(self):
        """Phase 2: Links loaded references and builds the functions table."""
        logger.info("Building cross-references and populating functions table...")
        
        for ref_doc in self.unlinked_refs:
            symbol_id = ref_doc['ID']
            if symbol_id not in self.symbols:
                continue
            
            for ref_data in ref_doc['References']:
                if 'Location' in ref_data and 'Kind' in ref_data:
                    reference = Reference.from_dict(ref_data)
                    self.symbols[symbol_id].references.append(reference)

                    if not self.has_container_field and reference.container_id:
                        self.has_container_field = True
                        self.has_call_kind = True

                    elif not self.has_call_kind and reference.kind >= 16:
                        self.has_call_kind = True

        for symbol in self.symbols.values():
            if symbol.is_function():
                self.functions[symbol.id] = symbol

        for rel_doc in self.unlinked_relations:
            # Predicate: 0 is BaseOf, 1 is OverriddenBy
            subject_id = rel_doc['Subject']['ID']
            object_id = rel_doc['Object']['ID']
            if rel_doc['Predicate'] == 0:
                self.inheritance_relations.append((subject_id, object_id))
            elif rel_doc['Predicate'] == 1:
                self.override_relations.append((subject_id, object_id))

        del self.unlinked_refs
        del self.unlinked_relations
        gc.collect()
        logger.info(f"Cross-referencing complete. Found {len(self.symbols)} symbols and {len(self.functions)} functions.")

    def _parse_symbol_doc(self, doc: dict) -> Symbol:
        """Parses a YAML document into a Symbol object."""
        sym_info = doc.get('SymInfo', {})
        return Symbol(
            id=doc['ID'],
            name=doc['Name'],
            kind=sym_info.get('Kind', ''),
            declaration=Location.from_dict(doc['CanonicalDeclaration']) if 'CanonicalDeclaration' in doc else None,
            definition=Location.from_dict(doc['Definition']) if 'Definition' in doc else None,
            references=[],
            scope=doc.get('Scope', ''),
            language=sym_info.get('Lang', ''),
            signature=doc.get('Signature', ''),
            return_type=doc.get('ReturnType', ''),
            type=doc.get('Type', '')
        )

    # ------------------Parallel parsing--------------------------------------

    # Batch helper function as YAML document batches generator
    def _sanitize_and_generate_batches(self, batch_size: int):
        """
        Stream the YAML file line-by-line, identify YAML document boundaries ('---'),
        and yield batches of *raw YAML text* (not parsed docs), where each batch
        contains batch_size documents.

        This avoids loading the entire file or large chunks into memory.
        """
        batch_lines = []          # lines belonging to the current batch
        docs_in_batch = 0         # number of documents in the current batch
        current_doc_lines = []    # lines of the current YAML document

        with open(self.index_file_path, 'r', errors='ignore') as f:
            for raw_line in f:
                line = raw_line.replace('\t', '  ')

                # Detect YAML document start
                if line.lstrip().startswith('---'):
                    # If previous doc exists, flush it into the batch
                    if current_doc_lines:
                        batch_lines.extend(current_doc_lines)
                        docs_in_batch += 1
                        current_doc_lines = []

                        # If batch is full → yield as one big YAML string
                        if docs_in_batch >= batch_size:
                            yield ''.join(batch_lines)
                            batch_lines = []
                            docs_in_batch = 0

                    # Start a new document
                    current_doc_lines = [line]
                else:
                    current_doc_lines.append(line)

            # EOF: flush the last document
            if current_doc_lines:
                batch_lines.extend(current_doc_lines)
                docs_in_batch += 1

        # yield final partial batch
        if batch_lines:
            yield ''.join(batch_lines)


    def _parallel_parse(self, num_workers: int, batch_size: int = 1000):
        batch_size = max(batch_size, self.log_batch_size)
        logger.info(f"Parallel YAML parsing with {num_workers} workers ( 1 batch={batch_size} symbols)")

        futures = {}
        max_pending = num_workers * 5

        batch_iter = self._sanitize_and_generate_batches(batch_size)
        ctx = get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=num_workers,
            mp_context=ctx, 
            initializer=_yaml_worker_initializer,
            initargs=(self.log_batch_size,)
        ) as executor:

            # Prime the worker queue
            for _ in range(max_pending):
                try:
                    batch = next(batch_iter)
                    fut = executor.submit(_yaml_worker_process, batch)
                    futures[fut] = True
                except StopIteration:
                    break

            with tqdm(desc=align_string("Parsing YAML"), unit="batch", total=0) as pbar:
                while futures:
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)

                    for fut in done:
                        futures.pop(fut)
                        pbar.total += 1
                        pbar.update(1)
                        # Submit next batch
                        try:
                            batch = next(batch_iter)
                            nf = executor.submit(_yaml_worker_process, batch)
                            futures[nf] = True
                        except StopIteration:
                            pass

                        try:
                            symbols, refs, rels = fut.result()
                            self.symbols.update(symbols)
                            self.unlinked_refs.extend(refs)
                            self.unlinked_relations.extend(rels)
                        except Exception as e:
                            logger.error(f"YAML worker failed: {e}", exc_info=True)

# ============================================================
# Global worker parser, initializer and worker function
# ============================================================
_worker_parser = None

def _yaml_worker_process(batch):
    global _worker_parser

    # Clear the scratch lists/dicts for this batch
    _worker_parser.symbols = {}
    _worker_parser.unlinked_refs = []
    _worker_parser.unlinked_relations = []

    # Parse the batch
    _worker_parser._load_from_string(batch)

    return (_worker_parser.symbols,
            _worker_parser.unlinked_refs,
            _worker_parser.unlinked_relations)

def _yaml_worker_initializer(log_batch_size):
    global _worker_parser
    _worker_parser = SymbolParser("", log_batch_size)
