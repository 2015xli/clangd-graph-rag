#!/usr/bin/env python3
"""
General utility functions for the project.
"""
import hashlib
import os   
import pickle
import logging

logger = logging.getLogger(__name__)

class CompatibilityUnpickler(pickle.Unpickler):
    """
    A custom Unpickler that handles module renames for backward compatibility.
    If a class is found under an old module name, it is redirected to the new one.
    """
    def __init__(self, file, *args, **kwargs):
        super().__init__(file, *args, **kwargs)
        self.migrated = False

    def find_class(self, module, name):
        # Mapping of old legacy module names to the current 'source_parser' package
        renamed_modules = {
            "compilation_parser": "source_parser",
            "compilation_manager": "source_parser",
            "compilation_ops": "source_parser",
            "compilation_engine": "source_parser",
        }
        
        for old_name, new_name in renamed_modules.items():
            if module == old_name or module.startswith(f"{old_name}."):
                new_module = module.replace(old_name, new_name, 1)
                logger.warning(f"Redirecting pickle class: {module}.{name} -> {new_module}.{name}")
                module = new_module
                self.migrated = True
                break
                
        return super().find_class(module, name)

def safe_pickle_load(file_path: str):
    """
    Loads a pickle file with module name compatibility and automatic migration.
    If redirection occurred during loading, the file is overwritten with the
    updated module names.
    """
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, 'rb') as f:
            unpickler = CompatibilityUnpickler(f)
            data = unpickler.load()
        
        if unpickler.migrated:
            logger.warning(f"Migrating legacy pickle cache '{os.path.basename(file_path)}' to current module structure...")
            try:
                with open(file_path, 'wb') as f:
                    pickle.dump(data, f)
                logger.info(f"Successfully migrated '{os.path.basename(file_path)}'.")
            except Exception as e:
                logger.error(f"Failed to save migrated cache '{file_path}': {e}")
                
        return data
    except Exception as e:
        logger.error(f"Error loading pickle file {file_path}: {e}")
        return None

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