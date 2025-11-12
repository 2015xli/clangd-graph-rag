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
from typing import Optional, Dict, List, Tuple

from clangd_index_yaml_parser import SymbolParser, Symbol, Location, RelativeLocation
from compilation_manager import CompilationManager
from compilation_parser import SourceSpan, SpanTreeNode

logger = logging.getLogger(__name__)

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

        flat_span_tree_data = self.compilation_manager.get_source_spans()
        logger.info(f"Processing SpanTrees from {len(flat_span_tree_data)} files for enrichment.")
        span_tree_data = self._build_span_forest(flat_span_tree_data)

        # 1. Build span and parent lookup tables from the SpanTree data
        spans_and_ids_lookup, parent_synthetic_id_lookup = self._build_span_and_parent_lookups(span_tree_data)

        # 2. Pass 1 — Enrich existing symbols and synthesize new ones
        synthetic_symbols = {}
        synthetic_id_to_index_id = {}
        matched_count = 0
        spans_and_ids_lookup_copy = spans_and_ids_lookup.copy()

        for sym_id, sym in self.symbol_parser.symbols.items():
            loc = sym.definition or sym.declaration
            if not loc:
                continue
            
            # Use the location from the symbol to find the corresponding span
            key = (sym.name, loc.file_uri, loc.start_line, loc.start_column)
            pair = spans_and_ids_lookup.get(key)
            if not pair:
                continue

            synth_id, span = pair
            # Enrich the existing symbol with its body location
            sym.body_location = span.body_location
            # Map the synthetic ID to the real symbol ID
            synthetic_id_to_index_id[synth_id] = sym_id
            matched_count += 1
            # Remove the span from the lookup since it's been matched
            del spans_and_ids_lookup[key]

        # Any remaining spans in the lookup are anonymous structures
        for key, (synth_id, span) in spans_and_ids_lookup.items():
            file_uri = key[1]
            synthetic_symbols[synth_id] = self._create_synthetic_symbol(synth_id, span, file_uri)

        # Add the new synthetic symbols to the main symbol parser
        self.symbol_parser.symbols.update(synthetic_symbols)
        logger.info(f"Matched and enriched {matched_count} existing symbols; added {len(synthetic_symbols)} synthetic symbols for anonymous structures.")

        # 3. Pass 2 — Assign parent IDs to all symbols
        assigned_count = 0
        for sym_id, sym in self.symbol_parser.symbols.items():
            # Use the symbol's location to find its parent in the lookup
            loc = sym.definition or sym.declaration
            if not loc:
                continue
            
            key = (sym.name, loc.file_uri, loc.start_line, loc.start_column)

            # Fields: assign parent via enclosing container
            if sym.kind in ("Field", "Variable"):
                field_name = RelativeLocation(loc.start_line, loc.start_column, loc.end_line, loc.end_column)
                field_span = SourceSpan(sym.name, "Field", field_name, field_name)
                span_tree = span_tree_data.get(loc.file_uri, [])
                container = self._find_innermost_container(span_tree, field_span)
                if container:
                    #parent_synth_id = self._make_synthetic_id(loc.file_uri, container)
                    container_key = (container.name, loc.file_uri, container.name_location.start_line, container.name_location.start_column)
                    parent_synth_id = spans_and_ids_lookup_copy.get(container_key)[0]
                else:
                    logger.debug(f"Could not find container for field {sym.kind} -- {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
            else:
                parent_synth_id = parent_synthetic_id_lookup.get(key)

            if not parent_synth_id:
                logger.debug(f"Could not find parent container for {sym.kind} -- {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
                continue

            # Resolve the parent's ID (use the real ID if it exists, otherwise the synthetic one)
            parent_id = synthetic_id_to_index_id.get(parent_synth_id, parent_synth_id)
            sym.parent_id = parent_id
            assigned_count += 1

        logger.info(f"Assigned parent_id to {assigned_count} symbols based on lexical nesting.")

        self.matched_count = matched_count
        self.assigned_count = assigned_count
        # Cleanup
        del span_tree_data, spans_and_ids_lookup, parent_synthetic_id_lookup, synthetic_symbols
        gc.collect()

    def get_matched_count(self) -> int:
        return self.matched_count

    def get_assigned_count(self) -> int:
        return self.assigned_count

    # ============================================================
    # Span forest construction utilities
    # ============================================================

    def _build_span_forest(self, spans_per_file: Dict[str, List[SourceSpan]]) -> Dict[str, List[SpanTreeNode]]:
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

            for span in spans_sorted:
                node = SpanTreeNode(span)
                # pop stack until current node fits as child
                while stack and not self._span_is_within(span, stack[-1].span):
                    stack.pop()

                if stack:
                    stack[-1].add_child(node)
                else:
                    root_nodes.append(node)

                stack.append(node)

            forests[file_uri] = root_nodes

        return forests

    def _span_is_within(self, inner: SourceSpan, outer: SourceSpan) -> bool:
        """Check if 'inner' span is fully inside 'outer' span."""
        s1, e1 = inner.body_location, outer.body_location
        # inner.start >= outer.start AND inner.end <= outer.end
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
        Builds two lookup tables by traversing the span tree data.
        1) spans_and_ids_lookup: (name, file_uri, name_line, name_col) -> (synthetic_id, SourceSpan)
        2) parent_lookup: (child_key) -> parent_synthetic_id
        """
        spans_and_ids_lookup = {}
        parent_lookup = {}

        for file_uri, span_forest in span_tree_data.items():
            for node in span_forest:
                self._collect_span_and_parent_info(node, file_uri, spans_and_ids_lookup, parent_lookup, parent_id=None)
        return spans_and_ids_lookup, parent_lookup

    def _collect_span_and_parent_info(self, node: SourceSpan, file_uri: str, spans_lookup, parent_lookup, parent_id: Optional[str]):
        """Recursively populates the lookup tables from a SourceSpane."""
        span = node.span
        # The key uniquely identifies a symbol declaration based on its name and location
        key = (span.name, file_uri, span.name_location.start_line, span.name_location.start_column)
        
        synth_id = self._make_synthetic_id(file_uri, span)
        spans_lookup[key] = (synth_id, span)
        
        if parent_id:
            parent_lookup[key] = parent_id

        # Recurse for children, passing the current node's synthetic ID as their parent_id
        for child in node.children:
            self._collect_span_and_parent_info(child, file_uri, spans_lookup, parent_lookup, parent_id=synth_id)

    def _make_synthetic_id(self, file_uri: str, span: SourceSpan) -> str:
        """Generates a deterministic synthetic ID for any structure span."""
        id_str = f"{file_uri}#{span.body_location.start_line}_{span.body_location.start_column}"
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

        # Embed span info in the name for easier debugging in the graph
        display_name = (
            span.name
            if not span.name.startswith("(anonymous")
            else f"{span.name} [{span.body_location.start_line}:{span.body_location.start_column}]"
        )

        return Symbol(
            id=synthetic_id,
            name=display_name,
            kind=span.kind,
            declaration=loc,
            definition=loc,
            references=[],
            scope="", # Scope is now handled by the parent_id relationship
            language="cpp", # Assume C++ for structures discovered this way
            body_location=span.body_location
        )
