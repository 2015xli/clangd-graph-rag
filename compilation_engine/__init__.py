#!/usr/bin/env python3
"""
Compilation engine for parsing C/C++ source code and extracting semantic metadata.
"""

from .manager import CompilationManager
from .worker import SourceSpan, MacroSpan, TypeAliasSpan, IncludeRelation
from utils import FileExtensions

__all__ = [
    'CompilationManager',
    'SourceSpan',
    'MacroSpan',
    'TypeAliasSpan',
    'IncludeRelation',
    'FileExtensions'
]
