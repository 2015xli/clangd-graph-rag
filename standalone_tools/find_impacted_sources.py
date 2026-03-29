#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from collections import defaultdict, deque
from clang import cindex


def load_compile_commands(path="compile_commands.json"):
    with open(path) as f:
        return json.load(f)


def get_clang_resource_dir():
    """Return the Clang built-in include path, so <stdint.h> etc. resolve properly."""
    try:
        resource_dir = subprocess.check_output(['clang', '-print-resource-dir']).decode('utf-8').strip()
        return os.path.join(resource_dir, 'include')
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("Could not find clang resource directory via 'clang -print-resource-dir'. Internal includes may be missing.")
        return None


def build_include_graph(compile_db):
    """Build a reverse include graph: included_file -> { including_files }"""
    index = cindex.Index.create()
    include_graph = defaultdict(set)
    skip_flags = {'-o', '-MMD', '-MF', '-MT', '-fcolor-diagnostics', '-fdiagnostics-color'}

    for entry in compile_db:
        src = os.path.abspath(entry["file"])
        args = entry.get("arguments") or entry.get("command").split()
        # Remove compiler name
        if args and args[0].endswith(("clang", "clang++", "gcc", "g++")):
            args = args[1:]

        # Sanitize arguments
        skip_next = False
        new_args = []
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a in skip_flags:
                skip_next = True
                continue
            if a.endswith((".o", ".so", ".a")):
                continue
            if a == src or os.path.basename(a) == os.path.basename(src):
                continue
            new_args.append(a)

        # Ensure compile-only mode
        if "-c" not in new_args:
            new_args.append("-c")

        # Add Clang internal include path
        clang_include_path = get_clang_resource_dir()
        if clang_include_path:
            new_args.append(f"-I{clang_include_path}")

        os.chdir(entry["directory"])  # Important: relative includes resolve properly
        print(f"clang {' '.join(new_args)} {src}")

        try:
            tu = index.parse(
                src,
                args=new_args,
                options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
            )
        except Exception as e:
            print(f"  [!] Failed to parse {src}: {e}")
            continue

        for inc in tu.get_includes():
            if inc.source is None or inc.include is None:
                continue
            including = os.path.abspath(inc.source.name)
            included = os.path.abspath(inc.include.name)
            print(f"[INC] {including} -> {included}")
            include_graph[included].add(including)

    return include_graph


def find_impacted_sources(include_graph, changed_header):
    """Find all .c/.cpp files that directly or indirectly include the given header."""
    changed_header = os.path.abspath(changed_header)
    impacted = set()
    queue = deque([changed_header])

    print(f"[INFO] Looking for header: {changed_header}")

    if changed_header not in include_graph:
        print(f"[!] Header {changed_header} not found directly in include graph keys.")
        # Try matching by basename
        matches = [k for k in include_graph if os.path.basename(k) == os.path.basename(changed_header)]
        if matches:
            print(f"[HINT] Found similar headers in graph:")
            for m in matches:
                print(f"       {m}")
            print("       (you may need to pass the absolute path above)")
        return []

    while queue:
        cur = queue.popleft()
        for dependent in include_graph.get(cur, []):
            if dependent not in impacted:
                impacted.add(dependent)
                queue.append(dependent)

    # Return only source files (.c, .cpp, .cc, .cxx)
    return [f for f in impacted if f.endswith((".c", ".cpp", ".cc", ".cxx"))]


def main():
    if len(sys.argv) != 3:
        print("Usage: python find_impacted_sources.py <path_to_compile_commands.json> <changed_header>")
        sys.exit(1)

    compile_db = load_compile_commands(sys.argv[1])
    changed_header = sys.argv[2]
    include_graph = build_include_graph(compile_db)

    print("\n=== Include Graph Summary (first 3 entries) ===")
    for k, v in list(include_graph.items()): #[:3]:
        print(f"{k} <- {len(v)} files")
        for inc in v:
            print(f"<-----------{inc}")

    impacted = find_impacted_sources(include_graph, changed_header)

    print("\n=== Impacted Source Files ===")
    if not impacted:
        print("(none)")
    else:
        for f in impacted:
            print(f)


if __name__ == "__main__":
    main()
