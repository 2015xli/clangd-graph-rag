#!/usr/bin/env python3
"""
CLI entry point for the summarization drivers.
Allows running the full summarization process via:
python3 -m summary_driver [args]
"""

import sys
from .full_summarizer import main

if __name__ == "__main__":
    sys.exit(main())
