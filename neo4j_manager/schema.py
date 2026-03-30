import logging
import json
from typing import List, Dict, Optional, Any
from collections import defaultdict

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class SchemaMixin:
    """Methods for database structure, constraints, and schema management."""

    def setup_database(self, project_path: str, init_property: Dict[str, Any]) -> None:
        """Initializes the database by clearing existing data and setting up constraints."""
        self.reset_database()
        self.reset_vector_indexes()
        self.reset_constraints()
        self.update_project_node(project_path, init_property)
        self.create_constraints()

    def reset_database(self) -> None:
        """Clears all data from the database."""
        with self.driver.session() as session:
            logger.info("Deleting existing data...")
            session.run("MATCH (n) DETACH DELETE n")
            logger.info("Database cleared.")

    def reset_vector_indexes(self):
        """Drops all VECTOR indexes."""
        with self.driver.session() as session:
            # Drop vector indexes
            vector_indexes = session.run("""
                SHOW INDEXES
                YIELD name, type
                WHERE type = 'VECTOR'
                RETURN name
            """).value()

            for name in vector_indexes:
                session.run(f"DROP INDEX {name} IF EXISTS")
        
        logger.info(f"Vector indexes dropped: {vector_indexes}")
        return {"vector_indexes_dropped": vector_indexes}

    def reset_constraints(self):
        """Drops all constraints."""
        with self.driver.session() as session:
            # Drop constraints
            constraints = session.run("""
                SHOW CONSTRAINTS
                YIELD name
                RETURN name
            """).value()

            for name in constraints:
                session.run(f"DROP CONSTRAINT {name} IF EXISTS")
        
        logger.info(f"Constraints dropped: {constraints}")
        return {"constraints_dropped": constraints}

    def create_constraints(self) -> None:
        """Creates unique constraints for all primary node labels."""
        constraints = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:FILE) REQUIRE f.path IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:FOLDER) REQUIRE f.path IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (fn:FUNCTION) REQUIRE fn.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (ds:DATA_STRUCTURE) REQUIRE ds.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (c:CLASS_STRUCTURE) REQUIRE c.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (m:METHOD) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:FIELD) REQUIRE f.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (v:VARIABLE) REQUIRE v.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (m:MACRO) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (ta:TYPE_ALIAS) REQUIRE ta.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:NAMESPACE) REQUIRE n.id IS UNIQUE",
        ]
        # NOTE: It is very weird that I hit two NAMESPACE symbols having same qualified_name in llvm's index yaml file
        # This is why we do not use qualified_name as a unique constraint.
        # Example IDs: E1B385F53F8B0222 and B6A044C64358A92D (both 'internal' in same scope).
        
        with self.driver.session() as session:
            for constraint in constraints:
                session.run(constraint)
    
    def bootstrap_schema(self) -> None:
        """Registers optional property keys and relationship types to silence planner warnings."""
        schema_cypher = """
            MERGE (s:__SCHEMA__)
            SET
            s.code_hash = '',
            s.code_analysis = '',
            s.body_location = [],
            s.dummy = true
            WITH s
            MERGE (s)-[:INHERITS]->(s)
            MERGE (s)-[:OVERRIDDEN_BY]->(s)
            MERGE (s)-[:HAS_METHOD]->(s)
            MERGE (s)-[:HAS_FIELD]->(s)
        """
        with self.driver.session() as session:
            session.run(schema_cypher)

    def create_vector_indexes(self) -> None:
        """Creates vector indices for summary embeddings."""
        index_queries = [
            "CREATE VECTOR INDEX summary_embeddings IF NOT EXISTS FOR (e:ENTITY) ON (e.summaryEmbedding) OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}",
        ]

        with self.driver.session() as session:
            logger.info("Creating vector indices for summary embeddings...")
            for query in index_queries:
                try:
                    session.run(query)
                except Exception as e:
                    logger.warning(f"Could not create vector index. Error: {e}")
                    break
            logger.info("Vector index setup complete.")

    def remove_agent_facing_schema(self):
        """
        Removes all agent-specific schema additions (ENTITY label, synthetic IDs, unified indexes)
        and restores the original per-label constraints for the build process.
        """
        logger.info("Removing agent-facing schema additions...")
        # 1. Drop unified vector index
        self.reset_vector_indexes()

        # 2. Drop all constraints (including the one on ENTITY)
        self.reset_constraints()

        # 3. Remove synthetic 'id' property from nodes that have it
        with self.driver.session() as session:
            id_removal_query = """
            CALL apoc.periodic.iterate(
                "MATCH (n) WHERE n:FILE OR n:FOLDER RETURN n",
                "REMOVE n.id",
                {batchSize: 10000, parallel: true}
            )
            """
            session.run(id_removal_query)
            logger.info("Removed synthetic 'id' properties from FILE and FOLDER nodes.")

        # 4. Remove the ENTITY label from all nodes
        with self.driver.session() as session:
            label_removal_query = """
            CALL apoc.periodic.iterate(
                "MATCH (e:ENTITY) RETURN e",
                "REMOVE e:ENTITY",
                {batchSize: 10000, parallel: true}
            )
            """
            session.run(label_removal_query)
            logger.info("Removed :ENTITY label from all relevant nodes.")

        # 5. Re-create the original, per-label constraints needed for the build
        logger.info("Restoring original per-label constraints for the build process...")
        self.create_constraints()
        logger.info("Agent-facing schema removed successfully.")

    def add_agent_facing_schema(self):
        """
        Adds the agent-facing schema elements: synthetic IDs, ENTITY label,
        unified constraints, and unified vector index.
        """
        logger.info("Adding agent-facing schema...")
        # 1. Add synthetic IDs to nodes that don't have one
        self.add_synthetic_ids_if_missing()

        # 2. Add the ENTITY label to all nodes
        self.add_entity_label_to_all_nodes()

        # 3. Swap per-label ID constraints for a single ENTITY ID constraint
        self.migrate_per_label_id_to_global_id()

        # 4. Create the single, unified vector index on the ENTITY label, only if embeddings exist
        if self.check_property_exists('summaryEmbedding', labels=['ENTITY']):
            self.create_vector_indexes()
        else:
            logger.info("No 'summaryEmbedding' property found on ENTITY nodes. Skipping vector index creation.")
        
        logger.info("Agent-facing schema added successfully.")

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
        self.create_vector_indexes()

    def get_vector_indexes(self) -> dict:
        """Fetches the vector index for summary embeddings."""
        logger.info("Fetching vector index...")
        try:
            query = """
                SHOW INDEXES
                YIELD name, type, labelsOrTypes, state, properties
                WHERE type = 'VECTOR' AND state = 'ONLINE'
                RETURN name, labelsOrTypes
            """
            return self.execute_read_query(query)
        except Exception as e:
            logger.error(f"Error fetching vector index: {e}")
            return {"error": str(e)}

    def get_schema(self) -> dict:
        """Fetches the graph schema using APOC meta procedures."""
        logger.info("Fetching graph schema...")
        try:
            graph_meta_raw = self.execute_read_query("CALL apoc.meta.graph() YIELD nodes, relationships RETURN nodes, relationships")
            node_properties_meta = self.execute_read_query("CALL apoc.meta.schema()")
            
            graph_meta_nodes = graph_meta_raw[0]['nodes'] if graph_meta_raw else []
            graph_meta_relationships = graph_meta_raw[0]['relationships'] if graph_meta_raw else []

            return {
                "graph_meta": {
                    "nodes": graph_meta_nodes,
                    "relationships": graph_meta_relationships
                },
                "node_properties_meta": node_properties_meta
            }
        except Exception as e:
            logger.error(f"Failed to fetch schema. Ensure APOC plugin is installed. Error: {e}")
            return {"error": str(e)}

    def check_property_exists(self, property_key: str, labels: Optional[List[str]] = None) -> bool:
        """Checks if any node in the graph, optionally filtered by labels, has the given property."""
        if labels:
            label_selector = ":" + "|".join(labels)
            target_clause = f"n{label_selector}"
        else:
            target_clause = "n"

        query = f"MATCH ({target_clause}) WHERE n.{property_key} IS NOT NULL RETURN n LIMIT 1"
        try:
            result = self.execute_read_query(query)
            return bool(result)
        except Exception:
            return False

    def get_labels_without_id_property(self) -> set[str]:
        """Returns node labels that lack an 'id' property."""
        missing = set()
        schema = self.get_schema()
        meta = schema.get("node_properties_meta", [])
        if not meta:
            return missing

        value = meta[0].get("value", {})
        for label, entry in value.items():
            if entry.get("type") != "node":
                continue
            properties = entry.get("properties", {})
            if "id" not in properties:
                if "path" not in properties:
                    raise ValueError(f"Node label '{label}' lacks both 'id' and 'path'.")
                missing.add(label)
        return missing

    def add_synthetic_ids_if_missing(self) -> int:
        """Adds synthetic IDs to node labels that only have a 'path' property."""
        labels_missing_id = self.get_labels_without_id_property()
        total = 0
        with self.driver.session() as session:
            for label in labels_missing_id:
                query = f"""
                MATCH (n:{label})
                WHERE n.id IS NULL
                WITH n, "{label}://" + n.path AS full_path
                SET n.id = apoc.util.md5([full_path])
                RETURN count(n)
                """
                result = session.run(query)
                record = result.single()
                total += record.value() if record else 0
        return total

    def add_entity_label_to_all_nodes(self) -> int:
        """Applies the ENTITY label to all nodes in the graph."""
        with self.driver.session() as session:
            query = """
            CALL apoc.periodic.iterate(
                "MATCH (n) WHERE NOT n:ENTITY RETURN n",
                "SET n:ENTITY",
                {batchSize: 10000, parallel: true}
            )
            YIELD total RETURN total
            """
            result = session.run(query)
            record = result.single()
            return record['total'] if record else 0

    def migrate_per_label_id_to_global_id(self):
        """Switches from per-label unique ID constraints to a single global ENTITY ID constraint."""
        self.reset_constraints()
        constraints = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:FILE) REQUIRE f.path IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:FOLDER) REQUIRE f.path IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (e:ENTITY) REQUIRE e.id IS UNIQUE",
        ]
        with self.driver.session() as session:
            for constraint in constraints:
                session.run(constraint)
        logger.info("Shifted per-label id to global ENTITY id.")

    def format_schema_for_display(self, schema_info: dict, args=None) -> str:
        """Formats the schema into a human-readable (and agent-readable) string."""
        output_lines = []
        all_present_property_keys = set()
        output_only_relations = args.only_relations if args else False
        output_with_node_counts = args.with_node_counts if args else False

        output_lines.extend([
            "## Schema Notes:",
            "- All nodes have an 'ENTITY' label and a globally unique 'id' property.",
            "- Prefer using the specific semantic labels shown below.",
            ""
        ])

        output_lines.append("## Relationships:")
        grouped_relations = defaultdict(lambda: defaultdict(set))
        for rel_list_item in schema_info['graph_meta'].get("relationships", []):
            if isinstance(rel_list_item, (list, tuple)) and len(rel_list_item) == 3:
                start_label = rel_list_item[0].get('name', 'UNKNOWN')
                rel_type = rel_list_item[1]
                end_label = rel_list_item[2].get('name', 'UNKNOWN')
                grouped_relations[start_label][rel_type].add(end_label)

        node_counts = {n['name']: n.get('count', 0) for n in schema_info['graph_meta'].get("nodes", []) if n.get('name')}

        for start_label in sorted(grouped_relations.keys()):
            if start_label == 'ENTITY' or start_label.startswith('_'): continue
            for rel_type in sorted(grouped_relations[start_label].keys()):
                end_labels = sorted([l for l in grouped_relations[start_label][rel_type] if l != 'ENTITY' and not l.startswith('_')])
                if not end_labels: continue
                count_str = f" (count: {node_counts.get(start_label, 0)})" if output_with_node_counts else ""
                output_lines.append(f"  ({start_label}){count_str} -[:{rel_type}]-> ({'|'.join(end_labels)})")

        if not output_only_relations:
            output_lines.append("\n## Node Properties:")
            props_by_label = defaultdict(dict)
            apoc_meta = schema_info.get("node_properties_meta", [])
            if apoc_meta and "value" in apoc_meta[0]:
                for label, details in apoc_meta[0]["value"].items():
                    if details.get("type") == "node":
                        for pk, pd in details.get("properties", {}).items():
                            props_by_label[label][pk] = pd

            for label in sorted(props_by_label.keys()):
                if label == 'ENTITY' or label.startswith('_'): continue
                count_str = f" (count: {node_counts.get(label, 0)})" if output_with_node_counts else ""
                output_lines.append(f"  ({label}){count_str}")
                for pk in sorted(props_by_label[label].keys()):
                    pd = props_by_label[label][pk]
                    idx = " (INDEXED)" if pd.get("indexed") else ""
                    uni = " (UNIQUE)" if pd.get("unique") else ""
                    output_lines.append(f"    {pk}: {pd.get('type', 'unknown')}{idx}{uni}")
                    all_present_property_keys.add(pk)

        if not output_only_relations and all_present_property_keys:
            output_lines.append("\n## Property Explanations:")
            explanations = {
                "id": "Unique identifier for the node.",
                "name": "Name of the entity (e.g., function name).",
                "original_name": "Original source text from macro expansion.",
                "path": "Relative path to project root.",
                "name_location": "Start position: [line, column].",
                "body_location": "Body range: [start_line, start_column, end_line, end_column].",
                "code_hash": "MD5 of source code body.",
                "kind": "Type of symbol (e.g., Function, Struct).",
                "scope": "Visibility scope (e.g., global, static).",
                "language": "Programming language.",
                "type": "Data type (e.g., int, void*).",
                "return_type": "Function return type.",
                "signature": "Full signature.",
                "has_definition": "Boolean indicating if an implementation exists.",
                "macro_definition": "Source text of a #define.",
                "code_analysis": "Literal functionality analysis.",
                "summary": "Context-aware summary.",
                "summaryEmbedding": "Vector embedding for semantic search.",
            }
            for pk, expl in explanations.items():
                if pk in all_present_property_keys:
                    output_lines.append(f"  {pk}: {expl}")

        return "\n".join(output_lines)
