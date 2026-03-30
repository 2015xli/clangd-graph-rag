#!/usr/bin/env python3
"""
CLI interface for the Neo4j database manager.
"""
import logging
import argparse
import json
import sys
from . import Neo4jManager

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

def _recursive_type_check(data, indent=0, path="", output_lines: list = None):
    """Recursively traverses a nested data structure and logs types/shapes."""
    if output_lines is None:
        output_lines = []
    prefix = "  " * indent
    if isinstance(data, dict):
        output_lines.append(f"{prefix}{path} (dict)")
        for k, v in data.items():
            _recursive_type_check(v, indent + 1, f"{path}.{k}", output_lines)
    elif isinstance(data, list):
        output_lines.append(f"{prefix}{path} (list of {len(data)} items)")
        if data:
            _recursive_type_check(data[0], indent + 1, f"{path}[0]", output_lines)
    elif isinstance(data, tuple):
        output_lines.append(f"{prefix}{path} (tuple of {len(data)} items)")
        if data:
            _recursive_type_check(data[0], indent + 1, f"{path}[0]", output_lines)
    else:
        output_lines.append(f"{prefix}{path} ({type(data).__name__}) = {str(data)[:50]}")
    return output_lines

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    parser = argparse.ArgumentParser(description="A CLI tool for Neo4j database management.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- dump-schema command ---
    parser_schema = subparsers.add_parser("dump-schema", help="Fetch and print the graph schema.")
    parser_schema.add_argument("-o", "--output", help="Optional path to save the output JSON file.")
    parser_schema.add_argument("--only-relations", action="store_true", help="Only show relationships, skip node properties.")
    parser_schema.add_argument("--with-node-counts", action="store_true", help="Include node and relationship counts in the output.")
    parser_schema.add_argument("--json-format", action="store_true", help="Output raw JSON from APOC meta procedures instead of formatted text.")

    # --- delete-property command ---
    parser_delete = subparsers.add_parser("delete-property", help="Delete a property from all nodes with a given label.")
    parser_delete.add_argument("--label", help="The node label to target (e.g., 'FUNCTION'). Required unless --all-labels is used.")
    parser_delete.add_argument("--key", required=True, help="The property key to remove (e.g., 'summaryEmbedding').")
    parser_delete.add_argument("--all-labels", action="store_true", help="Delete the property from all nodes that have it, regardless of label.")
    parser_delete.add_argument("--rebuild-indices", action="store_true", help="If deleting embedding properties, drop and recreate vector indices.")

    # --- dump-schema-types command ---
    parser_check_types = subparsers.add_parser("dump-schema-types", help="Recursively check and print types of the schema data returned by Neo4j.")
    parser_check_types.add_argument("-o", "--output", help="Optional path to save the output text file.")

    args = parser.parse_args()

    with Neo4jManager() as neo4j_mgr:
        if not neo4j_mgr.check_connection():
            sys.exit(1)

        if args.command == "dump-schema":
            schema_info = neo4j_mgr.get_schema()
            if not schema_info or schema_info.get("error"):
                logger.error("Could not retrieve schema.")
                sys.exit(1)
            
            if args.json_format:
                output_content = json.dumps(schema_info, default=str, indent=2)
            else:
                output_content = neo4j_mgr.format_schema_for_display(schema_info, args)

            if args.output:
                try:
                    with open(args.output, 'w') as f:
                        f.write(output_content)
                    logger.info(f"Schema successfully written to {args.output}")
                except Exception as e:
                    logger.error(f"Failed to write schema to file: {e}")
            else:
                print(output_content)
        
        elif args.command == "delete-property":
            if not args.label and not args.all_labels:
                logger.error("Error: Either --label or --all-labels must be specified for 'delete-property'.")
                sys.exit(1)
            if args.label and args.all_labels:
                logger.error("Error: Cannot specify both --label and --all-labels. Choose one.")
                sys.exit(1)

            count = neo4j_mgr.delete_property(args.label, args.key, args.all_labels)
            logger.info(f"Removed property '{args.key}' from {count} nodes.")

            if args.rebuild_indices and "embedding" in args.key.lower():
                logger.info("Rebuilding vector indices as requested...")
                neo4j_mgr.rebuild_vector_indices()
        
        elif args.command == "dump-schema-types":
            output_lines = _recursive_type_check(neo4j_mgr.get_schema(), path="schema_info")
            output_content = "\n".join(output_lines)

            if args.output:
                try:
                    with open(args.output, 'w') as f:
                        f.write(output_content)
                    logger.info(f"Schema types successfully written to {args.output}")
                except Exception as e:
                    logger.error(f"Failed to write schema types to file: {e}")
            else:
                print(output_content)

if __name__ == "__main__":
    main()
