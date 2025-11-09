#!/usr/bin/env python3
"""
This module provides the SourceSpanProvider class. 

In the refactored architecture, this class acts as an ADAPTER and ENRICHER. It
takes a SymbolParser and a CompilationManager and its primary purpose is to
enrich the in-memory Symbol objects with `body_location` data.
"""

import logging
import os, gc
from typing import List, Optional, Dict

from urllib.parse import urlparse, unquote

from clangd_index_yaml_parser import SymbolParser
from compilation_manager import CompilationManager
from compilation_parser import SourceSpan # Import the new SpanNode dataclass

logger = logging.getLogger(__name__)

class SourceSpanProvider:
    """
    Matches pre-parsed span data from a CompilationManager with Symbol objects
    from a SymbolParser, enriching them in-place with `body_location` data.
    """
    def __init__(self, symbol_parser: Optional[SymbolParser], compilation_manager: CompilationManager):
        """
        Initializes the provider with the necessary data sources.
        No actual work is done in the constructor.
        """
        self.symbol_parser = symbol_parser
        self.compilation_manager = compilation_manager
        self.matched_symbols_count = 0

    def _build_lookup_from_tree(self, node: SourceSpan, file_uri: str, lookup_table: Dict):
        """Recursively traverses a SpanNode tree to populate a flat lookup table."""
        # Key is (name, file_uri, name_start_line, name_start_column)
        key = (
            node.name,
            file_uri,
            node.name_location.start_line,
            node.name_location.start_column
        )
        # The value is the body span, which is what the symbol needs
        lookup_table[key] = node.body_location

    def enrich_symbols_with_span(self):
        """
        Performs the main enrichment process. It gets the new SpanTree data from the
        compilation manager, builds a lookup table by traversing the trees, and then
        matches it against the symbols to attach the `body_location` attribute.
        """
        if not self.symbol_parser:
            logger.warning("No SymbolParser provided to SourceSpanProvider; cannot enrich symbols.")
            return

        # 1. Get the new SpanTree data structure
        span_tree_data = self.compilation_manager.get_source_spans()
        
        # 2. Process the SpanTree into a flat lookup table for fast matching
        spans_lookup = {}
        logger.info(f"Processing SpanTrees from {len(span_tree_data)} files for enrichment.")

        for file_uri, source_spans in span_tree_data.items():
            for source_span in source_spans:
                self._build_lookup_from_tree(source_span, file_uri, spans_lookup)
        
        # 3. Match symbols against the lookup table and enrich
        matched_count = 0
        # Iterate through ALL symbols, not just functions
        for sym in self.symbol_parser.symbols.values():
            # We can only match symbols that have a declaration/definition location
            primary_location = sym.definition or sym.declaration
            if primary_location:
                key = (
                    sym.name,
                    primary_location.file_uri,
                    primary_location.start_line,
                    primary_location.start_column
                )
                
                body_span = spans_lookup.get(key)
                if body_span:
                    # Enrich the Symbol object directly in-place
                    sym.body_location = body_span
                    matched_count += 1
        
        self.matched_symbols_count = matched_count
        logger.info(f"Matched and enriched {self.matched_symbols_count} symbols with body spans.")

        # 4. Clean up references to free memory
        self.symbol_parser = None
        del span_tree_data, spans_lookup
        gc.collect()

    def get_matched_count(self) -> int:
        """Returns the number of symbols that were successfully enriched."""
        return self.matched_symbols_count
