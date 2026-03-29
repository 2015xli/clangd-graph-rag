#!/usr/bin/env python3
"""
Caching logic for compilation results.
"""

import os
import pickle
import hashlib
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

class CacheManager:
    """Handles finding, validating, loading, and saving cache files."""
    def __init__(self, cache_directory: str, project_name: str):
        self.cache_directory = cache_directory
        self.project_name = project_name
        os.makedirs(self.cache_directory, exist_ok=True)

    def _construct_git_filename(self, new_commit: str, old_commit: str = None) -> str:
        """Constructs a cache filename based on commit hashes."""
        new_short = new_commit[:8]
        old_short = old_commit[:8] if old_commit else ''
        return f"parsing_{self.project_name}_hash_{new_short}_{old_short}.pkl"

    def _construct_mtime_filename(self, latest_mtime: float, oldest_mtime: float) -> str:
        """Constructs a cache filename based on modification times."""
        latest_hex = f"{int(latest_mtime):08x}"
        oldest_hex = f"{int(oldest_mtime):08x}"
        return f"parsing_{self.project_name}_time_{latest_hex}_{oldest_hex}.pkl"

    def find_and_load_git_cache(self, new_commit: str, old_commit: str = None) -> Optional[Dict[str, Any]]:
        """Finds and loads a cache based on Git commit hashes."""
        filename = self._construct_git_filename(new_commit, old_commit)
        cache_path = os.path.join(self.cache_directory, filename)

        if not os.path.exists(cache_path):
            return None

        try:
            with open(cache_path, "rb") as f:
                cached_data = pickle.load(f)
            
            # Deep validation
            if (cached_data.get("new_commit") == new_commit and
                cached_data.get("old_commit") == old_commit):
                logger.info(f"Found and validated Git-based cache: {filename}")
                return {
                    "source_spans": cached_data.get("source_spans", {}),
                    "include_relations": cached_data.get("include_relations", set()),
                    "static_call_relations": cached_data.get("static_call_relations", set()),
                    "type_alias_spans": cached_data.get("type_alias_spans", {}),
                    "macro_spans": cached_data.get("macro_spans", {})
                }
            else:
                logger.warning(f"Cache file {filename} has mismatched full commit hashes. Ignoring.")
                return None
        except (pickle.UnpicklingError, EOFError, KeyError, AttributeError, ModuleNotFoundError) as e:
            logger.warning(f"Cache file {cache_path} is incompatible or corrupted: {e}. Ignoring.")
            return None

    def find_and_load_mtime_cache(self, file_list: List[str]) -> Optional[Dict[str, Any]]:
        """Finds and loads a cache based on file modification times and content hash."""
        if not file_list:
            return None

        try:
            mtimes = [os.path.getmtime(f) for f in file_list]
            latest_mtime = max(mtimes)
            oldest_mtime = min(mtimes)
        except FileNotFoundError:
            return None # One of the files doesn't exist

        filename = self._construct_mtime_filename(latest_mtime, oldest_mtime)
        cache_path = os.path.join(self.cache_directory, filename)

        if not os.path.exists(cache_path):
            return None

        try:
            with open(cache_path, "rb") as f:
                cached_data = pickle.load(f)

            # Deep validation: check if the list of files is identical
            current_file_hash = hashlib.sha256("".join(sorted(file_list)).encode()).hexdigest()
            if cached_data.get("file_list_hash") == current_file_hash:
                logger.info(f"Found and validated mtime-based cache: {filename}")
                return {
                    "source_spans": cached_data.get("source_spans", {}),
                    "include_relations": cached_data.get("include_relations", set()),
                    "static_call_relations": cached_data.get("static_call_relations", set()),
                    "type_alias_spans": cached_data.get("type_alias_spans", {}),
                    "macro_spans": cached_data.get("macro_spans", {})
                }
            else:
                logger.warning(f"Cache file {filename} has mismatched file list hash. Ignoring.")
                return None
        except (pickle.UnpicklingError, EOFError, KeyError, AttributeError, ModuleNotFoundError) as e:
            logger.warning(f"Cache file {cache_path} is incompatible or corrupted: {e}. Ignoring.")
            return None

    def save_git_cache(self, data: Dict[str, Any], new_commit: str, old_commit: str = None):
        """Saves data to a Git-based cache file."""
        filename = self._construct_git_filename(new_commit, old_commit)
        cache_path = os.path.join(self.cache_directory, filename)
        
        cache_obj = {
            "source_spans": data["source_spans"],
            "include_relations": data["include_relations"],
            "static_call_relations": data["static_call_relations"],
            "type_alias_spans": data["type_alias_spans"],
            "macro_spans": data["macro_spans"],
            "new_commit": new_commit,
            "old_commit": old_commit
        }
        logger.info(f"Saving Git-based cache to: {filename}")
        with open(cache_path, "wb") as f:
            pickle.dump(cache_obj, f)

    def save_mtime_cache(self, data: Dict[str, Any], file_list: List[str]):
        """Saves data to an mtime-based cache file."""
        mtimes = [os.path.getmtime(f) for f in file_list]
        filename = self._construct_mtime_filename(max(mtimes), min(mtimes))
        cache_path = os.path.join(self.cache_directory, filename)
        
        file_list_hash = hashlib.sha256("".join(sorted(file_list)).encode()).hexdigest()
        
        cache_obj = {
            "source_spans": data["source_spans"],
            "include_relations": data["include_relations"],
            "static_call_relations": data["static_call_relations"],
            "type_alias_spans": data["type_alias_spans"],
            "macro_spans": data["macro_spans"],
            "file_list_hash": file_list_hash
        }
        logger.info(f"Saving mtime-based cache to: {filename}")
        with open(cache_path, "wb") as f:
            pickle.dump(cache_obj, f)
