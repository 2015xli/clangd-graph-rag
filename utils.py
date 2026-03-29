#!/usr/bin/env python3
"""
General utility functions for the project.
"""

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
