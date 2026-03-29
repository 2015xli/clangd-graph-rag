#!/usr/bin/env python3
"""
General utility functions for the project.
"""
import hashlib
import os   

class FileExtensions:
    """Grouped file extension constants for C/C++ projects."""
    C_SOURCE = ('.c',)
    CPP_SOURCE = ('.cpp', '.cc', '.cxx')
    CPP20_MODULE = ('.cppm', '.ccm', '.cxxm', '.c++m')
    
    ALL_CPP_SOURCE = CPP_SOURCE + CPP20_MODULE
    ALL_SOURCE = C_SOURCE + ALL_CPP_SOURCE

    VOLATILE_HEADER = ('.inc', '.def')
    C_HEADER = ('.h',) + VOLATILE_HEADER
    CPP_HEADER = ('.hpp', '.hh', '.hxx', '.h++') + C_HEADER
    
    ALL_HEADER = CPP_HEADER
    ALL_C_CPP = ALL_SOURCE + ALL_HEADER

def align_string(string: str, width: int = 45, direction: str = 'right', fillchar: str = ' ') -> str:
    """
    Aligns a string within a specified width.
    Primarily used for consistent formatting in progress bars and logs.
    """
    if direction == 'left':
        return string.ljust(width, fillchar)
    elif direction == 'right':
        return string.rjust(width, fillchar)
    else:
        return string.center(width)

def hash_usr_to_id(usr: str) -> str:
    """
    Replicates clangd's ID generation by taking the first 8 bytes of
    the SHA1 hash of the USR. Returns a 16-char uppercase hex string.
    """
    sha1_hash = hashlib.sha1(usr.encode()).digest()
    return sha1_hash[:8].hex().upper()

def make_symbol_key(name: str, kind: str, file_uri: str, line: int, col: int) -> str:
    """
    Generates a deterministic location-based key for a symbol.
    Format: kind::symbol name::file URI:line:col
    """
    return f"{kind}::{name}::{file_uri}:{line}:{col}"

def make_synthetic_id(key: str) -> str:
    """
    Generates a deterministic MD5 hash for a given key string.
    """
    return hashlib.md5(key.encode()).hexdigest()


def get_language(file_name: str) -> str:
    ext = os.path.splitext(file_name)[1].lower()
    if ext in FileExtensions.ALL_CPP_SOURCE or ext in FileExtensions.CPP_HEADER: return "Cpp"
    if ext in FileExtensions.C_SOURCE or ext in FileExtensions.C_HEADER: return "C"
    return "Unknown"