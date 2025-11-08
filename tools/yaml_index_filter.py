#!/usr/bin/env python3
import sys
import os
import yaml
from pathlib import Path

def load_yaml_documents(path):
    class IgnoreTagsLoader(yaml.SafeLoader):
        pass
    def ignore_unknown(loader, tag_suffix, node):
        return loader.construct_mapping(node)
    IgnoreTagsLoader.add_multi_constructor('', ignore_unknown)
    with open(path, "r", encoding="utf-8") as f:
        return list(yaml.load_all(f, Loader=IgnoreTagsLoader))

def dump_yaml_documents(docs, path):
    """Dump YAML documents back to file with proper tags and '...' endings."""
    class TaggedDumper(yaml.SafeDumper):
        pass

    def represent_with_tag(dumper, data):
        # Choose tag based on document content
        if "SymInfo" in data:
            tag = "!Symbol"
        elif "References" in data:
            tag = "!Refs"
        elif "Subject" in data:
            tag = "!Relations"
        else:
            tag = "!"
        node = dumper.represent_mapping(tag, data)
        return node

    TaggedDumper.add_representer(dict, represent_with_tag)

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump_all(
            docs,
            f,
            sort_keys=False,
            default_flow_style=False,
            Dumper=TaggedDumper,
            explicit_start=True,   # adds "---"
            explicit_end=True      # adds "..."
        )

def file_in_project(uri, project_path):
    """Check if FileURI points inside the project path."""
    if not uri or not uri.startswith("file://"):
        return False
    file_path = uri[len("file://"):]
    return os.path.abspath(file_path).startswith(os.path.abspath(project_path))

def main(yaml_path, project_path):
    docs = load_yaml_documents(yaml_path)

    symbols, refs, relations = [], [], []

    # First pass — collect subset IDs
    subset_ids = set()
    for doc in docs:
        if isinstance(doc, dict) and doc.get("SymInfo"):
            file_uri = doc.get("Definition", {}).get("FileURI") or doc.get("CanonicalDeclaration").get("FileURI")
            if file_in_project(file_uri, project_path):
                symbols.append(doc)
                subset_ids.add(doc.get("ID"))

    # Second pass — filter Refs and Relations
    for doc in docs:
        if isinstance(doc, dict) and not doc.get("SymInfo") and doc.get("References"):
            if doc.get("ID") not in subset_ids:
                continue
            kept_refs = []
            for ref in doc.get("References", []):
                file_uri = ref.get("Location").get("FileURI")
                if file_in_project(file_uri, project_path):
                    kept_refs.append(ref)
            if kept_refs:
                new_doc = dict(doc)
                new_doc["References"] = kept_refs
                refs.append(new_doc)

        elif isinstance(doc, dict) and not doc.get("SymInfo") and not doc.get("References") and doc.get("Subject"):
            subj_id = doc.get("Subject", {}).get("ID")
            obj_id  = doc.get("Object", {}).get("ID")
            if subj_id in subset_ids and obj_id in subset_ids:
                relations.append(doc)

    filtered_docs = symbols + refs + relations
    output_path = os.path.splitext(yaml_path)[0] + "_filtered.yaml"
    dump_yaml_documents(filtered_docs, output_path)

    print(f"[✓] Filtered YAML written to: {output_path}")
    print(f"    Symbols kept: {len(symbols)}")
    print(f"    Refs kept:    {len(refs)}")
    print(f"    Relations kept: {len(relations)}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 filter_clangd_yaml.py <index.yaml> <project_path>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
