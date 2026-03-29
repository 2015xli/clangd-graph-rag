#!/usr/bin/env python3
import sys
import argparse


def extract_unique_markers(input_file, marker, count_repeats=False):
    '''
    Get document kinds, and reference kinds in clangd index yaml file by matching the leading marker.
    Document kinds: leading marker "--- !", such as "--- !Symbol", "--- !Refs"
    Reference kinds: leading marker "  - Kind:"
    Scope leading marker "Scoped:"
    '''
    unique_lines = set()
    with open(input_file, "r", encoding="utf-8") as f:
        if count_repeats:
            old_line = ""
            count = 1
        
        for line in f:
            if line.startswith(marker):
                line = line.rstrip("\n")
                if count_repeats:
                    if old_line == line:
                        count = count + 1
                    else:
                        if old_line != "":  #exclude the first time
                            print(f"Line '{old_line}' repeats {count} times")
                        count = 1
                        old_line = line

                unique_lines.add(line)

        if count_repeats and old_line != "":  #exclude the first time
            print(f"Line '{old_line}' repeats {count} times")

    return unique_lines

def main():
    parser = argparse.ArgumentParser(description='Extract unique lines starting with a marker from a file.')
    parser.add_argument('input_file', help='Input file to process')
    parser.add_argument('marker', nargs='?', default='--- !', help='Pattern that lines must start with (default: --- !)')
    parser.add_argument('--count-repeats', action='store_true', 
                      help='Count and report consecutive repeat lines')
    
    args = parser.parse_args()

    unique_markers = extract_unique_markers(args.input_file, args.marker, args.count_repeats)
    print("\nUnique lines:")
    for line in sorted(unique_markers):
        print(line)

if __name__ == "__main__":
    main()
