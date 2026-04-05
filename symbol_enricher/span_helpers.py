import logging
from urllib.parse import urlparse, unquote
from typing import Optional

from symbol_parser import Symbol, Location
from source_parser import SourceSpan

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class HelpersMixin:
    """Provides utility methods for filtering, geometric checks, and symbol creation."""
    VARIABLE_KIND = {"Field", "StaticProperty", "EnumConstant", "Variable"}

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
            expanded_from_id=span.expanded_from_id,
            primary_template_id=span.primary_template_id,
            template_specialization_args=span.template_specialization_args or "",
            is_synthetic=span.is_synthetic
        )
