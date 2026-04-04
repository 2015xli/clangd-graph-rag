#!/usr/bin/env python3
"""
CLI entry point for the summarization engine cache management.
Allows backing up and restoring summaries via:
python3 -m summary_engine [command] [args]
"""

import argparse
import sys
import logging
from .node_cache import SummaryCacheManager
from neo4j_manager import Neo4jManager
from log_manager import init_logging

logger = logging.getLogger(__name__)

def main():
    # Initialize logging with standardized formatting
    init_logging()

    parser = argparse.ArgumentParser(description="A tool for managing the RAG summary cache.")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available commands")
    
    # Backup Command
    parser_backup = subparsers.add_parser("backup", help="Backup summaries from Neo4j graph to the cache file.")
    parser_backup.add_argument("--batch-size", type=int, default=10000, help="Number of records per query.")
    
    # Restore Command
    parser_restore = subparsers.add_parser("restore", help="Restore summaries from the cache file to the Neo4j graph.")
    parser_restore.add_argument("--batch-size", type=int, default=2000, help="Number of records per transaction.")

    # Clean Fakes Command
    parser_clean = subparsers.add_parser("clean-fakes", help="Surgically remove fake summaries from BOTH Neo4j and L1 cache.")
    parser_clean_cache = subparsers.add_parser("clean-fake-cache", help="Surgically remove fake summaries from L1 cache only.")

    args = parser.parse_args()

    try:
        # We initialize the manager without a path first, as we'll discover it from the DB
        cache_manager = SummaryCacheManager(project_path=None)

        with Neo4jManager() as neo4j_mgr:
            if not neo4j_mgr.check_connection():
                sys.exit(1)

            # Discover and configure the cache path based on the PROJECT node in the database
            cache_manager.configure_project_path(neo4j_mgr)

            if args.command == "backup":
                cache_manager.backup_db_to_file(neo4j_mgr, batch_size=args.batch_size)
            elif args.command == "restore":
                cache_manager.restore_db_from_file(neo4j_mgr, batch_size=args.batch_size)
            elif args.command == "clean-fakes" or args.command == "clean-fake-cache":
                logger.info("Starting cleanup of fake summaries...")
                if args.command == "clean-fakes":
                    # 1. Clean Neo4j
                    logger.info("Step 1: Cleaning fake summaries from Neo4j...")
                    removed_count = neo4j_mgr.delete_property(label=None, property_key="fake_summary", all_labels=True)
                    logger.info(f"Removed {removed_count} fake summaries from Neo4j.")
                    
                    # 2. Clean L1 Cache
                    logger.info("Step 2: Cleaning fake summaries from L1 cache...")
                
                cache_manager.load()
                removed_count = cache_manager.clean_fake_summaries()
                if removed_count > 0:
                    cache_manager._write_cache_to_file()
                else:
                    logger.info("No fake entries found in L1 cache. Cache file remains unchanged.")
                
                logger.info("✅ Cleanup of fake summaries complete.")

    except Exception as e:
        logger.critical(f"An unhandled error occurred: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
