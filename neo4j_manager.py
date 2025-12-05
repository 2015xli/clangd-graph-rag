import os
from neo4j import GraphDatabase
import logging
import argparse
import json
from typing import List, Dict, Tuple, Optional, Any
from collections import defaultdict
from tqdm import tqdm

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Neo4j connection settings
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "12345678")


def align_string(string: str, width: int = 45, direction: str = 'right', fillchar: str = ' ') -> str:
    if direction == 'left':
        return string.ljust(width, fillchar)
    elif direction == 'right':
        return string.rjust(width, fillchar)
    else:
        return string.center(width)

class Neo4jManager:
    """Manages Neo4j database operations."""
    def __init__(self, uri: str = NEO4J_URI, user: str = NEO4J_USER, password: str = NEO4J_PASSWORD) -> None:
        self.uri, self.user, self.password = uri, user, password
        self.driver = None
        
    def __enter__(self):
        self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.driver: self.driver.close()
    
    def check_connection(self) -> bool:
        try:
            self.driver.verify_connectivity()
            logger.info("✅ Connection established!")
            return True
        except Exception as e:
            logger.error(f"❌ Connection failed: {e}")
            return False
        
    def reset_database(self) -> None:
        with self.driver.session() as session:
            logger.info("Deleting existing data...")
            session.run("MATCH (n) DETACH DELETE n")
            logger.info("Database cleared.")
    
    def create_constraints(self) -> None:
        constraints = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:FILE) REQUIRE f.path IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:FOLDER) REQUIRE f.path IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (fn:FUNCTION) REQUIRE fn.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (ds:DATA_STRUCTURE) REQUIRE ds.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (c:CLASS_STRUCTURE) REQUIRE c.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (m:METHOD) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:FIELD) REQUIRE f.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (v:VARIABLE) REQUIRE v.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:NAMESPACE) REQUIRE n.id IS UNIQUE",
           # "CREATE CONSTRAINT IF NOT EXISTS FOR (n:NAMESPACE) REQUIRE n.qualified_name IS UNIQUE",
        ]
        with self.driver.session() as session:
            for constraint in constraints:
                session.run(constraint)
    
    def update_project_node(self, project_path: str, properties: Dict[str, Any]) -> None:
        """Finds or creates the PROJECT node and updates its properties."""
        with self.driver.session() as session:
            # Ensure the name is set if not already present
            if 'name' not in properties:
                properties['name'] = os.path.basename(project_path) or "Project"
            
            session.run(
                "MERGE (p:PROJECT {path: $path}) SET p += $properties",
                {"path": project_path, "properties": properties}
            )
    
    def get_graph_commit_hash(self, project_path: str) -> Optional[str]:
        """Fetches the commit_hash property from the PROJECT node."""
        query = "MATCH (p:PROJECT {path: $path}) RETURN p.commit_hash AS hash"
        result = self.execute_read_query(query, {"path": project_path})
        if result and result[0] and result[0].get('hash'):
            return result[0]['hash']
        return None

    def verify_project_path(self, project_path: str) -> bool:
        """Verifies that the project path in the graph matches the provided path."""
        query = "MATCH (p:PROJECT) RETURN p.path AS path"
        result = self.execute_read_query(query)

        if not result:
            logger.warning("No PROJECT node found in the graph. Skipping path verification, assuming new graph.")
            return True # Allow proceeding on an empty graph

        if len(result) > 1:
            logger.error(f"Multiple PROJECT nodes found in the graph. Aborting due to ambiguous state.")
            return False

        graph_project_path = result[0].get('path')
        if graph_project_path != project_path:
            logger.critical(f"Project path mismatch! Provided: '{project_path}', Graph contains: '{graph_project_path}'. Aborting.")
            return False

        logger.info("Project path verification successful.")
        return True

    def process_batch(self, batch: List[Tuple[str, Dict]]) -> List[Any]: # Returns list of summary.counters
        all_counters = []
        with self.driver.session() as session:
            with session.begin_transaction() as tx:
                for cypher, params in batch:
                    result = tx.run(cypher, **params)
                    all_counters.append(result.consume().counters)
                tx.commit()
        return all_counters

    def execute_autocommit_query(self, cypher: str, params: Dict = None) -> Any: # Returns summary.counters
        with self.driver.session() as session:
            result = session.run(cypher, **(params or {}))
            return result.consume().counters

    def execute_read_query(self, cypher: str, params: dict = None) -> list[dict]:
        """Executes a read query and returns a list of result records."""
        with self.driver.session() as session:
            result = session.run(cypher, **(params or {}))
            return [record.data() for record in result]

    def execute_query_and_return_records(self, cypher: str, params: dict = None) -> List[Dict]:
        """Executes a query and returns a list of result records."""
        with self.driver.session() as session:
            result = session.run(cypher, **(params or {}))
            return [record.data() for record in result]

    def cleanup_orphan_nodes(self):
        query = """
                MATCH (n)
                WHERE NOT (n)--()
                WITH collect( properties(n)) AS deleted_nodes,
                     collect(n) AS nodes
                FOREACH (x IN nodes | DETACH DELETE x)
                RETURN deleted_nodes
        """

        with self.driver.session() as session:
            record = session.run(query).single()
            deleted_nodes = record["deleted_nodes"]
            logger.debug(f"Deleted {len(deleted_nodes)} orphan nodes.")
            logger.debug(json.dumps(deleted_nodes, indent=2, default=str))
            return len(deleted_nodes)

    def total_nodes_in_graph(self) -> int:
        query = "MATCH (n) RETURN count(n)"
        with self.driver.session() as session:
            result = session.run(query)
            return result.single()[0]

    def total_relationships_in_graph(self) -> int:
        query = "MATCH ()-[r]->() RETURN count(r)"
        with self.driver.session() as session:
            result = session.run(query)
            return result.single()[0]

    def purge_files(self, file_paths: List[str]) -> Tuple[int, int]:
        """Deletes FILE nodes for the given paths. Not pruning empty folders."""
        deleted_files = 0
        if not file_paths:
            return 0, 0

        with self.driver.session() as session:
            # Delete the specified FILE nodes
            del_files_query = "UNWIND $paths AS path MATCH (f:FILE {path: path}) DETACH DELETE f"
            result = session.run(del_files_query, paths=file_paths)
            deleted_files = result.consume().counters.nodes_deleted
            logger.info(f"Purged {deleted_files} FILE nodes.")

        return deleted_files

    def cleanup_empty_paths_recursively(self) -> Tuple[int, int]:
        """Deletes FILE nodes for the empty paths and recursively prunes empty FOLDERs."""
        deleted_files = 0
        deleted_folders = 0

        with self.driver.session() as session:
            # Delete the specified FILE nodes
            del_files_query = "MATCH (f:FILE) WHERE NOT (f)-->() DETACH DELETE f"
            result = session.run(del_files_query)
            deleted_files = result.consume().counters.nodes_deleted
            logger.info(f"Deleted {deleted_files} empty FILE nodes.")

            # Iteratively delete empty folders
            while True:
                del_folders_query = """
                MATCH (d:FOLDER)
                WHERE NOT (d)-[:CONTAINS]->()
                DETACH DELETE d
                RETURN count(d)
                """
                result = session.run(del_folders_query)
                count = result.single()[0]
                if count == 0:
                    break
                deleted_folders += count
                logger.info(f"Pruned {deleted_folders} empty FOLDER nodes so far...")
        
        logger.info(f"Total empty FOLDER nodes pruned: {deleted_folders}")
        return deleted_files, deleted_folders

    def cleanup_empty_namespaces_recursively(self) -> int:
        """
        Deletes NAMESPACE nodes that do not contain any other nodes.
        This process is run iteratively to handle cascading deletions of nested namespaces.
        """
        total_deleted = 0
        while True:
            query = """
            MATCH (ns:NAMESPACE)
            WHERE NOT EXISTS((ns)-[:SCOPE_CONTAINS]->())
            DETACH DELETE ns
            RETURN count(ns) AS deletedCount
            """
            with self.driver.session() as session:
                result = session.run(query)
                deleted_count = result.single()['deletedCount']
                if deleted_count == 0:
                    break # No more namespaces to delete in this iteration
                total_deleted += deleted_count
                logger.info(f"Cleaned up {deleted_count} empty NAMESPACE nodes in this iteration.")
        return total_deleted

    def purge_symbols_defined_in_files(self, file_paths: List[str]) -> int:
        """
        Finds and deletes all symbols defined in the given file paths, including
        all their owned descendant symbols (methods, fields, nested types, etc.).
        """
        if not file_paths:
            return 0
        
        # This query uses APOC to find the entire subgraph of "owned" symbols
        # starting from the top-level symbols defined in a file.
        # The ">" in the relationship filter ensures we only traverse outwards
        # from the defined symbol to its children.

        # Currently,  the graph schema is (FILE) -[:DEFINES]-> (CLASS_STRUCTURE|DATA_STRUCTURE|FUNCTION|VARIABLE), 
        # meaning all the nodes in the other end of DEFINES relationship are ensured to be (CLASS_STRUCTURE|DATA_STRUCTURE|FUNCTION|VARIABLE). 
        # That means, we don't need to know the label of the other end node, but to match any node of the DEFINES relationship. 
        # If we use  (FILE) -[:DEFINES]-> (s), I guess it can avoid a match for the label of the other end node, 
        # compared to  (FILE) -[:DEFINES]-> (CLASS_STRUCTURE|DATA_STRUCTURE|FUNCTION|VARIABLE). 
        # Of course, this optimization is very marginal.

        query = """
        UNWIND $paths AS path
        MATCH (file:FILE {path: path})-[:DEFINES]->(s)
        CALL apoc.path.subgraphNodes(s, {
            relationshipFilter: "HAS_METHOD|HAS_FIELD|HAS_NESTED>"
        }) YIELD node
        DETACH DELETE node
        """
        with self.driver.session() as session:
            result = session.run(query, paths=file_paths)
            deleted_symbols = result.consume().counters.nodes_deleted
            logger.info(f"Purged {deleted_symbols} symbols (including descendants) defined in {len(file_paths)} files.")
            return deleted_symbols

    def purge_symbols_declared_in_files(self, file_paths: List[str]) -> int:
        """Finds and deletes all symbols declared in the given file paths."""
        if not file_paths:
            return 0
        
        query = """
        UNWIND $paths AS path
        MATCH (file:FILE {path: path})-[:DECLARES]->(s)
        WHERE s:FUNCTION OR s:CLASS_STRUCTURE OR s:DATA_STRUCTURE OR s:NAMESPACE
        DETACH DELETE s
        """
        with self.driver.session() as session:
            result = session.run(query, paths=file_paths)
            deleted_symbols = result.consume().counters.nodes_deleted
            logger.info(f"Purged {deleted_symbols} symbols declared in {len(file_paths)} files.")
            return deleted_symbols

    def ingest_include_relations(self, relations: List[Dict], batch_size: int = 1000):
        """
        Ingests :INCLUDES relationships between files in batches.

        Args:
            relations: A list of dictionaries, each with 'including_path' and 'included_path'.
            batch_size: The number of relations to process in each transaction.
        """
        if not relations:
            return

        logger.info(f"Ingesting {len(relations)} :INCLUDES relationships in batches of {batch_size}...")
        query = """
        UNWIND $batch as relation
        MATCH (including:FILE {path: relation.including_path})
        MATCH (included:FILE {path: relation.included_path})
        MERGE (including)-[:INCLUDES]->(included)
        """

        total_created = 0
        for i in tqdm(range(0, len(relations), batch_size), desc=align_string("Ingesting INCLUDES relationships")):
            batch = relations[i:i + batch_size]
            summary = self.execute_autocommit_query(query, {"batch": batch})
            total_created += summary.relationships_created

        logger.info(f"Finished ingesting :INCLUDES relationships. Total new relationships: {total_created}.")

    def purge_include_relations_from_files(self, file_paths: List[str]) -> int:
        """Deletes all outgoing :INCLUDES relationships from the given file paths."""
        if not file_paths:
            return 0
        
        query = """
        UNWIND $paths AS path
        MATCH (:FILE {path: path})-[r:INCLUDES]->()
        DELETE r
        RETURN count(r)
        """
        with self.driver.session() as session:
            result = session.run(query, paths=file_paths)
            count = result.single()[0]
            logger.info(f"Purged {count} :INCLUDES relationships from {len(file_paths)} files.")
            return count

    def create_vector_indices(self) -> None:
        """Creates vector indices for summary embeddings."""
        index_queries = [
            "CREATE VECTOR INDEX function_summary_embeddings IF NOT EXISTS FOR (n:FUNCTION) ON (n.summaryEmbedding) OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}",
            "CREATE VECTOR INDEX method_summary_embeddings IF NOT EXISTS FOR (n:METHOD) ON (n.summaryEmbedding) OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}",
            "CREATE VECTOR INDEX class_summary_embeddings IF NOT EXISTS FOR (n:CLASS_STRUCTURE) ON (n.summaryEmbedding) OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}",
            "CREATE VECTOR INDEX namespace_summary_embeddings IF NOT EXISTS FOR (n:NAMESPACE) ON (n.summaryEmbedding) OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}",
            "CREATE VECTOR INDEX file_summary_embeddings IF NOT EXISTS FOR (n:FILE) ON (n.summaryEmbedding) OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}",
            "CREATE VECTOR INDEX folder_summary_embeddings IF NOT EXISTS FOR (n:FOLDER) ON (n.summaryEmbedding) OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}",
        ]
        with self.driver.session() as session:
            logger.info("Creating vector indices for summary embeddings...")
            for query in index_queries:
                try:
                    session.run(query)
                except Exception as e:
                    logger.warning(f"Could not create vector index. This is expected on Neo4j Community Edition. Error: {e}")
                    break
            logger.info("Vector index setup complete.")

    def drop_vector_indices(self) -> None:
        """Drops existing vector indices for summary embeddings."""
        logger.info("Dropping existing vector indices...")
        existing_indices = self.execute_read_query("SHOW VECTOR INDEXES")
        
        with self.driver.session() as session:
            for index_info in existing_indices:
                if index_info.get("name", "").endswith("_summary_embeddings"):
                    index_name = index_info["name"]
                    drop_query = f"DROP INDEX {index_name}"
                    try:
                        session.run(drop_query)
                        logger.info(f"Dropped vector index: {index_name}")
                    except Exception as e:
                        logger.warning(f"Could not drop vector index {index_name}. Error: {e}")
            logger.info("Finished dropping vector indices.")

    def rebuild_vector_indices(self) -> None:
        """Drops and recreates all vector indices for summary embeddings."""
        self.drop_vector_indices()
        self.create_vector_indices()

    def get_schema(self) -> dict:
        """Fetches the graph schema using APOC meta procedures."""
        logger.info("Fetching graph schema...")
        try:
            # apoc.meta.graph() returns a single record with 'nodes' and 'relationships' lists
            graph_meta_raw = self.execute_read_query("CALL apoc.meta.graph() YIELD nodes, relationships RETURN nodes, relationships")
            
            # apoc.meta.schema() returns a list of records, each describing a label's properties
            node_properties_meta = self.execute_read_query("CALL apoc.meta.schema()")
            
            # Extract the actual nodes and relationships lists from the raw graph_meta
            graph_meta_nodes = graph_meta_raw[0]['nodes'] if graph_meta_raw else []
            graph_meta_relationships = graph_meta_raw[0]['relationships'] if graph_meta_raw else []

            # Combine results into a single dictionary
            schema_info = {
                "graph_meta": {
                    "nodes": graph_meta_nodes,
                    "relationships": graph_meta_relationships
                },
                "node_properties_meta": node_properties_meta
            }
            return schema_info
        except Exception as e:
            logger.error(f"Failed to fetch schema. Ensure APOC plugin is installed. Error: {e}")
            return {"error": str(e)}

    def delete_property(self, label: Optional[str], property_key: str, all_labels: bool = False) -> int:
        """
        Deletes a property from nodes with a given label, or from all nodes if all_labels is True.
        Returns the count of affected nodes.
        """
        if not label and not all_labels:
            raise ValueError("Either 'label' must be provided or 'all_labels' must be True.")
        if label and all_labels:
            raise ValueError("Cannot specify both 'label' and 'all_labels'. Choose one.")

        target_clause = f"n:{label}" if label else "n"
        logger.info(f"Deleting property '{property_key}' from nodes matching '{target_clause}'...")
        
        query = f"MATCH ({target_clause}) WHERE n.{property_key} IS NOT NULL REMOVE n.{property_key} RETURN count(n)"
        
        with self.driver.session() as session:
            result = session.run(query)
            count = result.single()[0] if result.peek() else 0
            logger.info(f"Removed property '{property_key}' from {count} nodes.")
            return count

