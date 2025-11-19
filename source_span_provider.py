#!/usr/bin/env python3
"""
This module provides the SourceSpanProvider class.

Its primary purpose is to enrich the in-memory Symbol objects with body spans
and parent-child relationship information derived from the source code's
lexical structure. It handles both named and anonymous structures by
synthesizing new Symbol objects for anonymous entities.
"""

import logging
import gc
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
        new_symbols = {}
        for sym_id, sym in self.symbol_parser.symbols.items():
            loc = sym.definition or sym.declaration
            if not loc:
                continue
            sym_abs_path = unquote(urlparse(loc.file_uri).path)
            if not sym_abs_path.startswith(project_path):
                if sym.kind not in ("Namespace"):
                    continue
            
            new_symbols[sym_id] = sym

        logger.info(f"Filtered {len(self.symbol_parser.symbols)} symbols to {len(new_symbols)} symbols.")
        del self.symbol_parser.symbols
        gc.collect()

        self.symbol_parser.symbols = new_symbols

        # 0. Get span data
        flat_span_tree_data = self.compilation_manager.get_source_spans()
        logger.info(f"Processing SpanTrees from {len(flat_span_tree_data)} files for enrichment.")
        span_tree_data = self._build_span_forest(flat_span_tree_data)
        del flat_span_tree_data
        gc.collect()
        # 1. Build span and parent lookup tables from the SpanTree data
        spans_lookup = self._build_span_and_parent_lookups(span_tree_data)

        # 2. Pass 1 — Enrich existing symbols and synthesize new ones
        synthetic_symbols = {}
        synthetic_id_to_index_id = {}
        matched_count = 0
        spans_lookup_copy = spans_lookup.copy()

        for sym_id, sym in self.symbol_parser.symbols.items():
            loc = sym.definition or sym.declaration
            if not loc:
                continue
            
            # Use the location from the symbol to find the corresponding span
            key = (sym.name, loc.file_uri, loc.start_line, loc.start_column)
            id_span_parent = spans_lookup.get(key)
            if not id_span_parent:
                continue

            synth_id, span, parent_id = id_span_parent
            # Enrich the existing symbol with its body location
            sym.body_location = span.body_location
            # Map the synthetic ID to the real symbol ID
            synthetic_id_to_index_id[synth_id] = sym_id
            matched_count += 1
            # Remove the span from the lookup since it's been matched
            del spans_lookup[key]

        # Any remaining spans in the lookup are anonymous structures
        for key, (synth_id, span, _) in spans_lookup.items():
            file_uri = key[1]
            synthetic_symbols[synth_id] = self._create_synthetic_symbol(synth_id, span, file_uri)

        # Add the new synthetic symbols to the main symbol parser
        self.symbol_parser.symbols.update(synthetic_symbols)
        logger.info(f"Matched and enriched {matched_count} existing symbols; added {len(synthetic_symbols)} synthetic symbols for anonymous structures.")

        # 3. Pass 2 — Assign parent IDs to all symbols
        assigned_parent_in_sym = 0
        assigned_parent_no_def = 0
        assigned_parent_with_def = 0

        for sym_id, sym in self.symbol_parser.symbols.items():
            if False:
                if sym_id == '3C6AD8457679DEAB':
                    logger.info(f"Found symbol: {sym}")

            # Use the symbol's location to find its parent in the lookup
            # We prioritize definition over declaration, but fall back to declaration if needed
            # Declaration is needed for pure virtual functions
            loc = sym.definition or sym.declaration
            if not loc:
                continue

            # ==== Step 1: Use existing symbol's reference container id for parent symbol
            # Most symbols can find parent id here.
            found_parent_id = False
            for ref in sym.references:
                # ref.kind: declaration (1)| definition (2)| reference (4) | spelled (8) | call (16)
                # We check if the reference is not a reference to the symbol, but either a definition or a declaration
                # Hopefully we only want symbols with definition. But pure virtual function has only declaration.
                # So we use both definition and declaration, but ensure it is not a reference and has a container_id
                if ref.location == loc and ref.kind & 3 and not ref.kind & 4 and ref.container_id != '0000000000000000':
                    sym.parent_id = ref.container_id 
                    assigned_parent_in_sym += 1
                    found_parent_id = True
                    break    
            if found_parent_id:
                continue

            # ==== Step 2: Use lexical scope lookup for parent symbol
            # For symbols that has body spans, get parent id from its span.parent_id
            # For symbols that don't have body spans, try to find the innermost container span's id

            parent_synth_id = None
            parent_id = None
            key = (sym.name, loc.file_uri, loc.start_line, loc.start_column)

            # Fields: assign parent via enclosing container to names that have no body (just a name).
            variable_kind = {"Field", "Variable", "EnumConstant", "StaticProperty"}
            # This is for the functions that are not defined in the code, like constructor() = 0;
            # For this kind of functions, we ensure they don't have body definition.
            function_kind = {"Constructor", "Destructor", "InstanceMethod", "ConversionFunction"}
            if sym.kind in variable_kind or sym.kind in function_kind and not sym.body_location:
                field_name = RelativeLocation(loc.start_line, loc.start_column, loc.end_line, loc.end_column)
                field_span = SourceSpan(sym.name, "Variable", sym.language, field_name, field_name, 0, 0)
                span_tree = span_tree_data.get(loc.file_uri, [])
                container = self._find_innermost_container(span_tree, field_span)
                if container:
                    #parent_synth_id = self._make_synthetic_id(loc.file_uri, container)
                    container_key = (container.name, loc.file_uri, container.name_location.start_line, container.name_location.start_column)
                    id_span_parent = spans_lookup_copy.get(container_key)
                    # A container should always have a synthetic id, so no checking
                    parent_synth_id = id_span_parent[0]
                    assigned_parent_no_def += 1

                else:
                    if sym.kind in {"Variable"}:
                        # TODO: make sure they are top level Variables
                        continue

                    logger.debug(f"Could not find container for no-definition {sym.kind} -- {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
            else:
                # Other symbols: find parent id from span lookup
                if sym.kind in {"TypeAlias"}:
                    # We don't support TypeAlias symbols at the moment.
                    continue

                if False:
                    if sym.name == "testing_start_info":
                        logger.info(f"Found structure for {sym.kind} -- {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")

                id_span_parent = spans_lookup_copy.get(key)
                if not id_span_parent:  # No matching container found. Can be non-container symbols like TypeAlias.
                    logger.debug(f"Could not find container for with-definition {sym.kind} -- {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
                    continue

                parent_synth_id = id_span_parent[2]
                if not parent_synth_id: # Matching container has no parent container. Is top level.
                    continue

                parent_synth_id_2 = id_span_parent[1].parent_id
                if parent_synth_id_2 != None:
                    if parent_synth_id != parent_synth_id_2:
                        logger.debug(f"Found different parent id {parent_synth_id} and {parent_synth_id_2} for {sym.kind} -- {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
                
                parent_synth_id =parent_synth_id_2

                assigned_parent_with_def += 1

            # Resolve the parent's ID (use the real ID if it exists, otherwise the synthetic one)
            parent_id = synthetic_id_to_index_id.get(parent_synth_id, parent_synth_id)
            if parent_id == sym.id:
                logger.warning(f"Found same parent id {parent_id} for {sym.kind} -- {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
                continue

            sym.parent_id = parent_id
            assigned_parent_in_sym += 1

        assigned_count = assigned_parent_in_sym + assigned_parent_with_def + assigned_parent_no_def
        logger.info(f"Assigned parent_id to {assigned_count} symbols: by sym ref: {assigned_parent_in_sym}, by lexical nesting: {assigned_parent_with_def} with definition, {assigned_parent_no_def} without definition.")

        self.matched_count = matched_count
        self.assigned_count = assigned_count
        # Cleanup
        del span_tree_data, spans_lookup, spans_lookup_copy, synthetic_id_to_index_id, synthetic_symbols
        gc.collect()

    def get_matched_count(self) -> int:
        return self.matched_count

    def get_assigned_count(self) -> int:
        return self.assigned_count

    # ============================================================
    # Span forest construction utilities
    # ============================================================

    def _build_span_forest(self, spans_per_file: Dict[str, Set[SourceSpan]]) -> Dict[str, List[SpanTreeNode]]:
        """
        Build a hierarchical span forest (list of roots) for each file.

        Args:
            spans_per_file: Output from Clang worker [(file_uri, [SourceSpan, ...])]

        Returns:
            Dict[file_uri, List[SpanTreeNode]]
        """
        forests: Dict[str, List[SpanTreeNode]] = {}

        for file_uri, spans in spans_per_file.items():
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

            debug_stack_set = set()
            for span in spans_sorted:
                if False:
                    if span.name == "testing_start_info":
                        logger.info(f"Found span for {span.name} at {file_uri}:{span.name_location.start_line}:{span.name_location.start_column}")
                
                node = SpanTreeNode(span)
                # pop stack until current node fits as child
                while stack and not self._span_is_within(span, stack[-1].span):
                    stack.pop()

                if stack:
                    stack[-1].add_child(node)
                else:
                    root_nodes.append(node)
                    debug_stack_set.clear()

                stack.append(node)
                
                if False:
                    debug_key = (span.name, span.name_location.start_line, span.name_location.start_column, span.name_location.end_line, span.name_location.end_column)
                    if debug_key in debug_stack_set:
                        logger.warning(f"Duplicate span found when building span forest for {file_uri}: {debug_key}")
                    debug_stack_set.add(debug_key)

            forests[file_uri] = root_nodes

        return forests

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
    def _find_innermost_container(self, span_tree: List[SpanTreeNode], span: SourceSpan):
        """Find the smallest enclosing SourceSpan node for a given position."""
        candidates = []
        for node in span_tree:
            self._collect_enclosing_nodes(node, span, candidates)
        if not candidates:
            return None
        # Return the most deeply nested one
        return min(candidates, key=lambda s: (s.body_location.end_line - s.body_location.start_line))

    def _collect_enclosing_nodes(self, node: SpanTreeNode, span: SourceSpan, candidates: List[SourceSpan]):
        """Recursively collect all enclosing nodes."""
        if self._span_is_within(span, node.span):
            candidates.append(node.span)
        for child in node.children:
            self._collect_enclosing_nodes(child, span, candidates)

    def _build_span_and_parent_lookups(self, span_tree_data):
        """
        Builds two lookup supports in one table by traversing the span tree data.
        1) symbol key -> synthetic_id, SourceSpan
        2) symbol key -> parent synthetic_id
        """
        spans_lookup = {}
        for file_uri, span_forest in span_tree_data.items():
            for node in span_forest:
                self._collect_span_and_parent_info(node, file_uri, spans_lookup, parent_id=None)
        return spans_lookup

    def _collect_span_and_parent_info(self, node: SpanTreeNode, file_uri: str, spans_lookup: Dict, parent_id: Optional[str]):
        """Recursively populates the lookup tables from a SourceSpan."""
        span = node.span
        # The key uniquely identifies a symbol declaration based on its name and location
        key = (span.name, file_uri, span.name_location.start_line, span.name_location.start_column)
        
        if False:
            if span.name == "testing_start_info":
                logger.info(f"Found span for {span.name} at {file_uri}:{span.name_location.start_line}:{span.name_location.start_column}")

        #synth_id = self._make_synthetic_id(file_uri, span) 
        sym_key = CompilationParser.make_symbol_key(span.name, file_uri, span.name_location.start_line, span.name_location.start_column)    
        synth_id = CompilationParser.make_synthetic_id(sym_key)    
        if parent_id:
            spans_lookup[key] = (synth_id, span, parent_id)
        else:
            spans_lookup[key] = (synth_id, span, None)

        # Recurse for children, passing the current node's synthetic ID as their parent_id
        for child in node.children:
            self._collect_span_and_parent_info(child, file_uri, spans_lookup, parent_id=synth_id)

    def _make_synthetic_id(self, file_uri: str, span: SourceSpan) -> str:
        """Generates a deterministic synthetic ID for any structure span."""
        id_str = (f"{file_uri}#{span.name}#{span.kind}#{span.lang}"
        f"{span.name_location.start_line}_{span.name_location.start_column}"
        f"{span.body_location.start_line}_{span.body_location.start_column}_{span.body_location.end_line}_{span.body_location.end_column}")
        return hashlib.md5(id_str.encode()).hexdigest()

    def _create_synthetic_symbol(self, synthetic_id: str, span: SourceSpan, file_uri: str) -> Symbol:
        """Constructs a minimal Symbol object for synthetic entities (anonymous structures)."""
        loc = Location(
            file_uri=file_uri,
            start_line=span.name_location.start_line,
            start_column=span.name_location.start_column,
            end_line=span.name_location.end_line,
            end_column=span.name_location.end_column
        )

        return Symbol(
            id=synthetic_id,
            name=span.name,
            kind=self.compilation_manager.parser_kind_to_index_kind(span.kind, span.lang),
            declaration=loc,
            definition=loc,
            references=[],
            scope="", # Scope is now handled by the parent_id relationship
            language=span.lang,
            body_location=span.body_location
        )

