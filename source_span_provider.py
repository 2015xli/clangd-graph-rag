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

from clangd_index_yaml_parser import SymbolParser, Symbol, Reference, Location, RelativeLocation
from compilation_manager import CompilationManager
from compilation_parser import SourceSpan, CompilationParser, TypeAliasSpan

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
        self.type_alias_spans: Dict[str, TypeAliasSpan] = compilation_manager.get_type_alias_spans()
        self.synthetic_id_to_index_id: Dict[str, str] = {} # Maps synthetic span IDs to canonical Symbol IDs
        self.matched_symbol_count = 0
        self.matched_typealias_count = 0
        self.assigned_parent_count = 0
        self.assigned_parent_in_sym = 0
        self.assigned_sym_parent_in_span = 0
        self.assigned_syn_parent_in_span = 0
        self.assigned_parent_no_span = 0
        self.assigned_parent_by_span = 0
        self.assigned_parent_matched_alias = 0
        self.assigned_parent_unmatched_alias = 0

    def enrich_symbols_with_span(self):
        """
        Orchestrates the enrichment of in-memory Symbol objects with span data.
        """
        if not self.symbol_parser:
            logger.warning("No SymbolParser provided; cannot enrich symbols.")
            return

        self._filter_symbols_by_project_path()
        self._assign_parent_ids_from_symbol_ref_container()
        self._match_and_enrich_with_source_spans()
        self._assign_parent_ids_lexically()
        self._enrich_with_type_alias_data() # New pass for TypeAlias
        self._enrich_with_static_calls()

        logger.info(f"Enrichment complete. Matched {self.matched_symbol_count} symbols with body spans.")
        logger.info(f"Total parent_id assigned to {self.assigned_parent_count} symbols.")


    def _filter_symbols_by_project_path(self):
        """
        Filters out symbols whose definitions or declarations are outside the project path.
        Namespace symbols are an exception and are always kept.
        """
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

    def _assign_parent_ids_from_symbol_ref_container(self):
        """
        Assign parent ids to symbols from their references.
        """

        for sym_id, sym in self.symbol_parser.symbols.items():
            loc = sym.definition or sym.declaration
            if not loc:
                continue

            # Ref kind:   Declaration = 1 << 0, // 1
            #             Definition  = 1 << 1, // 2
            #             Reference   = 1 << 2, // 4
            # ref.container_id != '0000000000000000' means it has a container symbol.
            # ref.kind & 3 and not ref.kind & 4, means it is either a definition or declaration, but not a reference.
            # So it means if a symbol has a definition (or declaration) reference inside another symbol's scope, 
            # then the container symbol is its parent.
            for ref in sym.references:
                if ref.location == loc and ref.kind & 3 and not ref.kind & 4 and ref.container_id != '0000000000000000':
                    sym.parent_id = ref.container_id 
                    self.assigned_parent_in_sym += 1
                    break    
            

    def _match_and_enrich_with_source_spans(self):
        """
        Matches existing symbols with SourceSpan data and synthesizes new symbols
        for unmatched SourceSpans (e.g., anonymous structures).
        """
        # Pass 0: Get span data
        file_span_data = self.compilation_manager.get_source_spans()
        logger.info(f"Processing SourceSpans from {len(file_span_data)} files for enrichment.")

        # Copy the file span data to avoid modifying the original data
        file_span_data_copy = copy.deepcopy(file_span_data)

        # Pass 1: Match clangd-indexed symbols with source spans
        for sym_id, sym in self.symbol_parser.symbols.items():
            loc = sym.definition or sym.declaration
            if not loc:
                continue
            
            # Find body spans for symbols from span data            
            key = CompilationParser.make_symbol_key(sym.name, loc.file_uri, loc.start_line, loc.start_column)
            source_span = file_span_data_copy[loc.file_uri].get(key)
            if not source_span:
                continue

            # Found a matched clangd-idexed symbol, enrich it with its body location
            sym.body_location = source_span.body_location
            self.synthetic_id_to_index_id[source_span.id] = sym_id
            self.matched_symbol_count += 1
            # Remove the span from the file spans in order to use the remaining spans for synthetic symbols
            del file_span_data_copy[loc.file_uri][key]

        # Pass 2: Any remaining spans are not clangd-indexed (such as anonymous structures or unused symbols)
        synthetic_symbols = {}
        for file_uri, key_spans in file_span_data_copy.items():
            for key, source_span in key_spans.items():
                synth_parent_id = source_span.parent_id
                parent_id = self.synthetic_id_to_index_id.get(synth_parent_id, synth_parent_id)
                if parent_id != synth_parent_id:
                    # Found parent symbol that is a Clangd indexed symbol
                    self.assigned_sym_parent_in_span += 1
                elif parent_id:
                    # Cannot find a clangd-indexed symbol as parent, use the parser-found scope as parent
                    self.assigned_syn_parent_in_span += 1

                synthetic_symbols[source_span.id] = self._create_synthetic_symbol(source_span, file_uri, parent_id)
                self.synthetic_id_to_index_id[source_span.id] = source_span.id # Map synthetic ID to itself

        # Add the new synthetic symbols to the main symbol parser
        self.symbol_parser.symbols.update(synthetic_symbols)
        logger.info(f"Matched and enriched {self.matched_symbol_count} existing symbols; added {len(synthetic_symbols)} synthetic symbols.")
        logger.info(f"Assigned parent_id to symbols: {self.assigned_parent_in_sym} by ref, {self.assigned_sym_parent_in_span} by span sym id, {self.assigned_syn_parent_in_span} by span synth id.")
        del file_span_data_copy, synthetic_symbols
        gc.collect()

    def _assign_parent_ids_lexically(self):
        """
        Assigns parent_id to symbols that don't have one, based on lexical scope.
        """
        logger.info("Assigning parent IDs based on lexical scope for remaining symbols.")
        # Pass 0: Get span data (again, as it might have been deleted in previous step)
        file_span_data = self.compilation_manager.get_source_spans()

        for sym_id, sym in self.symbol_parser.symbols.items():
            # Skip symbols that already assigned parent id in pass 1
            if sym.parent_id:
                continue

            # Use the symbol's location to find its parent symbol by lexical scope container id lookup
            # We prioritize definition over declaration, but fall back to declaration if needed
            # Declaration is needed for pure virtual functions
            loc = sym.definition or sym.declaration
            if not loc:
                continue
            
            # Namespace symbols are allowed to not be in the project path
            sym_abs_path = unquote(urlparse(loc.file_uri).path)
            if not sym_abs_path.startswith(self.compilation_manager.project_path):
                continue

            # For symbols that don't have parent id, try to find the innermost container span's id
            parent_synth_id = None

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
                    parent_synth_id = container.id
                    self.assigned_parent_no_span += 1

                else:
                    # Variables defined at top level have no parent scope
                    if not sym.kind in {"Variable"}:
                        logger.debug(f"Could not find container for no-body {sym.kind} -- {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
                
                #now we have the parent scope id in parent_synth_id

            else:
                # For other symbols (except Variable and no-body function): find parent id from span lookup
                # We skip two cases:
                # 1. TypeAlias: We handle it separately. They are not managed in SpanTrees.
                # 2. Function without definition, which is only a declaration that we don't care.
                if sym.kind == "TypeAlias" or (sym.kind == "Function" and not sym.definition):
                    continue

                key = CompilationParser.make_symbol_key(sym.name, loc.file_uri, loc.start_line, loc.start_column)
                span_tree = file_span_data.get(loc.file_uri, {})
                if not span_tree:
                    if sys.argv[0].endswith("clangd_graph_rag_builder.py"):
                        # When the graph is incrementally updated (not built from scratch), it is normal that some files don't have span trees.
                        # The reason is, the symbol (and its file) is extended from the seed symbols, whose source file may not be parsed.
                        # We only log the debug message for the builder, when all the source files should be parsed, and have span trees.
                        logger.debug(f"Could not find span tree for file {loc.file_uri}, symbol {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
                    continue

                span = span_tree.get(key)
                if not span:  # No matching container found. 
                    logger.debug(f"Could not find body span for sym with-body {sym.kind} -- {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
                    continue

                parent_synth_id = span.parent_id
                if not parent_synth_id: # Matching body span has no parent container. Is top level.
                    continue

                # Finally we found a matched span for this symbol that also has a parent scope with id parent_synth_id.
                self.assigned_parent_by_span += 1

            # Resolve the parent's ID (use the real ID if it exists, otherwise the synthetic one)
            parent_id = self.synthetic_id_to_index_id.get(parent_synth_id, parent_synth_id)
            if parent_id == sym.id:
                logger.warning(f"Found same parent id {parent_id} for {sym.kind} -- {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
                continue

            sym.parent_id = parent_id

        logger.info(f"Found remaining symbols' parent with lexical scope: {self.assigned_parent_no_span} without body, {self.assigned_parent_by_span} with body.")
        assigned_count = self.assigned_parent_in_sym + self.assigned_sym_parent_in_span + self.assigned_syn_parent_in_span + self.assigned_parent_no_span + self.assigned_parent_by_span
        logger.info(f"Before type alias enrichment, total parent_id assigned to {assigned_count} symbols.")
        self.assigned_parent_count = assigned_count
        # Cleanup
        del file_span_data
        gc.collect()

    def _enrich_with_type_alias_data(self):
        """
        Matches existing TypeAlias symbols with TypeAliasSpan data and synthesizes new symbols
        for unmatched TypeAliasSpans.
        """
        logger.info("Enriching symbols with TypeAlias data.")
        if not self.type_alias_spans:
            logger.info("No TypeAlias spans found to enrich.")
            return

        # Create a copy of the type_alias_spans to allow modification (deletion of matched spans)
        unmatched_type_alias_spans = self.type_alias_spans.copy()

        synthetic_type_alias_symbols = {}
        
        # First Pass: Match existing TypeAlias symbols from clangd index
        for sym_id, sym in self.symbol_parser.symbols.items():
            if sym.kind != 'TypeAlias': continue

            # Use sym.id (USR-derived) to look up a matching TypeAliasSpan
            matched_tas = unmatched_type_alias_spans.get(sym_id)
            if not matched_tas:
                logger.warning(f"Could not find matching TypeAliasSpan for TypeAlias symbol {sym.name} at {sym.definition.file_uri}:{sym.definition.start_line}:{sym.definition.start_column}")
                continue
            
            # Enrich existing Symbol
            sym.aliased_canonical_spelling = matched_tas.aliased_canonical_spelling
            sym.aliased_type_id = self.synthetic_id_to_index_id.get(matched_tas.aliased_type_id, matched_tas.aliased_type_id)
            sym.aliased_type_kind = matched_tas.aliased_type_kind
            sym.body_location = matched_tas.body_location

            if sym.parent_id is None and matched_tas.parent_id:
                self.assigned_parent_matched_alias += 1
                sym.parent_id = self.synthetic_id_to_index_id.get(matched_tas.parent_id, matched_tas.parent_id)
            if sym.scope is None:
                sym.scope = matched_tas.scope

            self.synthetic_id_to_index_id[matched_tas.id] = sym_id # Map synthetic ID to clangd ID
            del unmatched_type_alias_spans[sym_id] # Mark as processed
            self.matched_typealias_count += 1

        # Second Pass: Create Synthetic Symbols for Unmatched TypeAliasSpans
        for unmatched_tas in unmatched_type_alias_spans.values():
            new_sym = self._create_synthetic_type_alias_symbol(unmatched_tas)
            synthetic_type_alias_symbols[new_sym.id] = new_sym
            # add unmatched symbol id to the mapping table for future use.
            # Actually unmatched_tas.id == new_sym.id, since we derive the tas id from USR in parser.
            self.synthetic_id_to_index_id[unmatched_tas.id] = new_sym.id 

        self.symbol_parser.symbols.update(synthetic_type_alias_symbols)
        logger.info(f"Processed {len(self.type_alias_spans)} TypeAlias.")
        logger.info(f"Matched {self.matched_typealias_count} symbols ({self.assigned_parent_matched_alias} newly assigned parent)."
                    f"Added {len(synthetic_type_alias_symbols)} synthetic symbols ({self.assigned_parent_unmatched_alias} with parent)."
                   )

        self.assigned_parent_count += self.assigned_parent_matched_alias + self.assigned_parent_unmatched_alias

    def _create_synthetic_type_alias_symbol(self, tas: TypeAliasSpan) -> Symbol:
        """Constructs a Symbol object for a synthetic TypeAlias."""
        loc = Location.from_relative_location(tas.name_location, file_uri=tas.file_uri)

        # Resolve parent_id and aliased_type_id using the global mapping
        resolved_parent_id = self.synthetic_id_to_index_id.get(tas.parent_id, tas.parent_id)
        if resolved_parent_id:
            self.assigned_parent_unmatched_alias += 1

        resolved_aliased_type_id = self.synthetic_id_to_index_id.get(tas.aliased_type_id, tas.aliased_type_id)

        new_sym = Symbol(
            id=tas.id,
            name=tas.name,
            kind='TypeAlias',
            declaration=loc,
            definition=loc,
            references=[],
            scope=tas.scope,
            language=tas.lang,
            body_location=tas.body_location,
            parent_id=resolved_parent_id,
            aliased_canonical_spelling=tas.aliased_canonical_spelling,
            aliased_type_id=resolved_aliased_type_id,
            aliased_type_kind=tas.aliased_type_kind
        )
        return new_sym

    def get_matched_count(self) -> int:
        return self.matched_symbol_count + self.matched_typealias_count


    def get_assigned_count(self) -> int:
        return self.assigned_parent_count

    def _enrich_with_static_calls(self):
        """
        Injects static call relations (found by compilation_parser) as synthetic
        references into the Symbol objects.
        """
        #("\n--- Starting Pass 4: Enriching with Static Call Relations ---"
        static_calls = self.compilation_manager.get_static_call_relations()
        if not static_calls:
            logger.info("No static call relations found to enrich.")
            return

        # The main symbol dictionary is already keyed by ID (which is the USR).
        # No need to build a new map.

        injected_ref_count = 0
        for caller_usr, callee_usr in static_calls:
            # Look up the callee symbol directly in the main dictionary
            callee_symbol = self.symbol_parser.symbols.get(callee_usr)

            if callee_symbol:
                # Create a synthetic reference. The location is not critical as the
                # call graph builder primarily uses kind and container_id.
                dummy_location = Location(file_uri='', start_line=0, start_column=0, end_line=0, end_column=0)

                # Kind 28 corresponds to a spelled, non-macro function call in modern clangd.
                new_ref = Reference(
                    kind=28,
                    location=dummy_location,
                    container_id=caller_usr
                )
                callee_symbol.references.append(new_ref)
                injected_ref_count += 1

        logger.info(f"Successfully injected {injected_ref_count} static call references.")
        #("--- Finished Pass 4 ---")


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

