#!/usr/bin/env python3
"""
Mixin for class and namespace summarization.
Handles complex hierarchies like inheritance and nesting.
"""

import logging
from typing import Set, List, Dict, Optional
from collections import defaultdict

# Set up logging for this module
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class ScopeProcessorMixin:
    """
    Encapsulates logic for generating summaries for logical structures
    like CLASSES and NAMESPACES.
    """

    def summarize_classes_with_ids(self, class_ids: Set[str]) -> Set[str]:
        """
        Orchestrates the process for generating class summaries by inheritance level.
        """
        if not class_ids:
            logger.info("No class IDs provided for summarization.")
            return set()

        logger.info(f"Processing summaries for {len(class_ids)} candidate classes by inheritance level.")
        classes_by_level = self._get_classes_by_inheritance_level(class_ids)
        if not classes_by_level:
            logger.info("No class structures found to summarize.")
            return set()

        all_updated_ids = set()
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers

        for level in sorted(classes_by_level.keys()):
            level_class_ids = [c['id'] for c in classes_by_level[level]]
            if not level_class_ids:
                continue
            
            logger.info(f"Processing {len(level_class_ids)} classes at inheritance level {level}.")
            
            updated_ids_at_level = self._parallel_process(
                items=level_class_ids,
                process_func=self._process_one_class_summary,
                max_workers=max_workers,
                desc=f"Pass 3: Class Summaries (Lvl {level})"
            )
            logger.info(f"Pass 3 (Lvl {level}): Class Summaries - Updated {len(updated_ids_at_level)} nodes.")
            all_updated_ids.update(updated_ids_at_level)
            
        logger.info(f"Pass 3 (all levels): Class Summaries - Updated {len(all_updated_ids)} total nodes.")
        return all_updated_ids

    def _get_classes_by_inheritance_level(self, target_class_ids: Optional[Set[str]] = None) -> Dict[int, List[Dict]]:
        """Groups classes by their position in the inheritance hierarchy."""
        classes_by_level = defaultdict(list)
        visited_ids = set()
        current_level = 0

        if target_class_ids:
            query_all_relevant_ids = """
                MATCH (c:CLASS_STRUCTURE)
                WHERE c.id IN $target_class_ids
                OPTIONAL MATCH (c)-[:INHERITS*0..]->(ancestor:CLASS_STRUCTURE)
                RETURN DISTINCT ancestor.id AS id
            """
            result = self.neo4j_mgr.execute_read_query(query_all_relevant_ids, {"target_class_ids": list(target_class_ids)})
            all_relevant_ids = {r['id'] for r in result}
            if not all_relevant_ids:
                return {}
        else:
            query_all_ids = "MATCH (c:CLASS_STRUCTURE) RETURN c.id AS id"
            result = self.neo4j_mgr.execute_read_query(query_all_ids)
            all_relevant_ids = {r['id'] for r in result}
            if not all_relevant_ids:
                return {}

        level_0_query = """
            MATCH (c:CLASS_STRUCTURE)
            WHERE c.id IN $all_relevant_ids AND NOT (c)-[:INHERITS]->(:CLASS_STRUCTURE)
            RETURN collect({id: c.id, name: c.name}) AS classes
        """
        result = self.neo4j_mgr.execute_read_query(level_0_query, {"all_relevant_ids": list(all_relevant_ids)})
        level_nodes = result[0]['classes'] if result and result[0]['classes'] else []
        
        if not level_nodes:
            return {}

        classes_by_level[current_level] = level_nodes
        visited_ids.update(n['id'] for n in level_nodes)

        while level_nodes:
            current_level += 1
            next_level_query = """
                MATCH (c:CLASS_STRUCTURE)
                WHERE (c.id IN $all_relevant_ids) AND NOT (c.id IN $visited_ids)
                WITH c
                MATCH (c)-[:INHERITS]->(p:CLASS_STRUCTURE)
                WITH c, collect(p.id) AS parent_ids
                WHERE size(parent_ids) > 0 AND all(pid IN parent_ids WHERE pid IN $visited_ids)
                RETURN collect({id: c.id, name: c.name}) AS classes
            """
            result = self.neo4j_mgr.execute_read_query(
                next_level_query, 
                {"all_relevant_ids": list(all_relevant_ids), "visited_ids": list(visited_ids)}
            )
            level_nodes = result[0]['classes'] if result and result[0]['classes'] else []

            if level_nodes:
                classes_by_level[current_level] = level_nodes
                visited_ids.update(n['id'] for n in level_nodes)
        
        return classes_by_level

    def _process_one_class_summary(self, class_id: str) -> dict:
        """Worker function for processing a single class summary."""
        context_query = """
        MATCH (c:CLASS_STRUCTURE {id: $id})
        OPTIONAL MATCH (c)-[:INHERITS]->(p:CLASS_STRUCTURE) WHERE p.summary IS NOT NULL
        OPTIONAL MATCH (c)-[:HAS_METHOD]->(m:METHOD) WHERE m.summary IS NOT NULL
        OPTIONAL MATCH (c)-[:HAS_FIELD]->(f:FIELD)
        RETURN c as node,
               collect(DISTINCT {id: p.id, label: 'CLASS_STRUCTURE'}) AS parents,
               collect(DISTINCT {id: m.id, label: 'METHOD'}) AS methods,
               collect(DISTINCT {name: f.name, type: f.type}) as fields
        """
        context_results = self.neo4j_mgr.execute_read_query(context_query, {"id": class_id})
        if not context_results or not context_results[0]['node']:
            logger.warning(f"Could not find class with ID {class_id} for summary.")
            return None

        record = context_results[0]
        node_data = dict(record['node'])
        node_data['label'] = 'CLASS_STRUCTURE'
        
        parent_entities = [p for p in record.get('parents', []) if p and p['id']]
        method_entities = [m for m in record.get('methods', []) if m and m['id']]
        field_entities = [f for f in record.get('fields', []) if f and f['name']]

        status, data = self.node_processor.get_class_summary(
            node_data, parent_entities, method_entities, field_entities
        )

        if status in ["summary_regenerated", "summary_restored"]:
            update_query = "MATCH (n:CLASS_STRUCTURE {id: $id}) SET n.summary = $summary REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(
                update_query,
                {"id": class_id, "summary": data["summary"]}
            )
        
        return {
            "key": class_id,
            "label": node_data["label"],
            "status": status,
            "data": data
        }

    def summarize_namespaces_with_ids(self, namespace_ids: Set[str]) -> Set[str]:
        """Orchestrates the map-reduce process for generating namespace summaries."""
        if not namespace_ids:
            logger.info("No namespace IDs provided for summarization.")
            return set()

        logger.info(f"Processing summaries for {len(namespace_ids)} candidate namespaces by nesting depth.")
        namespaces_by_depth = self._get_namespaces_by_depth(namespace_ids)
        if not namespaces_by_depth:
            logger.info("No namespaces found to summarize.")
            return set()

        all_updated_ids = set()
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers

        for depth in sorted(namespaces_by_depth.keys(), reverse=True):
            level_namespace_ids = [ns['id'] for ns in namespaces_by_depth[depth]]
            if not level_namespace_ids:
                continue
            
            logger.info(f"Processing {len(level_namespace_ids)} namespaces at depth {depth}.")
            
            updated_ids_at_level = self._parallel_process(
                items=level_namespace_ids,
                process_func=self._process_one_namespace_summary,
                max_workers=max_workers,
                desc=f"Pass 4: Namespace Summaries (Depth {depth})"
            )
            logger.info(f"Pass 4 (Depth {depth}): Namespace Summaries - Updated {len(updated_ids_at_level)} nodes.")
            all_updated_ids.update(updated_ids_at_level)
            
        logger.info(f"Pass 4 (all depths): Namespace Summaries - Updated {len(all_updated_ids)} nodes.")
        return all_updated_ids

    def _get_namespaces_by_depth(self, namespace_ids: Set[str]) -> Dict[int, List[Dict]]:
          """Groups namespaces by their nesting depth based on qualified name."""
          if not namespace_ids:
              return {}
  
          namespaces_by_depth = defaultdict(list)
          query = """ 
          MATCH (n:NAMESPACE)
          WHERE n.id IN $namespace_ids
          RETURN n.id as id, n.qualified_name AS qualified_name, n.name AS name
          """
          results = self.neo4j_mgr.execute_read_query(query, {"namespace_ids": list(namespace_ids)})
  
          for ns in results:
              depth = ns['qualified_name'].count('::')
              namespaces_by_depth[depth].append(ns)
      
          return namespaces_by_depth

    def _process_one_namespace_summary(self, namespace_id: str) -> dict:
        """Worker function for processing a single namespace summary."""
        context_query = """
        MATCH (ns:NAMESPACE {id: $id})
        OPTIONAL MATCH (ns)-[:SCOPE_CONTAINS]->(child)
        WHERE child.summary IS NOT NULL
        RETURN ns as node,
               collect(DISTINCT {id: child.id, labels: labels(child), name: child.name}) as children
        """
        context_results = self.neo4j_mgr.execute_read_query(context_query, {"id": namespace_id})
        if not context_results or not context_results[0]['node']:
            logger.warning(f"Could not find namespace with ID {namespace_id} for summary.")
            return None

        record = context_results[0]
        node_data = dict(record['node'])
        node_data['label'] = 'NAMESPACE'
        
        child_entities = [c for c in record.get('children', []) if c and c['id']]

        for child in child_entities:
            child['label'] = [l for l in child['labels'] if l in ['NAMESPACE', 'CLASS_STRUCTURE', 'DATA_STRUCTURE', 'FUNCTION', 'VARIABLE']][0]

        status, data = self.node_processor.get_namespace_summary(
            node_data, child_entities
        )

        if status in ["summary_regenerated", "summary_restored"]:
            update_query = "MATCH (n:NAMESPACE {id: $id}) SET n.summary = $summary REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(
                update_query,
                {"id": namespace_id, "summary": data["summary"]}
            )
        
        return {
            "key": namespace_id,
            "label": node_data["label"],
            "status": status,
            "data": data
        }
