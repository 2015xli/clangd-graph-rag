#!/usr/bin/env python3
"""
This module provides the SummaryCacheManager class, which manages the data and
persistence for the RAG summary cache.

It can also be run as a standalone script to backup summaries from Neo4j to a
cache file, or restore summaries from the file back to Neo4j.
"""

import os
import logging
import json
import argparse
import sys
from typing import Optional, Dict, Any
from collections import defaultdict

from neo4j_manager import Neo4jManager
from log_manager import init_logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class SummaryCacheManager:
    """
    Manages the data and persistence of the summary cache, using a single,
    evolving cache dictionary.
    """

    DEFAULT_CACHE_FILENAME = "summary_backup.json"

    def __init__(self, project_path: Optional[str]):
        """
        Initializes the SummaryCacheManager.
        """
        self.cache: Dict[str, Dict[str, Any]] = defaultdict(lambda: defaultdict(dict))
        self.runtime_status: Dict[str, Dict[str, Any]] = defaultdict(dict)
        
        self.project_path = project_path
        self.cache_path: Optional[str] = None
        if project_path:
            self.cache_path = os.path.join(self.project_path, ".cache", self.DEFAULT_CACHE_FILENAME)

        self.started_with_empty_cache = False
        
        logger.info("SummaryCacheManager initialized.")

    def load(self):
        """Loads the cache from the JSON file into the main cache dictionary."""
        if not self.cache_path or not os.path.exists(self.cache_path):
            logger.warning(f"Summary cache file not found at {self.cache_path}. Starting with empty cache.")
            self.started_with_empty_cache = True
            return

        try:
            with open(self.cache_path, 'r') as f:
                loaded_data = json.load(f)
                # Convert to defaultdict for consistent behavior
                for key, inner_dict in loaded_data.items():
                    self.cache[key].update(inner_dict)

            total_entries = sum(len(v) for v in self.cache.values())
            logger.info(f"Loaded {total_entries} entries from summary cache file {self.cache_path}.")
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Failed to read or parse summary cache file {self.cache_path}: {e}")

    def save(self, mode: str, neo4j_mgr: Neo4jManager, is_intermediate: bool = False):
        """
        Finalizes and saves the cache to disk, with backup rotation and sanity checks.
        """
        if not self.cache_path:
            logger.error("Cannot save cache: cache path is not configured.")
            return

        if is_intermediate:
            logger.debug("Performing intermediate cache save to temporary file...")
            self._write_cache_to_file(path_override=self.cache_path + ".tmp")
            return

        logger.info(f"Finalizing cache in {mode} mode...")

        # This is a special case for healing an empty cache. It's a full rebuild,
        # so we rotate first and then write directly to the main file.
        if self.started_with_empty_cache and mode == 'updater':
            logger.info("Run started with an empty cache; performing full backup from graph to ensure cache is complete.")
            self._rotate_backups()
            self.backup_db_to_file(neo4j_mgr)
            return

        # --- Standard Final Save Logic ---
        if mode == "builder":
            logger.info("Pruning dormant entries from cache for builder mode...")
            pruned_cache = defaultdict(dict)
            for label, entries in self.cache.items():
                for key, data in entries.items():
                    if self.runtime_status.get(label, {}).get(key, {}).get('visited'):
                        pruned_cache[label][key] = data
            self.cache = pruned_cache
            logger.info(f"Cache pruned. New total entries: {sum(len(v) for v in self.cache.values())}.")

        # Write the final version of the in-memory cache to the temporary file
        tmp_path = self.cache_path + ".tmp"
        self._write_cache_to_file(path_override=tmp_path)

        # Attempt to promote the temporary file to become the new main cache
        self._promote_tmp_cache(tmp_path)

        logger.info("Cache finalization complete.")

    def _promote_tmp_cache(self, tmp_path: str):
        """
        Performs a sanity check on the temporary cache file and, if it passes,
        rotates the old backups and promotes the temporary file to the main cache.
        """
        if not os.path.exists(tmp_path):
            logger.error(f"Promotion failed: Temporary cache file {tmp_path} does not exist.")
            return

        try:
            with open(tmp_path, 'r') as f:
                new_cache_data = json.load(f)
            new_entry_count = sum(len(v) for v in new_cache_data.values())
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Promotion failed: Could not read or parse temporary cache file {tmp_path}: {e}")
            return

        old_entry_count = 0
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, 'r') as f:
                    old_cache_data = json.load(f)
                old_entry_count = sum(len(v) for v in old_cache_data.values())
            except (IOError, json.JSONDecodeError):
                logger.warning(f"Could not read old cache file {self.cache_path} for comparison.")
        
        # Sanity Check: Proceed if new cache is at least 95% of the old one's size.
        # The check is skipped if the old cache was very small, to allow for initial growth.
        if old_entry_count > 50 and new_entry_count < old_entry_count * 0.95:
            logger.critical(
                f"PROMOTION ABORTED: Sanity check failed. New cache size ({new_entry_count}) "
                f"is less than 95% of the old cache size ({old_entry_count}). "
                f"The temporary cache file has been left at {tmp_path} for inspection."
            )
            return

        # Promotion: If the check passes, rotate backups and rename the temp file.
        logger.info("Sanity check passed. Promoting temporary cache to main cache file.")
        self._rotate_backups()
        try:
            os.rename(tmp_path, self.cache_path)
            logger.info(f"Successfully promoted {tmp_path} to {self.cache_path}")
        except OSError as e:
            logger.error(f"Failed to promote temporary cache file: {e}")

    def _rotate_backups(self):
        """
        Performs a 2-level backup rotation of the cache file.
        summary.json -> summary.json.bak.1
        summary.json.bak.1 -> summary.json.bak.2
        """
        if not self.cache_path: return

        bak1_path = self.cache_path + ".bak.1"
        bak2_path = self.cache_path + ".bak.2"

        try:
            if os.path.exists(bak2_path):
                os.remove(bak2_path)
                logger.debug(f"Removed old backup: {bak2_path}")

            if os.path.exists(bak1_path):
                os.rename(bak1_path, bak2_path)
                logger.debug(f"Rotated backup {bak1_path} -> {bak2_path}")

            if os.path.exists(self.cache_path):
                os.rename(self.cache_path, bak1_path)
                logger.info(f"Backed up current cache {self.cache_path} -> {bak1_path}")

        except OSError as e:
            logger.error(f"Error during cache backup rotation: {e}", exc_info=True)

    def _write_cache_to_file(self, path_override: Optional[str] = None):
        """Writes the current in-memory cache to the specified JSON file."""
        path = path_override or self.cache_path
        if not path:
            logger.error("Cannot save summary cache: cache path is not configured.")
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                json.dump(self.cache, f, indent=2)
            total_entries = sum(len(v) for v in self.cache.values())
            logger.info(f"Successfully wrote {total_entries} entries to cache file {path}.")
        except IOError as e:
            logger.error(f"Failed to write to summary cache file {path}: {e}")

    def get_cache_entry(self, label: str, key: str) -> Optional[Dict[str, Any]]:
        """Provides read-only access to a single entry in the cache."""
        return self.cache.get(label, {}).get(key)

    def update_cache_entry(self, label: str, key: str, data: Dict[str, Any]):
        """Updates or adds an entry in the cache. Called by the orchestrator."""
        self.cache[label][key].update(data)

    def set_runtime_status(self, label: str, key: str, status: str):
        """Sets the runtime status of a node for the current run."""
        if key not in self.runtime_status[label]:
            self.runtime_status[label][key] = {'visited': True}
        
        if status == 'code_summary_changed':
            self.runtime_status[label][key]['code_summary_changed'] = True
        elif status == 'summary_changed':
            self.runtime_status[label][key]['summary_changed'] = True

    def get_runtime_status(self, label: str, key: str) -> Dict[str, Any]:
        """Gets the runtime status of a node."""
        return self.runtime_status.get(label, {}).get(key, {})

    def configure_project_path(self, neo4j_mgr: Neo4jManager):
        """
        Discovers the project path from the Neo4j database and configures
        the cache path.
        """
        if self.project_path: return
        query = "MATCH (p:PROJECT) RETURN p.path AS path"
        result = neo4j_mgr.execute_read_query(query)
        project_path = result[0].get('path') if result and len(result) == 1 else None
        
        if not project_path:
            raise ValueError("Could not determine project path from Neo4j. A PROJECT node with a 'path' property must exist.")
        
        self.project_path = project_path
        self.cache_path = os.path.join(self.project_path, ".cache", self.DEFAULT_CACHE_FILENAME)
        logger.info(f"Discovered and configured project path: {self.project_path}")

    def backup_db_to_file(self, neo4j_mgr: Neo4jManager, batch_size: int = 10000):
        """Rebuilds the cache from scratch by querying all summaries from Neo4j."""
        logger.info("Starting full summary backup from Neo4j to rebuild cache...")
        
        new_cache_data = defaultdict(dict)
        query_configs = {
            "id_based_full": {
                "labels": ["FUNCTION", "METHOD"],
                "query": "MATCH (n:{label}) WHERE n.summary IS NOT NULL OR n.codeSummary IS NOT NULL RETURN n.id AS identifier, n.code_hash as code_hash, n.codeSummary AS codeSummary, n.summary AS summary ORDER BY n.id SKIP $skip LIMIT $limit"
            },
            "id_based_simple": {
                "labels": ["NAMESPACE", "CLASS_STRUCTURE"],
                "query": "MATCH (n:{label}) WHERE n.summary IS NOT NULL RETURN n.id AS identifier, n.summary AS summary ORDER BY n.id SKIP $skip LIMIT $limit"
            },
            "path_based": {
                "labels": ["FILE", "FOLDER", "PROJECT"],
                "query": "MATCH (n:{label}) WHERE n.summary IS NOT NULL RETURN n.path AS identifier, n.summary AS summary ORDER BY n.path SKIP $skip LIMIT $limit"
            }
        }

        for config in query_configs.values():
            for label in config["labels"]:
                logger.info(f"Backing up summaries for label: {label}...")
                skip = 0
                while True:
                    results = neo4j_mgr.execute_read_query(config["query"].format(label=label), {"skip": skip, "limit": batch_size})
                    if not results: break
                    
                    for record in results:
                        identifier = record.get('identifier')
                        if not identifier: continue
                        entry = {k: v for k, v in record.items() if k != 'identifier' and v is not None}
                        new_cache_data[label][identifier] = entry
                    
                    logger.info(f"  Fetched {len(new_cache_data[label])} records for {label} so far...")
                    skip += batch_size

        self.cache = new_cache_data
        self._write_cache_to_file()
        logger.info("Full summary backup from Neo4j complete.")

    def restore_db_from_file(self, neo4j_mgr: Neo4jManager, batch_size: int = 2000):
        """
        Restores summaries from the cache file to the Neo4j graph.
        """
        logger.info("Restoring summaries from cache file to Neo4j...")
        self.load()
        if not self.cache:
            logger.warning("Cache is empty. Nothing to restore.")
            return

        for label, entries in self.cache.items():
            if not entries: continue
            
            logger.info(f"Restoring {len(entries)} summaries for label: {label}...")
            
            data_list = []
            identifier_key = "path" if label in ["FILE", "FOLDER", "PROJECT"] else "id"

            for identifier, properties in entries.items():
                item = {identifier_key: identifier, **properties}
                data_list.append(item)

            if not data_list: continue

            sample_props = data_list[0]
            set_clauses = [f"n.{prop} = d.{prop}" for prop in sample_props if prop != identifier_key]
            
            if not set_clauses:
                logger.warning(f"No properties to restore for label {label}. Skipping.")
                continue

            query = f"""
            UNWIND $data AS d
            MATCH (n:{label} {{{identifier_key}: d.{identifier_key}}})
            SET {', '.join(set_clauses)}
            """
            
            for i in range(0, len(data_list), batch_size):
                batch = data_list[i:i + batch_size]
                try:
                    counters = neo4j_mgr.execute_autocommit_query(query, params={"data": batch})
                    logger.info(f"  Processed batch for {label}. Properties set: {counters.properties_set}")
                except Exception as e:
                    logger.error(f"An error occurred during batch restore for label {label}: {e}")
                    logger.error(f"Failed query: {query}")
                    logger.error(f"Failed with batch (first item): {batch[0] if batch else 'N/A'}")

        logger.info("Restore from cache file to Neo4j graph complete.")


