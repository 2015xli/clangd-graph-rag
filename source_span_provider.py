#!/usr/bin/env python3
"""
This module provides the SourceSpanProvider class.

Its primary purpose is to enrich the in-memory Symbol objects with body spans
and parent-child relationship information derived from the source code's
lexical structure. It handles both named and anonymous structures by
synthesizing new Symbol objects for anonymous entities.
"""

import logging
import gc, copy, sys
import hashlib
from typing import Optional, Dict, List, Set
from urllib.parse import urlparse, unquote

from clangd_index_yaml_parser import SymbolParser, Symbol, Location, RelativeLocation
from compilation_manager import CompilationManager
from compilation_parser import SourceSpan, SpanTreeNode, CompilationParser

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class SourceSpanProvider:
    """
    Matches pre-parsed span data from a CompilationManager with Symbol objects
    from a SymbolParser, enriching them in-place with `body_location` and `parent_id`.
    """
    def __init__(self, symbol_parser: Optional[SymbolParser], compilation_manager: CompilationManager):
        """
        Initializes the provider with the necessary data sources.
        """
        self.symbol_parser = symbol_parser
        self.compilation_manager = compilation_manager

    def enrich_symbols_with_span(self):
        """
        Efficient two-pass enrichment:
        Pass 1: Enrich existing symbols with body spans and synthesize missing
                symbols for anonymous structures.
        Pass 2: Assign `parent_id` to all symbols based on the lexical
                structure discovered by the parser.
        """
        if not self.symbol_parser:
            logger.warning("No SymbolParser provided; cannot enrich symbols.")
            return

        logger.info("Filtering symbols to only include those in the project path.")
        project_path = self.compilation_manager.project_path
        
        keys_to_remove = []
        for sym_id, sym in self.symbol_parser.symbols.items():
            loc = sym.definition or sym.declaration
            if loc:
                sym_abs_path = unquote(urlparse(loc.file_uri).path)
                if sym_abs_path.startswith(project_path) or sym.kind in ("Namespace"):
                    continue
                
            keys_to_remove.append(sym_id)

        logger.info(f"Filtered {len(self.symbol_parser.symbols)} symbols to {len(self.symbol_parser.symbols) - len(keys_to_remove)} symbols.")        
        for key in keys_to_remove:
            del self.symbol_parser.symbols[key]
        
        # Pass 0: Get span data
        # file_span_data: Dict[file_uri → Dict[key → SourceSpan]}
        file_span_data = self.compilation_manager.get_source_spans()
        logger.info(f"Processing SpanTrees from {len(file_span_data)} files for enrichment.")

        # Pass 1: Enrich existing symbols and synthesize new ones
        synthetic_id_to_index_id = {}
        matched_body_count = 0
        assigned_parent_in_sym = 0

        # Copy the file span data to avoid modifying the original data
        file_span_data_copy = copy.deepcopy(file_span_data)

        for sym_id, sym in self.symbol_parser.symbols.items():
            loc = sym.definition or sym.declaration
            if not loc:
                continue

            # Step 1: Assign parent id from symbol's references    
            # Most symbols can find parent id in their references.
            for ref in sym.references:
                # ref.kind: declaration (1)| definition (2)| reference (4) | spelled (8) | call (16)
                # We check if the reference is not a reference to the symbol, but either a definition or a declaration
                # Hopefully we only want symbols with definition. But pure virtual function has only declaration.
                # So we use both definition and declaration, but ensure it is not a reference and has a container_id
                if ref.location == loc and ref.kind & 3 and not ref.kind & 4 and ref.container_id != '0000000000000000':
                    sym.parent_id = ref.container_id 
                    assigned_parent_in_sym += 1
                    break    
            
            # Step 2: Find body spans for symbols from span data            
            key = CompilationParser.make_symbol_key(sym.name, loc.file_uri, loc.start_line, loc.start_column)
            source_span = file_span_data_copy[loc.file_uri].get(key)
            if not source_span:
                continue

            # Enrich the existing symbol with its body location
            sym.body_location = source_span.body_location
            # So far we can only match symbols that have body span, since we don't return nodes from clang.cindex that have no body span.
            # Our purpose with body span is:
            # 1. Find entity's body code for graphRAG, 2. Find parent relationships for symbols.
            # Purpose 1 is served well. 
            # Purpose 2 is fine too, since for symbols without body span, they either have parent id from their references, or we can find parent id from their lexical scope in pass 3.
            # The following map is critical for pass 2 and 3, since we need real matched symbol id as parent id to build graph relationships.
            # That is why we need separate passes for pass 2 and 3, only after pass 1 that has built the map.
            synthetic_id_to_index_id[source_span.id] = sym_id
            matched_body_count += 1
            # Remove the span from the file spans in order to use the remaining spans for synthetic symbols
            del file_span_data_copy[loc.file_uri][key]

        # Pass 2: Any remaining spans in the all file spans are not clangd-indexed (such as anonymous structures or unused symbols)
        # Note we add parent id for the synthetic symbols if they have one in their source span
        synthetic_symbols = {}
        assigned_sym_parent_in_span = 0
        assigned_syn_parent_in_span = 0
        for file_uri, key_spans in file_span_data_copy.items():
            for key, source_span in key_spans.items():
                synth_parent_id = source_span.parent_id
                parent_id = synthetic_id_to_index_id.get(synth_parent_id, synth_parent_id)
                if parent_id != synth_parent_id:
                    assigned_sym_parent_in_span += 1
                elif parent_id:
                    assigned_syn_parent_in_span += 1

                synthetic_symbols[source_span.id] = self._create_synthetic_symbol(source_span, file_uri, parent_id)

        # Add the new synthetic symbols to the main symbol parser
        self.symbol_parser.symbols.update(synthetic_symbols)
        logger.info(f"Matched and enriched {matched_body_count} existing symbols; added {len(synthetic_symbols)} synthetic symbols.")
        logger.info(f"Assigned parent_id to symbols: {assigned_parent_in_sym} by ref, {assigned_sym_parent_in_span} by span sym id, {assigned_syn_parent_in_span} by span syn id.")
        del file_span_data_copy, synthetic_symbols
        gc.collect()

        # Pass 3: Assign parent IDs to remaining symbols that don't have parent id from clangd-index and clang.cindex
        # Top level symbols have no parent id. 
        # TODO: May give parent id with file path to top level symbols, so that we don't need to manually extract (FILE) -[:DEFINES]-> (<symbol>) relationships.
        assigned_parent_no_span = 0
        assigned_parent_by_span = 0
        for sym_id, sym in self.symbol_parser.symbols.items():
            # Skip symbols that already assigned parent id in pass 1
            if sym.parent_id:
                continue

            if False:
                if sym_id == '3C6AD8457679DEAB':
                    logger.info(f"Found symbol: {sym}")

            # Use the symbol's location to find its parent symbol by lexical scope container id lookup
            # We prioritize definition over declaration, but fall back to declaration if needed
            # Declaration is needed for pure virtual functions
            loc = sym.definition or sym.declaration
            if not loc:
                continue
            
            # Namespace symbols are allowed to not be in the project path
            sym_abs_path = unquote(urlparse(loc.file_uri).path)
            if not sym_abs_path.startswith(project_path):
                continue

            # For symbols that don't have parent id, try to find the innermost container span's id
            # ==== Step 1: Find parent id for single name symbols that have no body
            parent_synth_id = None
            parent_id = None

            # Fields: assign parent via enclosing container to names that have no body (just a name).
            variable_kind = {"Field", "Variable", "EnumConstant", "StaticProperty"}
            # This is for the functions that are not defined in the code, like constructor() = 0;
            # For this kind of functions, we ensure they don't have body definition.
            function_kind = {"Constructor", "Destructor", "InstanceMethod", "ConversionFunction"}
            if sym.kind in variable_kind or (sym.kind in function_kind and not sym.body_location):
                field_name = RelativeLocation(loc.start_line, loc.start_column, loc.end_line, loc.end_column)
                field_span = SourceSpan(sym.name, "Variable", sym.language, field_name, field_name, '', '')
                span_tree = file_span_data.get(loc.file_uri, {}) 
                container = self._find_innermost_container(span_tree, field_span)
                if container:
                    #container_key = CompilationParser.make_symbol_key(container.name, loc.file_uri, container.name_location.start_line, container.name_location.start_column)
                    #parent_span = span_tree.get(container_key)
                    # A container should always have a synthetic id, so no checking
                    #parent_synth_id = parent_span.id

                    parent_synth_id = container.id
                    assigned_parent_no_span += 1

                else:
                    if sym.kind in {"Variable"}:
                        # TODO: make sure they are top level Variables
                        continue

                    logger.debug(f"Could not find container for no-body {sym.kind} -- {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
            else:
                # Other symbols: find parent id from span lookup
                if sym.kind in {"TypeAlias"}:
                    # We don't support TypeAlias symbols at the moment.
                    continue

                if False:
                    if sym.name == "testing_start_info":
                        logger.info(f"Symbol with body: {sym.kind} -- {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")

                key = CompilationParser.make_symbol_key(sym.name, loc.file_uri, loc.start_line, loc.start_column)
                span_tree = file_span_data.get(loc.file_uri, {})
                if not span_tree:
                    if not sys.argv[0].endswith("clangd_graph_rag_updater.py"):
                        # The symbol is extended from the seed symbols, which we may not compile its source file at all.
                        logger.debug(f"Could not find span tree for file {loc.file_uri}, symbol {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
                    continue

                span = span_tree.get(key)
                if not span:  # No matching container found. Can be non-container symbols like TypeAlias.
                    logger.debug(f"Could not find body span for with-body {sym.kind} -- {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
                    continue

                parent_synth_id = span.parent_id
                if not parent_synth_id: # Matching body span has no parent container. Is top level.
                    continue
                assigned_parent_by_span += 1

            # Resolve the parent's ID (use the real ID if it exists, otherwise the synthetic one)
            parent_id = synthetic_id_to_index_id.get(parent_synth_id, parent_synth_id)
            if parent_id == sym.id:
                logger.warning(f"Found same parent id {parent_id} for {sym.kind} -- {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
                continue

            sym.parent_id = parent_id

        logger.info(f"Found remaining symbols' parent with lexical scope: {assigned_parent_no_span} without body, {assigned_parent_by_span} with body.")
        assigned_count = assigned_parent_in_sym + assigned_sym_parent_in_span + assigned_syn_parent_in_span + assigned_parent_no_span + assigned_parent_by_span
        logger.info(f"Total parent_id assigned to {assigned_count} symbols.")
        self.matched_count = matched_body_count
        self.assigned_count = assigned_count
        # Cleanup
        del file_span_data, synthetic_id_to_index_id
        gc.collect()

    def get_matched_count(self) -> int:
        return self.matched_count

    def get_assigned_count(self) -> int:
        return self.assigned_count


    # ============================================================
    # Span utilities
    # ============================================================
    def _span_is_within(self, inner: SourceSpan, outer: SourceSpan) -> bool:
        """Check if 'inner' span is fully inside 'outer' span."""
        s1, e1 = inner.body_location, outer.body_location

        # Condition: inner.start >= outer.start AND inner.end <= outer.end

        # If outer and inner are completely overlapping, they are not nested.
        if s1.start_line == e1.start_line and s1.start_column == e1.start_column and s1.end_line == e1.end_line and s1.end_column == e1.end_column:
            return False
        # If outer span is a single line, it cannot contain inner span, unless inner span is just a variable (Variable or Field)
        # We only compare with Variable because we create fake variable spans for both fields and variables
        if inner.kind != "Variable" and e1.start_line == e1.end_line: 
            return False

        if (s1.start_line > e1.start_line or
            (s1.start_line == e1.start_line and s1.start_column >= e1.start_column)):
            if (s1.end_line < e1.end_line or
                (s1.end_line == e1.end_line and s1.end_column <= e1.end_column)):
                return True
        return False

    # -------------------------------------------------------------------------
    def _find_innermost_container(self, span_tree: dict[str, SourceSpan], span: SourceSpan):
        """Find the smallest enclosing SourceSpan node for a given position."""
        candidates = []
        for node in span_tree.values():
            if self._span_is_within(span, node):
                candidates.append(node)
        if not candidates:
            return None
        # Return the most deeply nested one
        return min(candidates, key=lambda s: (s.body_location.end_line - s.body_location.start_line))

    def _create_synthetic_symbol(self, span: SourceSpan, file_uri: str, parent_id: Optional[str]) -> Symbol:
        """Constructs a minimal Symbol object for synthetic entities (anonymous structures)."""
        loc = Location(
            file_uri=file_uri,
            start_line=span.name_location.start_line,
            start_column=span.name_location.start_column,
            end_line=span.name_location.end_line,
            end_column=span.name_location.end_column
        )

        return Symbol(
            id=span.id, 
            name=span.name,
            kind=span.kind,
            declaration=loc,
            definition=loc,
            references=[],
            scope="", # Scope is now handled by the parent_id relationship
            language=span.lang,
            body_location=span.body_location,
            parent_id=parent_id
        )

