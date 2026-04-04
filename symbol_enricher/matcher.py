import logging
from typing import Dict, Tuple

from clangd_index_yaml_parser import Location
from source_parser import SourceSpan
from utils import make_symbol_key

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class MatcherMixin:
    """Provides methods for matching Clangd symbols with implementation SourceSpans."""

    def _match_symbols_by_id(self, all_remaining_spans: Dict[str, Tuple[str, SourceSpan]]):
        """
        TIER 1: Direct ID Matching.
        Matches Clangd symbols with SourceSpans based purely on their ID (USR-hash).
        This is authoritative for all Clang-parsed AST nodes where IDs mathematically match.
        """
        logger.info("Matching symbols by direct ID lookup...")
        matched_count = 0
        for sym_id, sym in self.symbol_parser.symbols.items():
            # Constant-time lookup. This covers 95%+ of symbols when using the same libclang version as Clangd.
            if sym_id in all_remaining_spans:
                file_uri, source_span = all_remaining_spans.pop(sym_id)
                
                # Enrich with semantic implementation details.
                sym.body_location = source_span.body_location
                sym.original_name = source_span.original_name
                sym.expanded_from_id = source_span.expanded_from_id
                
                # ANCHORING: Move the symbol to the actual implementation file.
                # This ensures the 'path' property in the graph is consistent with 'body_location'.
                # It can be inconsistent if a symbol is defined in a macro, when its name literal is defined in macro.
                # We now use the macro's expansion site as the definition site for all the symbols expanded from this macro.
                sym.definition = Location.from_relative_location(source_span.name_location, file_uri)
                
                # Map the parser ID to the index ID so children can find this parent.
                self.synthetic_id_to_index_id[source_span.id] = sym_id

                # Record parentage for later propagation. We don't assign it here to keep 
                # matching and hierarchy building as separate, stable stages.
                # If we assign sym.parent_id here, this id may have not been matched yet till this iteration, which leaves sym a synthetic parent id.
                if (not sym.parent_id) and source_span.parent_id:
                    self.sym_source_span_parent[sym.id] = source_span.parent_id

                self.matched_symbol_count += 1
                matched_count += 1
        
        logger.info(f"Directly matched {matched_count} symbols by ID.")


    def _match_symbols_by_location(self, all_remaining_spans: Dict[str, Tuple[str, SourceSpan]]):
        """
        TIER 2: Exact location match.
        Matches Clangd symbols with SourceSpans based on their name, kind, and location.
        This handles symbols from syntactic parsers or cases where USRs diverge.
        """
        logger.info("Matching remaining symbols by exact location...")
        
        # 1. Build a lookup for remaining spans by their location-based key.
        # This key is derived from the implementation coordinates.
        remaining_spans_by_loc = {}
        for span_id, (file_uri, span) in all_remaining_spans.items():
            key = make_symbol_key(
                span.name, span.kind, file_uri, 
                span.name_location.start_line, span.name_location.start_column
            )
            # Use a list to handle potential coordinate collisions.
            if key not in remaining_spans_by_loc:
                remaining_spans_by_loc[key] = []
            remaining_spans_by_loc[key].append((span_id, span))

        matched_count = 0
        for sym_id, sym in self.symbol_parser.symbols.items():
            # Skip symbols successfully enriched in Tier 1.
            if sym.body_location:
                continue
                
            loc = sym.definition or sym.declaration
            if not loc:
                continue
            
            # Reconstruct the expected key based on Clangd index data.
            key = make_symbol_key(sym.name, sym.kind, loc.file_uri, loc.start_line, loc.start_column)
            candidates = remaining_spans_by_loc.get(key)
            
            # AMBIGUITY CHECK: Only match if the coordinate context is unique.
            if candidates and len(candidates) == 1:
                span_id, source_span = candidates[0]
                
                # Match found!
                sym.body_location = source_span.body_location
                sym.original_name = source_span.original_name
                sym.expanded_from_id = source_span.expanded_from_id
                
                # Record the mapping for hierarchy propagation.
                self.synthetic_id_to_index_id[source_span.id] = sym_id
                
                # Record parentage for later propagation.
                if (not sym.parent_id) and source_span.parent_id:
                    self.sym_source_span_parent[sym.id] = source_span.parent_id
                
                self.matched_symbol_count += 1
                matched_count += 1
                
                # Remove from both the global flat map and the temporary coordinate map.
                all_remaining_spans.pop(span_id, None)
                del remaining_spans_by_loc[key]

        logger.info(f"Matched {matched_count} symbols by location coordinates.")

    def _match_symbols_by_context_fallback(self, all_remaining_spans: Dict[str, Tuple[str, SourceSpan]]):
        """
        TIER 3: Semantic fallback match.
        Matches remaining macro-generated symbols by their parent context, name, and kind.
        This handles cases where Clangd uses the spelling location (header) 
        but implementation resides in the expansion site.
        """
        logger.info("Performing iterative semantic matching for macro-generated symbols...")
        
        # 1. Identify candidate Clangd symbols once. 
        candidate_symbols_by_context = {}
        for sym_id, sym in self.symbol_parser.symbols.items():
            if sym.body_location: # Only match symbols that haven't been matched yet
                continue
            # Context is based on index ID of the parent.
            context_key = (sym.parent_id, sym.name, sym.kind)
            if context_key in candidate_symbols_by_context:
                candidate_symbols_by_context[context_key] = None # Mark as ambiguous
            else:
                candidate_symbols_by_context[context_key] = sym

        iteration = 0
        total_semantic_matches = 0
        
        # ITERATIVE REFINEMENT: As parents are anchored, their children become matchable.
        while True:
            iteration += 1
            round_matches = 0
            
            # 2. Build lookup for remaining spans. 
            remaining_spans_by_context = {}
            for span_id, (file_uri, span) in all_remaining_spans.items():
                # Map synthetic parent_id to index_id. If parent was anchored in a previous round,
                # this resolution now provides the correct canonical parent ID.
                resolved_parent_id = self.synthetic_id_to_index_id.get(span.parent_id)
                
                # If nested but parent hasn't been anchored yet, skip for this iteration.
                if not (span.parent_id  or resolved_parent_id):
                    continue
                
                context_key = (resolved_parent_id, span.name, span.kind)
                if context_key in remaining_spans_by_context:
                    remaining_spans_by_context[context_key] = None # Ambiguous
                else:
                    remaining_spans_by_context[context_key] = (file_uri, span_id, span)

            # 3. Match round.
            for context_key in list(candidate_symbols_by_context.keys()):
                sym = candidate_symbols_by_context[context_key]
                if sym is None: 
                    continue # Skip ambiguous Clangd symbols.
                
                match_info = remaining_spans_by_context.get(context_key)
                if not match_info: 
                    continue # Missing or ambiguous synthetic span.
                
                file_uri, span_id, source_span = match_info
                
                # Safe one-to-one semantic match found!
                sym.body_location = source_span.body_location
                sym.original_name = source_span.original_name
                sym.expanded_from_id = source_span.expanded_from_id
                
                # ANCHORING: Move definition to implemention file.
                sym.definition = Location.from_relative_location(source_span.name_location, file_uri)
                
                self.synthetic_id_to_index_id[source_span.id] = sym_id

                # Record parentage for later propagation.
                if (not sym.parent_id) and source_span.parent_id:
                    self.sym_source_span_parent[sym.id] = source_span.parent_id

                self.matched_symbol_count += 1
                round_matches += 1
                
                # Cleanup.
                all_remaining_spans.pop(span_id, None)
                del candidate_symbols_by_context[context_key]

            total_semantic_matches += round_matches
            if round_matches > 0:
                logger.info(f"Iteration {iteration}: Semantically matched {round_matches} symbols.")
            
            if round_matches == 0:
                break

        logger.info(f"Successfully iterative-matched {total_semantic_matches} symbols by context.")

    def _create_synthetic_symbols(self, all_remaining_spans: Dict[str, Tuple[str, SourceSpan]]):
        """
        Creation of synthetic symbols for remaining unmatched spans.
        This handles anonymous structures and entities missed by the indexer.
        """
        logger.info("Creating synthetic symbols for remaining unmatched spans...")
        synthetic_symbols = {}
        for span_id, (file_uri, source_span) in all_remaining_spans.items():
            synthetic_symbols[source_span.id] = self._create_synthetic_symbol(source_span, file_uri, None)
            # Register synthetic ID mapping for potential children.
            self.synthetic_id_to_index_id[source_span.id] = source_span.id 

            # Record parentage for later propagation.
            if source_span.parent_id:
                self.sym_source_span_parent[source_span.id] = source_span.parent_id

        # Add the new synthetic symbols to the main symbol parser.
        self.symbol_parser.symbols.update(synthetic_symbols)
        logger.info(f"Added {len(synthetic_symbols)} synthetic symbols.")
