#!/usr/bin/env python3
"""
Entry point for running the neo4j_manager package as a script.
Usage: python3 -m neo4j_manager <command> [options]
"""
import sys
from .cli import main

if __name__ == "__main__":
    sys.exit(main())