if __name__ == "__main__":
    init_logging()

    parser = argparse.ArgumentParser(description="A tool for managing the RAG summary cache.")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available commands")
    
    parser_backup = subparsers.add_parser("backup", help="Backup summaries from Neo4j graph to the cache file.")
    parser_backup.add_argument("--batch-size", type=int, default=10000, help="Number of records per query.")
    
    parser_restore = subparsers.add_parser("restore", help="Restore summaries from the cache file to the Neo4j graph.")
    parser_restore.add_argument("--batch-size", type=int, default=2000, help="Number of records per transaction.")

    args = parser.parse_args()

    try:
        with Neo4jManager() as neo4j_mgr:
            if not neo4j_mgr.check_connection():
                logger.critical("Could not connect to Neo4j.")
                sys.exit(1)
            
            # Instantiate manager without a project path; it will be discovered.
            cache_manager = SummaryCacheManager(project_path=None)
            cache_manager.configure_project_path(neo4j_mgr)

            if args.command == "backup":
                cache_manager.backup_db_to_file(neo4j_mgr, batch_size=args.batch_size)
            elif args.command == "restore":
                cache_manager.restore_db_from_file(neo4j_mgr, batch_size=args.batch_size)

    except Exception as e:
        logger.critical(f"An unhandled error occurred: {e}", exc_info=True)
        sys.exit(1)
