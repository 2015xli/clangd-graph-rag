import logging
import gc
from typing import Optional, Dict, Tuple

from clangd_index_yaml_parser import SymbolParser
from compilation_engine import CompilationManager, SourceSpan
from .matcher import MatcherMixin
from .hierarchy import HierarchyMixin
from .enrich_extras import EnrichExtrasMixin
from .span_helpers import UtilsMixin

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class SourceSpanProvider(MatcherMixin, HierarchyMixin, EnrichExtrasMixin, UtilsMixin):
    """
    Matches pre-parsed span data from a CompilationManager with Symbol objects
    from a SymbolParser, enriching them in-place with `body_location` and `parent_id`.
    
    Identity pluralism is supported: 
    - Tier 1 uses high-performance direct ID matching (USR-based).
    - Tier 2 uses location matching (coordinate-based fallback).
    - Tier 3 uses iterative semantic context matching.
    """
    def __init__(self, symbol_parser: Optional[SymbolParser], compilation_manager: CompilationManager):
        """
        Initializes the provider with the necessary data sources.
        """
        self.symbol_parser = symbol_parser
        self.compilation_manager = compilation_manager
        self.synthetic_id_to_index_id: Dict[str, str] = {} # Maps synthetic span IDs to canonical clangd Symbol IDs
        # Maps a sym's ID to its matched sourcespan's parent_id. 
        # If the parent_id is mapped to a clangd symbol id, then the sym's parent_id is set to that id.
        self.sym_source_span_parent: Dict[str, str] = {} 
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
        self.assigned_parent_by_member_list = 0

    def enrich_symbols_with_span(self):
        """
        Orchestrates the enrichment of in-memory Symbol objects with span data.
        This is a multi-pass process that prioritizes semantic matching over coordinate matching.
        """
        if not self.symbol_parser:
            logger.warning("No SymbolParser provided; cannot enrich symbols.")
            return
       
        # Step 1: Preliminary parent assignment purely based on clangd index data.
        self._filter_symbols_by_project_path()
        self._assign_parent_ids_from_symbol_ref_container()
        self._infer_parent_ids_from_scope()
        
        # Step 2: Do the span matching to enrich symbols with body_location and parent_id
        file_span_data = self.compilation_manager.get_source_spans()
        
        # Flatten all spans into a single map for O(1) ID lookups across all files.
        all_remaining_spans: Dict[str, Tuple[str, SourceSpan]] = {
            span.id: (file_uri, span)
            for file_uri, spans in file_span_data.items()
            for span in spans.values()
        }
        
        # TIER 1: High-performance ID matching (authoritative for semantic parsers).
        self._match_symbols_by_id(all_remaining_spans)
        
        # TIER 2: Location-based matching (fallback for coordinate divergence or syntactic parsers).
        self._match_symbols_by_location(all_remaining_spans)        
        
        # Propagate parentage after initial matching rounds.
        self._assign_sym_parent_based_on_sourcespan_parent(after_adding_syn_symbols=False)
        
        # TIER 3: Iterative semantic context matching (safety net for macro-expanded hierarchies).
        self._match_symbols_by_context_fallback(all_remaining_spans)

        # Step 3: Synthesis. Any remaining spans are added as new synthetic nodes.
        self._create_synthetic_symbols(all_remaining_spans)

        # Now we have all the symbol matching done, including the synthetic symbols.
        self._assign_sym_parent_based_on_sourcespan_parent(after_adding_syn_symbols=True)

        # Use semantic member lists from AST to link macro-generated members to their parents.
        self._assign_parent_ids_from_member_lists(file_span_data)
        
        # Memory management: drop the large temporary map.
        del all_remaining_spans
        gc.collect()
        
        # Step 4: Assign parents for symbols without body (such as variables, fields, etc). 
        self._assign_parent_ids_lexically()

        # Step 5: Finalize hierarchy and additional metadata.
        self._enrich_with_type_alias_data() 
        self._discover_macros() 
        self._enrich_with_static_calls()

        logger.info(f"Enrichment complete. Matched {self.matched_symbol_count} symbols with body spans.")
        self.assigned_parent_count = self.assigned_parent_in_sym + self.assigned_sym_parent_in_span + \
                                     self.assigned_syn_parent_in_span + self.assigned_parent_no_span + \
                                     self.assigned_parent_by_span + self.assigned_parent_by_member_list
        logger.info(f"Total parent_id assigned to {self.assigned_parent_count} symbols.")

    def get_matched_count(self) -> int:
        return self.matched_symbol_count + self.matched_typealias_count

    def get_assigned_count(self) -> int:
        return self.assigned_parent_count
