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
import concurrent.futures
import itertools

from memory_debugger import Debugger # Import Debugger

logger = logging.getLogger(__name__)

# --- YAML tag handling ---
def unknown_tag(loader, tag_suffix, node):
    return loader.construct_mapping(node)

yaml.SafeLoader.add_multi_constructor("!", unknown_tag)

# --- Common Data Classes ---

@dataclass
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

@dataclass
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

@dataclass
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

    # Reads and parses a clangd YAML index in parallel by chunking it in memory.

    def _sanitize_and_chunk_in_memory(self, num_chunks: int) -> List[str]:
        """Reads the source file once, returning a list of sanitized in-memory chunk strings."""
        if num_chunks <= 0:
            raise ValueError("Number of chunks must be positive.")

        logger.info(f"Reading and chunking '{self.index_file_path}' into {num_chunks} in-memory chunks...")
        
        # First, count the documents to determine chunk size
        total_docs = 0
        with open(self.index_file_path, 'r', errors='ignore') as f:
            for line in f:
                if line.startswith('---'):
                    total_docs += 1
        
        if total_docs == 0:
            docs_per_chunk = 0
        else:
            docs_per_chunk = math.ceil(total_docs / num_chunks)

        if docs_per_chunk == 0:
            logger.warning("No YAML documents found. Proceeding with a single chunk.")
            with open(self.index_file_path, 'r', errors='ignore') as f:
                return [f.read().replace('\t', '  ')]

        # Now, read the file again and create the in-memory chunks
        chunks = []
        current_chunk_lines = []
        doc_count_in_chunk = 0
        with open(self.index_file_path, 'r', errors='ignore') as f_in:
            for line in f_in:
                sanitized_line = line.replace('\t', '  ')
                if sanitized_line.startswith('---'):
                    if doc_count_in_chunk >= docs_per_chunk and len(chunks) < num_chunks -1:
                        chunks.append("".join(current_chunk_lines))
                        current_chunk_lines = []
                        doc_count_in_chunk = 0
                    doc_count_in_chunk += 1
                current_chunk_lines.append(sanitized_line)
        
        if current_chunk_lines:
            chunks.append("".join(current_chunk_lines))

        logger.info(f"Successfully created {len(chunks)} in-memory chunks.")
        return chunks

    def _parallel_parse(self, num_workers: int):
        """
        Phase 1 (Parallel): Reads and loads raw data from the index file in parallel.
        """
        logger.info("Sanitizing and chunking YAML file for parallel processing...")
        # Create in-memory chunks from the main file
        content_chunks = self._sanitize_and_chunk_in_memory(num_workers)

        logger.info(f"Starting parallel parsing of {len(content_chunks)} chunks with {num_workers} workers...")
        with concurrent.futures.ProcessPoolExecutor(max_workers=num_workers) as executor:
            results = executor.map(_parse_worker, content_chunks, itertools.repeat(self.log_batch_size))
            
            for i, (symbols_chunk, refs_chunk, relations_chunk) in enumerate(results):
                logger.info(f"Merging results from chunk {i+1}/{len(content_chunks)}...")
                self.symbols.update(symbols_chunk)
                self.unlinked_refs.extend(refs_chunk)
                self.unlinked_relations.extend(relations_chunk)
        
        logger.info("All chunks processed and merged.")

# --- Parallel Parser ---

def _parse_worker(yaml_content_chunk: str, log_batch_size: int) -> Tuple[Dict[str, Symbol], List[Dict], List[Dict], bool]:
    """
    Worker function to parse a YAML content string chunk.
    This function is executed in a separate process.
    """
    # new a local parser since the forked process can only see module-level symbols
    # we only need to use its functions, so no need to pass the index_file_path
    local_parser = SymbolParser("", log_batch_size)
    try:
        local_parser._load_from_string(yaml_content_chunk)
        return local_parser.symbols, local_parser.unlinked_refs, local_parser.unlinked_relations

    except yaml.YAMLError as e:
        logger.error(f"YAML parsing error in worker: {e}")
        return {}, [], [], False



