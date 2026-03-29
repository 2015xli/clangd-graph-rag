#!/usr/bin/env python3
"""
Constants and registry of Clang node kinds for the compilation engine.
"""
import clang.cindex

# --- Node Kind Groupings ---
NODE_KIND_VARIABLES = {
    clang.cindex.CursorKind.VAR_DECL.name
}

NODE_KIND_FUNCTIONS = {
    clang.cindex.CursorKind.FUNCTION_DECL.name,
    clang.cindex.CursorKind.FUNCTION_TEMPLATE.name
}

NODE_KIND_CONSTRUCTOR = {
    clang.cindex.CursorKind.CONSTRUCTOR.name
}

NODE_KIND_DESTRUCTOR = {
    clang.cindex.CursorKind.DESTRUCTOR.name
}

NODE_KIND_CONVERSION_FUNCTION = {
    clang.cindex.CursorKind.CONVERSION_FUNCTION.name
}

NODE_KIND_CXX_METHOD = {
    clang.cindex.CursorKind.CXX_METHOD.name
}

NODE_KIND_METHODS = NODE_KIND_CXX_METHOD | NODE_KIND_CONSTRUCTOR | NODE_KIND_DESTRUCTOR | NODE_KIND_CONVERSION_FUNCTION
NODE_KIND_CALLERS = NODE_KIND_FUNCTIONS | NODE_KIND_METHODS

NODE_KIND_UNION = {
    clang.cindex.CursorKind.UNION_DECL.name
}

NODE_KIND_ENUM = {
    clang.cindex.CursorKind.ENUM_DECL.name
}

NODE_KIND_STRUCT = {
    clang.cindex.CursorKind.STRUCT_DECL.name
}

NODE_KIND_CLASSES = {
    clang.cindex.CursorKind.CLASS_DECL.name,
    clang.cindex.CursorKind.CLASS_TEMPLATE.name,
    clang.cindex.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION.name
}

NODE_KIND_FOR_COMPOSITE_TYPES = NODE_KIND_UNION | NODE_KIND_ENUM | NODE_KIND_STRUCT | NODE_KIND_CLASSES
NODE_KIND_FOR_BODY_SPANS = NODE_KIND_FUNCTIONS | NODE_KIND_METHODS | NODE_KIND_FOR_COMPOSITE_TYPES | NODE_KIND_VARIABLES

NODE_KIND_NAMESPACE = {
    clang.cindex.CursorKind.NAMESPACE.name
}

NODE_KIND_FOR_SCOPES = NODE_KIND_NAMESPACE | NODE_KIND_FOR_COMPOSITE_TYPES

NODE_KIND_TYPE_ALIASES = {
    clang.cindex.CursorKind.TYPE_ALIAS_TEMPLATE_DECL.name,
    clang.cindex.CursorKind.TYPE_ALIAS_DECL.name,
    clang.cindex.CursorKind.TYPEDEF_DECL.name
}

NODE_KIND_FOR_USER_DEFINED_TYPES = NODE_KIND_FOR_COMPOSITE_TYPES | NODE_KIND_TYPE_ALIASES

# Kinds that represent semantic members of a composite type
NODE_KIND_MEMBERS = NODE_KIND_CALLERS | {
    clang.cindex.CursorKind.FIELD_DECL.name,
    clang.cindex.CursorKind.ENUM_CONSTANT_DECL.name,
    clang.cindex.CursorKind.VAR_DECL.name, # For static members
} | NODE_KIND_FOR_USER_DEFINED_TYPES # For nested types
