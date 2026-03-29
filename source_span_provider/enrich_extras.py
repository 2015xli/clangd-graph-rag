import logging
import sys
from typing import Dict

from clangd_index_yaml_parser import Symbol, Location, Reference
from compilation_engine import TypeAliasSpan

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class EnrichExtrasMixin:
    """Provides methods for specialized enrichment passes (TypeAlias, Macros, Static Calls)."""

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

        unmatched_type_alias_spans = type_alias_spans.copy()
        synthetic_type_alias_symbols = {}
        
        # First Pass: Match existing TypeAlias symbols from clangd index
        for sym_id, sym in self.symbol_parser.symbols.items():
            if sym.kind != 'TypeAlias': continue

            # Use sym.id (USR-derived) to look up a matching TypeAliasSpan
            matched_tas = unmatched_type_alias_spans.get(sym_id)
            if not matched_tas:
                if sys.argv[0].endswith("clangd_graph_rag_builder.py"):
                    logger.debug(f"Could not find matching TypeAliasSpan for TypeAlias symbol {sym_id} {sym.name} at {sym.definition.file_uri}:{sym.definition.start_line}:{sym.definition.start_column}")
                continue
            
            # Enrich existing Symbol
            sym.aliased_canonical_spelling = matched_tas.aliased_canonical_spelling
            sym.aliased_type_id = self.synthetic_id_to_index_id.get(matched_tas.aliased_type_id, matched_tas.aliased_type_id)
            sym.aliased_type_kind = matched_tas.aliased_type_kind
            sym.body_location = matched_tas.body_location
            sym.original_name = matched_tas.original_name
            sym.expanded_from_id = matched_tas.expanded_from_id

            sym.definition = Location.from_relative_location(matched_tas.name_location, matched_tas.file_uri)

            if sym.parent_id is None and matched_tas.parent_id:
                self.assigned_parent_matched_alias += 1
                sym.parent_id = self.synthetic_id_to_index_id.get(matched_tas.parent_id, matched_tas.parent_id)
            if sym.scope is None:
                sym.scope = matched_tas.scope

            self.synthetic_id_to_index_id[matched_tas.id] = sym_id
            del unmatched_type_alias_spans[sym_id]
            self.matched_typealias_count += 1

        # Second Pass: Create Synthetic Symbols for Unmatched TypeAliasSpans
        for unmatched_tas in unmatched_type_alias_spans.values():
            new_sym = self._create_synthetic_type_alias_symbol(unmatched_tas)
            synthetic_type_alias_symbols[new_sym.id] = new_sym
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
                scope="", 
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
        Injects static call relations as synthetic references into the Symbol objects.
        """
        static_calls = self.compilation_manager.get_static_call_relations()
        if not static_calls:
            logger.info("No static call relations found to enrich.")
            return

        injected_ref_count = 0
        for caller_usr, callee_usr in static_calls:
            callee_symbol = self.symbol_parser.symbols.get(callee_usr)

            if callee_symbol:
                dummy_location = Location(file_uri='', start_line=0, start_column=0, end_line=0, end_column=0)
                new_ref = Reference(
                    kind=28,
                    location=dummy_location,
                    container_id=caller_usr
                )
                callee_symbol.references.append(new_ref)
                injected_ref_count += 1

        logger.info(f"Successfully injected {injected_ref_count} static call references.")
