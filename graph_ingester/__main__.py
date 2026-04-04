#!/usr/bin/env python3
"""
CLI entry point for the graph ingester package.
Allows running sub-modules via:
python3 -m graph_ingester [symbol|call] [args]
"""

import sys
import argparse
from log_manager import init_logging

# Import the main functions from the sub-modules
from .symbol import main as symbol_main
from .call import main as call_main
from .include import main as include_main

def main():
    # Initialize logging with standardized formatting
    init_logging()
    
    parser = argparse.ArgumentParser(
        description="Graph Ingester Package - Sub-module CLI Dispatcher",
        usage="python3 -m graph_ingester <submodule> [args]"
    )
    parser.add_argument("submodule", choices=["symbol", "call", "include"], help="Sub-module to run")

    # The sub-modules have their own complex argument parsing.
    # To keep it simple and maintain their original CLI experience,
    # we just hand over the argv to them.
    
    if len(sys.argv) < 2:
        parser.print_help()
        sys.exit(1)
        
    submodule = sys.argv[1]
    # Remove the submodule name from argv so the sub-module's parser sees its own args
    sys.argv.pop(1) 
    
    if submodule == "symbol":
        sys.exit(symbol_main())
    elif submodule == "call":
        sys.exit(call_main())
    elif submodule == "include":
        sys.exit(include_main())
    else:
        # This should not be reached due to choices in add_argument
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
