#!/usr/bin/env python3
import os, tempfile, shutil
import sys
import yaml
import argparse
import clang.cindex


import subprocess


class ClangSpanExtractor:
   
    def __init__(self, compile_commands_path, project_path=None):
        compile_commands_path = os.path.abspath(compile_commands_path)
        # Note: For a custom-built clang, ensure the path to its 'lib' directory
        # is in the LD_LIBRARY_PATH environment variable, so the cindex library
        # can find the correct libclang.so file.
        # Example: export LD_LIBRARY_PATH=/path/to/your/llvm/build/lib:$LD_LIBRARY_PATH

        compile_commands_path = os.path.abspath(compile_commands_path)

        if os.path.isdir(compile_commands_path):
            # Expect compile_commands.json inside
            if not os.path.exists(os.path.join(compile_commands_path, "compile_commands.json")):
                raise FileNotFoundError(f"No compile_commands.json found in {compile_commands_path}")
            db_dir = compile_commands_path

        elif os.path.isfile(compile_commands_path):
            filename = os.path.basename(compile_commands_path)
            if filename != "compile_commands.json":
                # create temporary directory with standard name
                tmpdir = tempfile.mkdtemp(prefix="clangdb_")
                shutil.copy(compile_commands_path, os.path.join(tmpdir, "compile_commands.json"))
                db_dir = tmpdir
            else:
                db_dir = os.path.dirname(compile_commands_path)

        else:
            raise FileNotFoundError(f"{compile_commands_path} not found")

        if not project_path:
            project_path = os.path.dirname(compile_commands_path)
        self.project_path = os.path.abspath(project_path)

        # Load the database (now guaranteed to have compile_commands.json)
        try:
            self.db = clang.cindex.CompilationDatabase.fromDirectory(db_dir)
        except clang.cindex.CompilationDatabaseError as e:
            raise RuntimeError(f"Error loading compilation database from {db_dir}: {e}")
            sys.exit(1)
            
        self.index = clang.cindex.Index.create()

        # Dynamically find clang's resource directory for internal includes
        try:
            resource_dir = subprocess.check_output(['clang', '-print-resource-dir']).decode('utf-8').strip()
            self.clang_include_path = os.path.join(resource_dir, 'include')
        except (FileNotFoundError, subprocess.CalledProcessError):
            print("Warning: Could not find clang resource directory. Internal includes may be missing.")
            self.clang_include_path = None

    # ------------------------------------------------------------
    # Utility: collect all source files under folder or specific files
    # ------------------------------------------------------------
    @staticmethod
    def collect_source_files(paths):
        exts = {'.c', '.cc', '.cpp', '.hpp'}
        collected = []
        for p in paths:
            p = os.path.abspath(p)
            if os.path.isfile(p):
                if os.path.splitext(p)[1] in exts:
                    collected.append(p)
            elif os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        if os.path.splitext(f)[1] in exts:
                            collected.append(os.path.join(root, f))
        return collected

    # ------------------------------------------------------------
    # Extract spans for one file
    # ------------------------------------------------------------
    def extract_file_spans(self, file_path):
        file_path = os.path.abspath(file_path)
        try:
            cmds = self.db.getCompileCommands(file_path)
        except Exception:
            cmds = None

        if not cmds:
            print(f"cannot get the compile commands for file {file_path}")
            return []

        # --- Extract and sanitize compile arguments ---
        raw_args = []
        for cmd in cmds:
            raw_args = list(cmd.arguments)[1:]  # skip compiler binary
            break
        else:
            print(f"no compilation arguments for file {file_path}")
            return []

        # Remove compiler-only flags that break parsing
        skip_flags = {'-c', '-o', '-MMD', '-MF', '-MT', '-fcolor-diagnostics', '-fdiagnostics-color'}
        args = []
        skip_next = False
        for a in raw_args:
            if skip_next:
                skip_next = False
                continue
            if a in skip_flags:
                skip_next = True
                continue
            # Remove source filename if present (libclang gets file separately)
            if a == file_path or os.path.basename(a) == os.path.basename(file_path):
                continue
            args.append(a)

        # Add system and Clang include paths if found
        if self.clang_include_path:
            args.append(f'-I{self.clang_include_path}')
        # A general system include path can still be useful as a fallback
        args.append('-I/usr/include')

        print(f"\n=== Parsing {file_path} ===")
        print("Args:", args)

        try:
            tu = self.index.parse(
                file_path,
                args=args,
                options=clang.cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
            )
        except clang.cindex.TranslationUnitLoadError as e:
            print(f"Failed to parse {file_path}: {e}")
            return []

        # Log diagnostics to file and console
        with open('diagnostics.log', 'a', encoding='utf-8') as f:
            for diag in tu.diagnostics:
                f.write(f"DIAG: {diag}\n")
                print(f"DIAG: {diag}")

        spans = []
        self._walk(tu.cursor, spans)
        return spans

    # ------------------------------------------------------------
    # Recursively walk AST to extract function definitions
    # ------------------------------------------------------------
    def _walk(self, node, spans):
        # --- Skip declarations not in project path ---
        file_name = (
            node.location.file.name
            if node.location.file
            else node.translation_unit.spelling
            )
        if not file_name.startswith(self.project_path):
            return
        try:
            if node.kind == clang.cindex.CursorKind.FUNCTION_DECL and node.is_definition():
                start = (node.extent.start.line - 1, node.extent.start.column - 1)
                end = (node.extent.end.line - 1, node.extent.end.column - 1)
                #name_start = (node.location.line - 1, node.location.column - 1)
                name_start = self._find_function_name_token_pos(node)
                spans.append({
                    'name': node.spelling,
                    'file': file_name,
                    'name_start': name_start,
                    'body_span': {'start': start, 'end': end},
                })

            # Recurse into children regardless of file name
            for c in node.get_children():
                self._walk(c, spans)
        except Exception:
            pass

    def _find_function_name_token_pos(self, node):
        """
        Return (line, column) of the function name token if found within its extent.
        Works even when definition comes from a macro expansion.
        """
        try:
            for tok in node.get_tokens():
                if tok.spelling == node.spelling:
                    loc = tok.location
                    if loc.file and loc.file.name.endswith(".c"):
                        return (loc.line - 1, loc.column - 1)
            # fallback to node.location if no matching token
            return (node.location.line - 1, node.location.column - 1)
        except Exception:
            return (node.location.line - 1, node.location.column - 1)

    # ------------------------------------------------------------
    # Extract spans from multiple files
    # ------------------------------------------------------------
    def extract_spans(self, files=None):
        if not files:
            # Default: entire project
            files = [self.project_path]
        file_list = self.collect_source_files(files)
        all_spans = []
        for f in file_list:
            spans = self.extract_file_spans(f)
            if spans:
                all_spans.append({'file': f, 'functions': spans})
        return all_spans

    # ------------------------------------------------------------
    # Export as YAML or Python data
    # ------------------------------------------------------------
    def get_spans(self, files=None, format='yaml', output=None):
        data = self.extract_spans(files)
        if format == 'yaml':
            yaml_content = yaml.dump(data, sort_keys=False, allow_unicode=True)
            if output:
                with open(output, 'w', encoding='utf-8') as f:
                    f.write(yaml_content)
            return yaml_content
        return data


# ==============================================================
# CLI interface
# ==============================================================
def main():
    # Adjust path for custom libclang build

    parser = argparse.ArgumentParser(description='Extract function spans using clang.cindex')
    parser.add_argument('compile_commands', help='Path to compile_commands.json')
    parser.add_argument('--project_path', help='Project root (optional). If not given, use the same folder of the compile_commands file.')
    parser.add_argument('--file_path', nargs='+', help='Specific files or folders to extract')
    parser.add_argument('--output', help='Output YAML file (optional)')
    parser.add_argument('--format', choices=['yaml', 'dict'], default='yaml')
    args = parser.parse_args()

    extractor = ClangSpanExtractor(args.compile_commands, args.project_path)
    result = extractor.get_spans(args.file_path, format=args.format, output=args.output)

    if args.format == 'yaml' and not args.output:
        print(result)
    elif args.format == 'dict':
        import pprint
        pprint.pp(result)


if __name__ == '__main__':
    main()
