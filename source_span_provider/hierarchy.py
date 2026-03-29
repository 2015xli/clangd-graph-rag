import logging
import gc, sys
from typing import Dict, Set
from urllib.parse import urlparse, unquote

from clangd_index_yaml_parser import Location, RelativeLocation
from compilation_ops import SourceSpan, CompilationParser

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class HierarchyMixin:
    """Provides methods for establishing and propagating symbol parent-child relationships."""

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
                if not span_tree:
                    if sys.argv[0].endswith("clangd_graph_rag_builder.py"):
                        # When the graph is incrementally updated with clangd_graph_rag_updater.py (not built from scratch), it is normal that some files don't have span trees.
                        # The reason is, the symbol (and its file) is extended from the seed symbols, whose source file may not be parsed.
                        # We only log the debug message for the builder, when all the source files should be parsed, and have span trees.
                        logger.debug(f"Could not find span tree for file {loc.file_uri}, no-body symbol {sym.name} {sym.id}")
                    continue

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
                        logger.debug(f"Could not find span tree for file {loc.file_uri}, with-body symbol {sym.name} {sym.id}")
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
