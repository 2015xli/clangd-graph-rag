import logging
import json
from typing import List, Dict, Tuple, Optional, Any
from collections import defaultdict
from tqdm import tqdm
from utils import align_string

logger = logging.getLogger(__name__)

class SchemaMixin:
    """Methods for database structure, constraints, and schema management."""

    def setup_database(self, project_path: str, init_property: Dict[str, Any]) -> None:
        self.reset_database()
        self.reset_vector_indexes()
        self.reset_constraints()
        self.update_project_node(project_path, init_property)
        self.create_constraints()

    def reset_database(self) -> None:
        with self.driver.session() as session:
            logger.info("Deleting existing data...")
            session.run("MATCH (n) DETACH DELETE n")
            logger.info("Database cleared.")

    def reset_vector_indexes(self):
        with self.driver.session() as session:
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
        with self.driver.session() as session:
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
        with self.driver.session() as session:
            for constraint in constraints:
                session.run(constraint)
    
    def bootstrap_schema(self) -> None:
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

    def wrapup_graph(self, keep_orphans: bool):
        if not keep_orphans:
            deleted_nodes_count = self.cleanup_orphan_nodes()
            logger.info(f"Removed {deleted_nodes_count} orphan nodes.")
        else:
            logger.info("Skipping cleanup of orphan nodes as requested.")
        logger.info(f"Total nodes in graph: {self.total_nodes_in_graph()}")
        logger.info(f"Total relationships in graph: {self.total_relationships_in_graph()}")

    def create_vector_indexes(self) -> None:
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
        logger.info("Removing agent-facing schema additions...")
        self.reset_vector_indexes()
        self.reset_constraints()
        with self.driver.session() as session:
            id_removal_query = """
            CALL apoc.periodic.iterate(
                "MATCH (n) WHERE n:FILE OR n:FOLDER RETURN n",
                "REMOVE n.id",
                {batchSize: 10000, parallel: true}
            )
            """
            session.run(id_removal_query)
            label_removal_query = """
            CALL apoc.periodic.iterate(
                "MATCH (e:ENTITY) RETURN e",
                "REMOVE e:ENTITY",
                {batchSize: 10000, parallel: true}
            )
            """
            session.run(label_removal_query)
        self.create_constraints()
        logger.info("Agent-facing schema removed successfully.")

    def add_agent_facing_schema(self):
        logger.info("Adding agent-facing schema...")
        self.add_synthetic_ids_if_missing()
        self.add_entity_label_to_all_nodes()
        self.migrate_per_label_id_to_global_id()
        if self.check_property_exists('summaryEmbedding', labels=['ENTITY']):
            self.create_vector_indexes()
        logger.info("Agent-facing schema added successfully.")

    def drop_vector_indices(self) -> None:
        logger.info("Dropping existing vector indices...")
        existing_indices = self.execute_read_query("SHOW VECTOR INDEXES")
        with self.driver.session() as session:
            for index_info in existing_indices:
                if index_info.get("name", "").endswith("_summary_embeddings"):
                    index_name = index_info["name"]
                    try:
                        session.run(f"DROP INDEX {index_name}")
                        logger.info(f"Dropped vector index: {index_name}")
                    except Exception as e:
                        logger.warning(f"Could not drop vector index {index_name}. Error: {e}")

    def rebuild_vector_indices(self) -> None:
        self.drop_vector_indices()
        self.create_vector_indexes()

    def get_vector_indexes(self) -> dict:
        try:
            query = "SHOW INDEXES YIELD name, type, labelsOrTypes, state, properties WHERE type = 'VECTOR' AND state = 'ONLINE' RETURN name, labelsOrTypes"
            return self.execute_read_query(query)
        except Exception as e:
            logger.error(f"Error fetching vector index: {e}")
            return {"error": str(e)}

    def get_schema(self) -> dict:
        try:
            graph_meta_raw = self.execute_read_query("CALL apoc.meta.graph() YIELD nodes, relationships RETURN nodes, relationships")
            node_properties_meta = self.execute_read_query("CALL apoc.meta.schema()")
            return {
                "graph_meta": {"nodes": graph_meta_raw[0]['nodes'] if graph_meta_raw else [], "relationships": graph_meta_raw[0]['relationships'] if graph_meta_raw else []},
                "node_properties_meta": node_properties_meta
            }
        except Exception as e:
            logger.error(f"Failed to fetch schema. Error: {e}")
            return {"error": str(e)}

    def check_property_exists(self, property_key: str, labels: Optional[List[str]] = None) -> bool:
        target_clause = f"n:{'|'.join(labels)}" if labels else "n"
        query = f"MATCH ({target_clause}) WHERE n.{property_key} IS NOT NULL RETURN n LIMIT 1"
        try:
            result = self.execute_read_query(query)
            return bool(result)
        except Exception as e:
            logger.warning(f"Query for property '{property_key}' failed: {e}")
            return False

    def get_labels_without_id_property(self) -> set[str]:
        missing = set()
        meta = self.get_schema().get("node_properties_meta", [])
        if not meta: return missing
        value = meta[0].get("value", {})
        for label, entry in value.items():
            if entry.get("type") == "node":
                properties = entry.get("properties", {})
                if "id" not in properties:
                    if "path" not in properties:
                        raise ValueError(f"Node label '{label}' lacks 'id' or 'path'.")
                    missing.add(label)
        return missing

    def add_synthetic_ids_if_missing(self) -> int:
        labels_missing_id = self.get_labels_without_id_property()
        total = 0
        with self.driver.session() as session:
            for label in labels_missing_id:
                query = f"MATCH (n:{label}) WHERE n.id IS NULL WITH n, \"{label}://\" + n.path AS full_path SET n.id = apoc.util.md5([full_path]) RETURN count(n)"
                result = session.run(query)
                total += result.single().value()
        return total

    def add_entity_label_to_all_nodes(self) -> int:
        with self.driver.session() as session:
            query = "CALL apoc.periodic.iterate(\"MATCH (n) WHERE NOT n:ENTITY RETURN n\", \"SET n:ENTITY\", {batchSize: 10000, parallel: true}) YIELD total RETURN total"
            result = session.run(query)
            return result.single().value()

    def migrate_per_label_id_to_global_id(self):
        self.reset_constraints()
        constraints = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:FILE) REQUIRE f.path IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:FOLDER) REQUIRE f.path IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (e:ENTITY) REQUIRE e.id IS UNIQUE",
        ]
        with self.driver.session() as session:
            for constraint in constraints:
                session.run(constraint)

    def format_schema_for_display(self, schema_info: dict, args=None) -> str:
        output_lines = []
        all_present_property_keys = set()
        output_only_relations = args.only_relations if args else False
        output_with_node_counts = args.with_node_counts if args else False

        schema_notes = ["## Schema Notes:", "- All nodes have an 'ENTITY' label and a globally unique 'id' property.", "- Prefer using the specific semantic labels shown below.", ""]
        output_lines.extend(schema_notes)
        output_lines.append("## Relationships:")
        
        grouped_relations = defaultdict(lambda: defaultdict(set))
        for rel_list_item in schema_info['graph_meta'].get("relationships", []):
            if isinstance(rel_list_item, (list, tuple)) and len(rel_list_item) == 3:
                start_label = rel_list_item[0].get('name', 'UNKNOWN')
                end_label = rel_list_item[2].get('name', 'UNKNOWN')
                grouped_relations[start_label][rel_list_item[1]].add(end_label)

        node_counts = {node_obj['name']: node_obj.get('count', 0) for node_obj in schema_info['graph_meta'].get("nodes", []) if node_obj.get('name')}

        for start_label in sorted(grouped_relations.keys()):
            if start_label == 'ENTITY' or start_label.startswith('_'): continue
            for rel_type in sorted(grouped_relations[start_label].keys()):
                end_labels = sorted([lbl for lbl in grouped_relations[start_label][rel_type] if lbl != 'ENTITY' and not lbl.startswith('_')])
                if not end_labels: continue
                count_str = f" (count: {node_counts.get(start_label, 0)})" if output_with_node_counts else ""
                output_lines.append(f"  ({start_label}){count_str} -[:{rel_type}]-> ({'|'.join(end_labels)})")

        if not output_only_relations:
            output_lines.append("\n## Node Properties:")
            props_by_label = defaultdict(dict)
            apoc_schema_data = schema_info.get("node_properties_meta", [])
            if apoc_schema_data and "value" in apoc_schema_data[0]:
                for label, details in apoc_schema_data[0]["value"].items():
                    if details.get("type") == "node":
                        for prop_key, prop_details in details.get("properties", {}).items():
                            props_by_label[label][prop_key] = prop_details

            for label in sorted(props_by_label.keys()):
                if label == 'ENTITY' or label.startswith('_'): continue
                count_str = f" (count: {node_counts.get(label, 0)})" if output_with_node_counts else ""
                output_lines.append(f"  ({label}){count_str}")
                for prop_key in sorted(props_by_label[label].keys()):
                    prop_details = props_by_label[label][prop_key]
                    output_lines.append(f"    {prop_key}: {prop_details.get('type', 'unknown')}{' (INDEXED)' if prop_details.get('indexed') else ''}{' (UNIQUE)' if prop_details.get('unique') else ''}")
                    all_present_property_keys.add(prop_key)

        if not output_only_relations and all_present_property_keys:
            output_lines.append("\n## Property Explanations:")
            explanations = {
                "id": "Unique identifier for the node.", "name": "Name of the entity.", "path": "Relative path to project root.",
                "code_hash": "MD5 hash of source code body.", "kind": "Type of symbol.", "summary": "AI summary.",
                "summaryEmbedding": "Vector embedding of summary."
            }
            for prop_key, explanation in explanations.items():
                if prop_key in all_present_property_keys:
                    output_lines.append(f"  {prop_key}: {explanation}")
        return "\n".join(output_lines)
