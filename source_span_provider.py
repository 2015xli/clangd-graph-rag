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
from typing import Optional, Dict, List, Set, Tuple
from collections import defaultdict
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
        self.sym_source_span_parent:Dict[str, str] = {} 
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

    VARIABLE_KIND = {"Field", "StaticProperty", "EnumConstant", "Variable"}

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
        # Note we don't immediately assign parent_id when we match a symbol, 
        # because the mapping table synthetic_id_to_index_id from parser span id to clangd symbol id 
        # is under building when we match spans. The parent_id may not in the table yet. 
        file_span_data = self.compilation_manager.get_source_spans()
        
        # Flatten all spans into a single map for O(1) ID lookups across all files.
        # This Map: id -> (file_uri, SourceSpan)
        # Note: Flattening is safe because USRs (and thus IDs) are globally unique.
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
        # We need assign sym with parent ids from the matched spans before context-based matching,
        # because context is (sym.parent_id, sym.name, sym.kind). More assigned parent_ids enable more matching.
        self._assign_sym_parent_based_on_sourcespan_parent(after_adding_syn_symbols=False)
        
        # TIER 3: Iterative semantic context matching (safety net for macro-expanded hierarchies).
        self._match_symbols_by_context_fallback(all_remaining_spans)

        # Step 3: Synthesis. Any remaining spans are added as new synthetic nodes.
        # It is like to match the spans to themselves.
        self._create_synthetic_symbols(all_remaining_spans)

        # Now we have all the symbol matching done, including the synthetic symbols (that are virtually matching themselves).
        # It is time to assign all the recorded symbols (no parend_id) with the matched spans parent span id (after converted to symbol id)
        self._assign_sym_parent_based_on_sourcespan_parent(after_adding_syn_symbols=True)

        # Use semantic member lists from AST to link macro-generated members to their parents.
        # This handles cases where members are outside the parent's lexical span.
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

    def _assign_parent_ids_from_member_lists(self, file_span_data: Dict[str, Dict[str, SourceSpan]]):
        """
        Uses semantic member lists from SourceSpans to link children to their parents.
        This is authoritative for macro-generated members that are outside the 
        lexical span of their parent composite type.
        """
        logger.info("Assigning parent IDs from semantic member lists...")
        assigned_count = 0
        
        for file_uri, spans in file_span_data.items():
            for span in spans.values():
                # Only composite types have member lists
                if not span.member_ids:
                    continue
                
                # Resolve the parent's canonical ID (may be indexed or synthetic)
                parent_id = self.synthetic_id_to_index_id.get(span.id, span.id)
                if parent_id not in self.symbol_parser.symbols:
                    continue
                
                for member_id in span.member_ids:
                    # Resolve the member's canonical ID
                    canonical_member_id = self.synthetic_id_to_index_id.get(member_id, member_id)
                    member_sym = self.symbol_parser.symbols.get(canonical_member_id)
                    
                    if member_sym and not member_sym.parent_id:
                        # Safety check: avoid circular reference
                        if canonical_member_id != parent_id:
                            member_sym.parent_id = parent_id
                            assigned_count += 1
        
        logger.info(f"Successfully assigned {assigned_count} parent IDs from semantic member lists.")
        self.assigned_parent_by_member_list = assigned_count

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
            nonexitent_parent = None
            for ref in sym.references:
                # We had required ref.location == loc in the following checking, but this condition is unnecessary because
                # it's possible a symbol is defined in a different file from its parent, such as 
                # a class member is defined seperately with a scope like aclass::foo(){...}; 
                # This is very common when a macro is expanded in a file (where a class is generated), while the macro (class members) is defined in another file.
                # The macro case happens overwhelmingly in llvm project. 
                # Even with this condition removed, llvm still has lots of member symbols that don't have their parent symbol in ref containers.
                # We improve it further by matching the scope string to the qualified name of a class/struct.
                if ref.kind & 3 and not ref.kind & 4 and ref.container_id != '0000000000000000': 
                    if sym.parent_id: 
                        logger.error(f"Symbol {sym.id} already has a parent ID {sym.parent_id}, but hits a new one {ref.container_id}. Symbol:{sym.name}")
                        sym.parent_id = None 
                        break
                    #check if parent_id exists. It may not exist, e.g., Member Function Specialization, which has container of partial specialized class that does not have a symbol.
                    is_parent_existing = self.symbol_parser.symbols.get(ref.container_id)
                    if not is_parent_existing:
                        nonexitent_parent = ref.container_id
                        #logger.warning(f"Symbol {sym.id} has a non-existent parent ID {nonexitent_parent}. Symbol:{sym.name}")
                        continue
                    elif (sym.kind in {"Field", "StaticProperty", "EnumConstant", "InstanceMethod", "StaticMethod", "Constructor", "Destructor", "ConversionFunction"}) and \
                         (is_parent_existing.kind == "Namespace"):
                        # Namespace should not directly contain those member/field symbols. 
                        # They are contained because their parent structure is anonymous. We don't add its parent here, but by other approaches later.
                        break
        
                    if nonexitent_parent:
                        logger.warning(f"Symbol {sym.id} before had a non-existent parent ID {nonexitent_parent}. Now has parent ID {ref.container_id}. Symbol:{sym.name}")

                    sym.parent_id = ref.container_id 
                    self.assigned_parent_in_sym += 1
                    break    

        logger.info(f"Successfully assigned {self.assigned_parent_in_sym} parent IDs from reference container id.")
            
    def _infer_parent_ids_from_scope(self):
        """
        Uses the scope string of symbols to infer their parent_id.
        This is a fallback for when the container_id is not available in the index.
        """
        logger.info("Inferring parent IDs from scope strings...")
        
        # Build a map of qualified name -> symbol ID for scope-defining symbols
        scope_to_id = {}
        scope_defining_kinds = {"Namespace", "Class", "Struct", "Union", "Enum"} 
        
        for sym_id, sym in self.symbol_parser.symbols.items():
            if sym.kind in scope_defining_kinds:
                qualified_name = sym.scope + sym.name + sym.template_specialization_args + "::"
                # If there are duplicates fully qualified names, we set it to None to avoid ambiguity.
                if qualified_name not in scope_to_id:
                    scope_to_id[qualified_name] = sym_id
                else:
                    scope_to_id[qualified_name] = None


        inferred_count = 0
        for sym_id, sym in self.symbol_parser.symbols.items():
            # Only infer if parent_id is not already set and it has a scope
            if sym.parent_id is None and sym.scope:
                if parent_id := scope_to_id.get(sym.scope):
                    parent_kind = self.symbol_parser.symbols[parent_id].kind
                    if (sym.kind in {"Field", "StaticProperty", "EnumConstant", "InstanceMethod", "StaticMethod", "Constructor", "Destructor", "ConversionFunction"}) and \
                            (parent_kind == "Namespace"):
                        # Namespace should not directly contain those member/field symbols. 
                        # They are contained because their parent structure is anonymous. We don't add its parent here, but by other approaches later.
                        continue
                    sym.parent_id = parent_id
                    inferred_count += 1
        
        logger.info(f"Successfully inferred {inferred_count} parent IDs from scope strings.")
        self.assigned_parent_in_sym += inferred_count


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
            key = CompilationParser.make_symbol_key(
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
            key = CompilationParser.make_symbol_key(sym.name, sym.kind, loc.file_uri, loc.start_line, loc.start_column)
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


    def _assign_sym_parent_based_on_sourcespan_parent(self, after_adding_syn_symbols):
        # check if the matched source span's parent id has a matched clangd symbol id 
        assigned_sym_parent_with_span_parent = 0
        for sym_id, sym in self.symbol_parser.symbols.items():
            if sym.parent_id: continue
            syn_parent_id = self.sym_source_span_parent.get(sym_id)
            if not syn_parent_id:
                continue

            sym_parent_id = self.synthetic_id_to_index_id.get(syn_parent_id)
            # If we have added synthetic symbols, all synth id should be in symbol_parser.symbols
            # If not, that means, the synth id only exists as a span parent id, but not a span id.
            # This is probably because the parent is an anonymous namespace, and our compilation parser does not return it in current code. 
            # So this span parent id is never added to symbol_parser.symbols.
            # On the other hand, if a span parent id is a named namespace, it may have a clang sym id mapped.
            # Since it never exists as a span, it has no chance to be the object of the mapping operation.
            # As a result, the synthetic_id_to_index_id table never has the span parent id as a key.
            # TODO: Need to confirm the reason, and consider to add NAMESPACE spans in parser.
            if after_adding_syn_symbols:
                if syn_parent_id not in self.symbol_parser.symbols:
                    del self.sym_source_span_parent[sym_id]
                    continue
                # syn_parent_id is in symbol_parser.symbols, but if it's a namespace, it is not in the mapping table yet.
                # But since it is already in symbol_parser.symbols, let's assign it as the sym parent id
                if not sym_parent_id:
                    assert self.symbol_parser.symbols[syn_parent_id].kind == "Namespace", f"Synthetic id {syn_parent_id} (parent of {sym_id}) is not mapped to a symbol id."
                    sym_parent_id = syn_parent_id
                    self.synthetic_id_to_index_id[syn_parent_id] = syn_parent_id
            else:
                if not sym_parent_id:
                    continue
            # Update the parent id to the clangd symbol id
            sym.parent_id = sym_parent_id
            del self.sym_source_span_parent[sym_id]
            assigned_sym_parent_with_span_parent += 1

        logger.info(f"Assigned {assigned_sym_parent_with_span_parent} parent id based on source span parent. "
                    f"Remaining {len(self.sym_source_span_parent)} unassigned."
                    )    

    def _assign_parent_ids_lexically(self):
        """
        Assigns parent_id to symbols that don't have one, based on lexical scope. 
        The lexical scope is the source span extracted by compilation parser. 
        This pass is largely no longer useful, because almost all symbols that have parents should have been assigned parents in preceeding passes.
        We still keep this pass mainly as sanity checking and debugging purpose.
        Variable, namespace, are top-level symbols that don't have parents. (They can have namespace scope that we process separately.)
        We should skip those symbols in this pass. Other symbols without a parent id will pass through the code here.
        A structure may be top level, and may be contained in another structure. A member entity should always have parent id.
        This pass has two branches, one for symbols without definition or body_location; the other is for the rest (with body_location) which can match a span.
        The no-body branch may catch a few symbols as a final safety net. We should always analyze why they had not been assigned parent before.
        The other branch (for with-body symbols) should not be assigned parent. If anyone is assigned, it must be bug.
        """
        logger.info("Assigning parent IDs based on lexical scope for remaining symbols.")
        # Pass 0: Get span data (again, as it might have been deleted in previous step)
        file_span_data = self.compilation_manager.get_source_spans()

        for sym_id, sym in self.symbol_parser.symbols.items():            
            # We should skip the synthetic symbols that come from the spans. No need to find their original spans.
            if sym.name.startswith("c:"):
                continue 
                
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
            VARIABLE_KIND = {"Field", "StaticProperty", "EnumConstant", "Variable"}
            # This is for the functions that are not defined in the code, like constructor() = 0;
            # For this kind of functions, we ensure they don't have body definition.
            # Also for declarations of Struct and Class
            special_kinds = {"Constructor", "Destructor", "InstanceMethod", "ConversionFunction", "Struct", "Class"}
            if sym.kind in VARIABLE_KIND or (sym.kind in special_kinds and not sym.body_location):
                field_name = RelativeLocation(loc.start_line, loc.start_column, loc.end_line, loc.end_column)
                field_span = SourceSpan(sym.name, "Variable", sym.language, field_name, field_name, '', '')
                span_tree = file_span_data.get(loc.file_uri, {}) 
                container = self._find_innermost_container(span_tree, field_span)
                if container:
                    parent_synth_id = container.id
                    self.assigned_parent_no_span += 1

                else:
                    # Fallback for EnumConstants in anonymous enums via USR-bridging (sym.type)
                    if sym.kind == "EnumConstant" and sym.type:
                        # Clean USR (strip extra '$')
                        cleaned_usr = sym.type.replace('$', '')
                        usr_parent_id = CompilationParser.hash_usr_to_id(cleaned_usr)
                        if usr_parent_id in self.symbol_parser.symbols:
                            parent_synth_id = usr_parent_id # Direct match to indexed/synthetic ID
                            self.assigned_parent_no_span += 1
                    
                    if not parent_synth_id:
                        # Variables and no-body Structs/Classes defined at top level have no parent scope
                        if not sym.kind in {"Variable", "Struct", "Class"}:
                            logger.debug(f"Could not find container for no-body {sym.kind}:{sym.id} - {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}")
                        continue   
                #now we have the parent scope id in parent_synth_id

            else:
                # NOTE: This branch pass should always return 0 with-body span matching.
                # If there are some symbols matching spans in this branch, there must be something wrong in previous passes.
                # We keep this branch as a sanity checking, so as to know 

                # We skip following cases:
                # 1. TypeAlias: We handle it separately. They are not managed in SpanTrees.
                # 2. TODO: Using and NamespaceAlias: we don't support them yet.
                # 3. TODO: We don't parse Namespace SourceSpans in compilation parser, so no chance to match here.
                # 4. Function without definition, which is only a declaration that we don't care.
                if sym.kind in {"TypeAlias", "Using", "NamespaceAlias", "Namespace"} or (sym.kind in {"Function"} and not sym.definition):
                    continue

                span_tree = file_span_data.get(loc.file_uri, {})
                if not span_tree:
                    if sys.argv[0].endswith("clangd_graph_rag_builder.py"):
                        # When the graph is incrementally updated with clangd_graph_rag_updater.py (not built from scratch), it is normal that some files don't have span trees.
                        # The reason is, the symbol (and its file) is extended from the seed symbols, whose source file may not be parsed.
                        # We only log the debug message for the builder, when all the source files should be parsed, and have span trees.
                        logger.debug(f"Could not find span tree for file {loc.file_uri}, symbol {sym.name} {sym.id}")
                    continue

                # 1. Primary Lookup: Try to find the span by its USR-derived ID.
                # In the USR-based system, sym.id matches span.id exactly.
                span = span_tree.get(sym.id)
                
                # 2. Fallback Lookup: If ID lookup fails (e.g. Treesitter or USR divergence),
                # use the location-based coordinate key.
                if not span:
                    key = CompilationParser.make_symbol_key(sym.name, sym.kind, loc.file_uri, loc.start_line, loc.start_column)
                    # Iterate the tree values since the dict is now keyed by ID.
                    for s in span_tree.values():
                        if CompilationParser.make_symbol_key(s.name, s.kind, loc.file_uri, s.name_location.start_line, s.name_location.start_column) == key:
                            span = s
                            break

                if not span:
                    if sym.kind in {"Function"}:  
                       # We should not see any Function here. Clangd does not distinguish extern function declaration from definition.
                       # IN this case, although a function's has_definition is True, it does not have body, so there is SourceSpan to match.
                        continue        
                    else: 
                        logger.debug(f"Could not find SourceSpan for with-body sym {sym.kind}:{sym.id} - {sym.scope} - {sym.name} at {loc.file_uri}:{loc.start_line}")
                    continue

                parent_synth_id = span.parent_id
                if not parent_synth_id: # Top-level symbol.
                    continue

                # The parent id is not a valid symbol, skip it.
                if not parent_synth_id in self.symbol_parser.symbols:
                    continue
                # Finally we found a matched span for this symbol that also has a parent scope.
                self.assigned_parent_by_span += 1

            # Resolve the parent's ID.
            # 1. Try to find if the parent ID was anchored to a canonical Clangd ID.
            # 2. If not (e.g. parent is anonymous), use the raw synthetic ID.
            parent_id = self.synthetic_id_to_index_id.get(parent_synth_id, parent_synth_id)
            assert parent_id != sym.id, f"Found same parent id {parent_id} for {sym.kind} {sym.id} -- {sym.name} at {loc.file_uri}:{loc.start_line}:{loc.start_column}"

            sym.parent_id = parent_id

        logger.info(f"Found remaining symbols' parent with lexical scope: {self.assigned_parent_no_span} without body.")
        if self.assigned_parent_by_span:
            logger.error(f"Found {self.assigned_parent_by_span} with-body symbols assigned parent by span, but expected 0")

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
        type_alias_spans: Dict[str, TypeAliasSpan] = self.compilation_manager.get_type_alias_spans()

        if not type_alias_spans:
            logger.info("No TypeAlias spans found to enrich.")
            return

        # Create a copy of the type_alias_spans to allow modification (deletion of matched spans)
        unmatched_type_alias_spans = type_alias_spans.copy()

        synthetic_type_alias_symbols = {}
        
        # First Pass: Match existing TypeAlias symbols from clangd index
        for sym_id, sym in self.symbol_parser.symbols.items():
            if sym.kind != 'TypeAlias': continue

            if False:
                if sym_id == "80451E61B2364407": 
                    pass

            # Use sym.id (USR-derived) to look up a matching TypeAliasSpan
            matched_tas = unmatched_type_alias_spans.get(sym_id)
            if not matched_tas:
                logger.debug(f"Could not find matching TypeAliasSpan for TypeAlias symbol {sym_id} {sym.name} at {sym.definition.file_uri}:{sym.definition.start_line}:{sym.definition.start_column}")
                continue
            
            # Enrich existing Symbol
            sym.aliased_canonical_spelling = matched_tas.aliased_canonical_spelling
            sym.aliased_type_id = self.synthetic_id_to_index_id.get(matched_tas.aliased_type_id, matched_tas.aliased_type_id)
            sym.aliased_type_kind = matched_tas.aliased_type_kind
            sym.body_location = matched_tas.body_location
            sym.original_name = matched_tas.original_name
            sym.expanded_from_id = matched_tas.expanded_from_id

            # Same TypeAlias symbol may appear at different files. Ensure the body_location is in the same file
            sym.definition = Location.from_relative_location(matched_tas.name_location, matched_tas.file_uri)

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
        logger.info(f"Processed {len(type_alias_spans)} TypeAlias spans.")
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
            aliased_type_kind=tas.aliased_type_kind,
            original_name=tas.original_name,
            expanded_from_id=tas.expanded_from_id
        )
        return new_sym

    def get_matched_count(self) -> int:
        return self.matched_symbol_count + self.matched_typealias_count


    def get_assigned_count(self) -> int:
        return self.assigned_parent_count

    def _discover_macros(self):
        """
        Discovers macros from the compilation manager and injects them as new Symbol objects.
        """
        macro_spans = self.compilation_manager.get_macro_spans()
        if not macro_spans:
            logger.info("No macro spans found to discover.")
            return

        new_macro_symbols = {}
        for span_id, span in macro_spans.items():
            # If the macro already exists (unlikely given synthetic IDs based on location), skip it.
            if span_id in self.symbol_parser.symbols:
                logger.warning(f"Macro {span.name} {span.file_uri} already exists with ID {span_id}, skipping.")
                continue

            loc = Location.from_relative_location(span.name_location, file_uri=span.file_uri)
            
            new_sym = Symbol(
                id=span.id,
                name=span.name,
                kind='Macro',
                declaration=loc,
                definition=Location.from_relative_location(span.body_location, file_uri=span.file_uri),
                references=[],
                scope="", # Macros are global in the preprocessor sense
                language=span.lang,
                body_location=span.body_location,
                is_macro_function_like=span.is_function_like,
                macro_definition=span.macro_definition
            )
            new_macro_symbols[span.id] = new_sym

        self.symbol_parser.symbols.update(new_macro_symbols)
        logger.info(f"Discovered and injected {len(new_macro_symbols)} new macro symbols.")

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
            if node.kind not in self.VARIABLE_KIND and self._span_is_within(span, node):
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
            parent_id=parent_id,
            original_name=span.original_name,
            expanded_from_id=span.expanded_from_id
        )