def _recursive_type_check(data, indent=0, path="", output_lines: list = None): # NEW HELPER
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

def _format_schema_for_display(schema_info: dict, args) -> str:
    output_lines = []
    all_present_property_keys = set()

    # --- Node Properties Section ---
    if not args.only_relations:
        output_lines.append("Node Properties:")
        
        # Group properties by label from apoc.meta.schema() output
        props_by_label = defaultdict(dict)
        # The apoc.meta.schema() returns a list with one dict, where the actual schema is under 'value'
        apoc_schema_data = schema_info.get("node_properties_meta", [])
        if apoc_schema_data and isinstance(apoc_schema_data[0], dict) and "value" in apoc_schema_data[0]:
            for label, details in apoc_schema_data[0]["value"].items():
                if details.get("type") == "node": # Ensure it's a node schema
                    for prop_key, prop_details in details.get("properties", {}).items():
                        props_by_label[label][prop_key] = prop_details

        # Get node counts from graph_meta for display
        node_counts = {}
        for node_obj in schema_info['graph_meta'].get("nodes", []):
            if node_obj.get('name'): # 'name' is the label in this context
                node_counts[node_obj['name']] = node_obj.get('count', 0)

        for label in sorted(props_by_label.keys()):
            count_str = f" (count: {node_counts.get(label, 0)})" if args.with_node_counts else ""
            output_lines.append(f"  ({label}){count_str}")
            for prop_key in sorted(props_by_label[label].keys()):
                prop_details = props_by_label[label][prop_key]
                prop_type = prop_details.get("type", "unknown")
                is_indexed = " (INDEXED)" if prop_details.get("indexed") else ""
                is_unique = " (UNIQUE)" if prop_details.get("unique") else ""
                output_lines.append(f"    {prop_key}: {prop_type}{is_indexed}{is_unique}")
                # Collect unique property keys for later explanation
                if prop_key not in all_present_property_keys:
                    all_present_property_keys.add(prop_key)
        output_lines.append("") # Blank line for separation

    # --- Relationships Section ---
    output_lines.append("Relationships:")
    
    # Group relationships by (start_label, rel_type)
    grouped_relations = defaultdict(lambda: defaultdict(set)) # (start_label) -> (rel_type) -> set(end_labels)

    for rel_list_item in schema_info['graph_meta'].get("relationships", []):
        if isinstance(rel_list_item, (list, tuple)) and len(rel_list_item) == 3:
            start_node_map = dict(rel_list_item[0]) if isinstance(rel_list_item[0], tuple) else rel_list_item[0]
            rel_type = rel_list_item[1]
            end_node_map = dict(rel_list_item[2]) if isinstance(rel_list_item[2], tuple) else rel_list_item[2]

            start_label = start_node_map.get('name', 'UNKNOWN')
            end_label = end_node_map.get('name', 'UNKNOWN')
            
            grouped_relations[start_label][rel_type].add(end_label)
        else:
            logger.warning(f"Unexpected relationship format in graph_meta: {rel_list_item}")

    # Format and print grouped relationships
    for start_label in sorted(grouped_relations.keys()):
        for rel_type in sorted(grouped_relations[start_label].keys()):
            end_labels = sorted(list(grouped_relations[start_label][rel_type]))
            end_labels_str = "|".join(end_labels)
            
            # Find count for the start_label if available
            start_node_count = node_counts.get(start_label, 0) if args.with_node_counts else None
            count_str = f" (count: {start_node_count})" if start_node_count is not None else ""

            output_lines.append(f"  ({start_label}){count_str} -[:{rel_type}]-> ({end_labels_str})")

    # --- Property Explanations Section ---
    if not args.only_relations and all_present_property_keys:
        output_lines.append("\nProperty Explanations:")
        property_explanations = {
            "id": "Unique identifier for the node.",
            "name": "Name of the entity (e.g., function name, file name).",
            "path": "Relative path to the project root if it is within the project folder. Otherwise, it is absolute path, including the PROJECT node",
            "name_location": "Entity's name location in the file, showing the start position: [line, column].",
            "body_location": "Entity's body location in the file, showing the range: [start_line, start_column, end_line, end_column].",
            "code_hash": "MD5 hashing value of entity's original source code body. Used to detect changed code.",
            "kind": "Type of symbol (e.g., Function, Struct, Variable).",
            "scope": "Visibility scope (e.g., global, static).",
            "language": "Programming language of the source code.",
            "type": "Data type of the symbol (e.g., int, void*).",
            "return_type": "Return type of a function/method.",
            "signature": "Full signature of a function/method.",
            "has_definition": "Boolean indicating if a symbol has a definition/implementation, not just a declaration.",
            "codeSummary": "LLM-generated summary of the code's literal function.",
            "summary": "LLM-generated context-aware summary of the node's purpose.",
            "summaryEmbedding": "Vector embedding of the 'summary' for similarity search.",
            "file_path": "Absolute path to the file containing the symbol."
        }
        #for prop_key in sorted(list(all_present_property_keys)):
        for prop_key, explanation in property_explanations.items():
            if prop_key in all_present_property_keys:
                output_lines.append(f"  {prop_key}: {explanation}")

    return "\n".join(output_lines)

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

    # --- check_types command --- RENAMED
    parser_check_types = subparsers.add_parser("dump-schema-types", help="Recursively check and print types of the schema data returned by Neo4j.")
    parser_check_types.add_argument("-o", "--output", help="Optional path to save the output text file.") # Added output arg

    args = parser.parse_args()

    with Neo4jManager() as neo4j_mgr:
        if not neo4j_mgr.check_connection():
            return 1

        if args.command == "dump-schema":
            schema_info = neo4j_mgr.get_schema()
            if not schema_info or schema_info.get("error"):
                logger.error("Could not retrieve schema.")
                return 1
            
            if args.json_format:
                output_content = json.dumps(schema_info, default=str, indent=2)
            else:
                output_content = _format_schema_for_display(schema_info, args)

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
                return 1
            if args.label and args.all_labels:
                logger.error("Error: Cannot specify both --label and --all-labels. Choose one.")
                return 1

            count = neo4j_mgr.delete_property(args.label, args.key, args.all_labels)
            logger.info(f"Removed property '{args.key}' from {count} nodes.")

            if args.rebuild_indices and "embedding" in args.key.lower():
                logger.info("Rebuilding vector indices as requested...")
                neo4j_mgr.rebuild_vector_indices()
        
        elif args.command == "dump-schema-types": # RENAMED
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
