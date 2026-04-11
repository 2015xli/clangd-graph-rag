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
        Orchestrates the process for generating class and data structure summaries.
        Uses a two-pass approach:
        1. Identify and summarize recursive SCC clusters (cycles + specializations).
        2. Assign standard inheritance levels to remaining nodes, seeded by SCC results.
        """
        if not class_ids:
            logger.info("No class or data structure IDs provided for summarization.")
            return set()

        # Step 1: Discover all relevant classes/data structures in the transitive closure
        query_all_relevant_ids = """
            MATCH (c:CLASS_STRUCTURE|DATA_STRUCTURE)
            WHERE c.id IN $target_class_ids
            OPTIONAL MATCH (c)-[:INHERITS*0..]->(ancestor:CLASS_STRUCTURE|DATA_STRUCTURE)
            RETURN DISTINCT ancestor.id AS id
        """
        result = self.neo4j_mgr.execute_read_query(query_all_relevant_ids, {"target_class_ids": list(class_ids)})
        all_relevant_ids = {r['id'] for r in result}
        if not all_relevant_ids:
            return set()

        all_updated_ids = set()
        scc_visited_ids = set()
        max_workers = self.num_local_workers if self.is_local_llm else self.num_remote_workers

        # Step 2: Pass 3.1 - Handle Strongly Connected Components (SCCs) for classes
        scc_clusters = self._identify_scc_clusters(all_relevant_ids)
        if scc_clusters:
            logger.info(f"Found {len(scc_clusters)} SCC clusters to process.")
            for cluster in scc_clusters:
                updated_cluster_ids = self._process_scc_cluster(cluster)
                all_updated_ids.update(updated_cluster_ids)
                scc_visited_ids.update(cluster) # All cluster nodes are now 'solved'

        # Step 3: Pass 3.2 - Assign levels to the remaining DAG portion, seeded by SCCs
        classes_by_level = self._get_classes_by_inheritance_level(all_relevant_ids, scc_visited_ids)
        
        for level in sorted(classes_by_level.keys()):
            level_node_ids = [c['id'] for c in classes_by_level[level]]
            if not level_node_ids:
                continue
            
            logger.info(f"Processing {len(level_node_ids)} structures at inheritance level {level}.")
            
            updated_ids_at_level = self._parallel_process(
                items=level_node_ids,
                process_func=self._process_one_class_summary,
                max_workers=max_workers,
                desc=f"Pass 3.2: Structural Summaries (Lvl {level})"
            )
            logger.info(f"Pass 3.2 (Lvl {level}): Structural Summaries - Updated {len(updated_ids_at_level)} nodes.")
            all_updated_ids.update(updated_ids_at_level)
            
        logger.info(f"Pass 3 (all stages): Structural Summaries - Updated {len(all_updated_ids)} total nodes.")
        return all_updated_ids

    def _identify_scc_clusters(self, all_relevant_ids: Set[str]) -> List[Set[str]]:
        """Groups cyclic nodes and their specializations into discrete clusters."""
        query = """
        // 1. Identify core cycle nodes
        MATCH (c:CLASS_STRUCTURE) WHERE c.id IN $all_relevant_ids
        MATCH (c)-[:INHERITS*1..]->(c)
        WITH DISTINCT c AS cycle_node
        
        // 2. Attach specializations (termination cases)
        OPTIONAL MATCH (spec:CLASS_STRUCTURE)-[:SPECIALIZATION_OF]->(cycle_node)
        RETURN cycle_node.id AS core_id, collect(DISTINCT spec.id) AS termination_ids
        """
        results = self.neo4j_mgr.execute_read_query(query, {"all_relevant_ids": list(all_relevant_ids)})
        
        # Merge overlapping results into discrete clusters
        clusters = []
        for r in results:
            new_set = {r['core_id']} | set(r['termination_ids'])
            # Check if this set overlaps with any existing cluster
            merged = False
            for existing in clusters:
                if not new_set.isdisjoint(existing):
                    existing.update(new_set)
                    merged = True
                    break
            if not merged:
                clusters.append(new_set)
        
        return clusters

    def _process_scc_cluster(self, cluster_ids: Set[str]) -> Set[str]:
        """Two-step summarization for a recursive cluster."""
        # 1. Fetch metadata for all members to enable collective analysis and cache checks
        metadata_query = """
        MATCH (c:CLASS_STRUCTURE)
        WHERE c.id IN $ids
        RETURN c as node
        """
        cluster_ids = sorted(list(cluster_ids)) 
        cluster_result = self.neo4j_mgr.execute_read_query(metadata_query, {"ids": cluster_ids})
        if not cluster_result:
            return set()
        cluster_metadata = [dict(record['node']) for record in cluster_result]

        # 2. Step A: Collective Analysis
        status, data = self.node_processor.get_scc_group_analysis(cluster_metadata)
        
        if status == "generation_failed":
            logger.error(f"Collective analysis failed for cluster: {cluster_ids}")
            return set()

        scc_id = "SCC:" + ",".join(cluster_ids)
        # reduction phase for the cluster itself (main thread serial update)
        if status in ["summary_regenerated", "summary_restored"]:
            self.summary_cache_manager.update_cache_entry("SCC_GROUP", scc_id, data)
            self.summary_cache_manager.set_runtime_status("SCC_GROUP", scc_id, "visited")                      
            if status == "summary_regenerated":                                                                
                self.summary_cache_manager.set_runtime_status("SCC_GROUP", scc_id, "summary_changed")  

        logger.info(f"Successfully processed a {len(cluster_ids)} nodes SCC.")
        collective_analysis = data.get('group_analysis')
        
        # 3. Step B: Individual Summaries
        updated_ids = set()        
        for class_id in cluster_ids:
            # We call the existing worker but pass the collective context.
            result = self._process_one_class_summary(
                class_id, 
                scc_context=collective_analysis
            )
            
            # Reduce phase for individual nodes (main thread serial update)
            if result:
                self.summary_cache_manager.update_cache_entry(result['label'], result['key'], result['data'])
                self.summary_cache_manager.set_runtime_status(result['label'], result['key'], "visited")
                if result['status'] in ["summary_regenerated", "summary_restored"]:
                    updated_ids.add(class_id)
                    if result['status'] == "summary_regenerated":
                        self.summary_cache_manager.set_runtime_status(result['label'], result['key'], "summary_changed")
                        self.n_generated += 1
                    else:
                        self.n_restored += 1
                elif result['status'] == "unchanged":
                    self.n_unchanged += 1
                elif result['status'] == "generation_failed":
                    self.n_failed += 1
        
        logger.info(f"Successfully processed the {len(cluster_ids)} classes in the SCC: {cluster_ids}")
        return updated_ids

    def _get_classes_by_inheritance_level(self, all_relevant_ids: Set[str], initial_visited: Set[str]) -> Dict[int, List[Dict]]:
        """Groups classes and data structures by their position in the inheritance hierarchy."""
        classes_by_level = defaultdict(list)
        visited_ids = set(initial_visited)
        current_level = 0

        # Level 0: Nodes with no parents OR all parents in initial_visited
        level_0_query = """
            MATCH (c)
            WHERE c.id IN $all_relevant_ids AND NOT (c.id IN $visited_ids) AND (c:CLASS_STRUCTURE OR c:DATA_STRUCTURE)
            WITH c
            OPTIONAL MATCH (c)-[:INHERITS]->(p:CLASS_STRUCTURE)
            WITH c, collect(p.id) AS parent_ids
            WHERE size(parent_ids) = 0 OR all(pid IN parent_ids WHERE pid IN $visited_ids)
            RETURN collect({id: c.id, name: c.name}) AS classes
        """
        result = self.neo4j_mgr.execute_read_query(level_0_query, {"all_relevant_ids": list(all_relevant_ids), "visited_ids": list(visited_ids)})
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

    def _process_one_class_summary(self, class_id: str, scc_context: Optional[str] = None) -> dict:
        """Worker function for processing a single class or data structure summary."""
        context_query = """
        MATCH (c) WHERE c.id = $id AND (c:CLASS_STRUCTURE OR c:DATA_STRUCTURE)
        OPTIONAL MATCH (c)-[:INHERITS]->(p:CLASS_STRUCTURE) WHERE p.summary IS NOT NULL
        OPTIONAL MATCH (c)-[:HAS_METHOD]->(m:METHOD) WHERE m.summary IS NOT NULL
        OPTIONAL MATCH (c)-[:HAS_FIELD]->(f:FIELD)
        RETURN c as node, labels(c) as n_labels,
               collect(DISTINCT {id: p.id, label: 'CLASS_STRUCTURE'}) AS parents,
               collect(DISTINCT {id: m.id, label: 'METHOD'}) AS methods,
               collect(DISTINCT {name: f.name, type: f.type}) as fields
        """
        context_results = self.neo4j_mgr.execute_read_query(context_query, {"id": class_id})
        if not context_results or not context_results[0]['node']:
            logger.warning(f"Could not find structure with ID {class_id} for summary.")
            return None

        record = context_results[0]
        node_data = dict(record['node'])
        node_label = [l for l in record['n_labels'] if l in ['CLASS_STRUCTURE', 'DATA_STRUCTURE']][0]
        node_data['label'] = node_label
        
        parent_entities = [p for p in record.get('parents', []) if p and p['id']]
        method_entities = [m for m in record.get('methods', []) if m and m['id']]
        field_entities = [f for f in record.get('fields', []) if f and f['name']]

        status, data = self.node_processor.get_class_summary(
            node_data, parent_entities, method_entities, field_entities, scc_context=scc_context
        )

        if status in ["summary_regenerated", "summary_restored"]:
            update_query = f"""
            MATCH (n:{node_label} {{id: $id}}) 
            SET n.summary = $summary, n.code_hash = $code_hash, n.group_analysis = $group_analysis
            REMOVE n.summaryEmbedding
            """
            self.neo4j_mgr.execute_autocommit_query(
                update_query,
                {
                    "id": class_id, 
                    "summary": data["summary"], 
                    "code_hash": data.get("code_hash"),
                    "group_analysis": scc_context or node_data.get('group_analysis')
                }
            )
        
        return {
            "key": class_id,
            "label": node_label,
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
        OPTIONAL MATCH (ns)-[:SCOPE_CONTAINS|DEFINES_TYPE_ALIAS]->(child)
        RETURN ns as node,
           collect(DISTINCT {
               id: child.id, 
               labels: labels(child), 
               name: child.name,
               aliased_canonical_spelling: child.aliased_canonical_spelling
           }) as children
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
            child['label'] = [l for l in child['labels'] if l in ['NAMESPACE', 'CLASS_STRUCTURE', 'DATA_STRUCTURE', 'FUNCTION', 'VARIABLE', 'TYPE_ALIAS']][0]

        status, data = self.node_processor.get_namespace_summary(
            node_data, child_entities
        )

        if status in ["summary_regenerated", "summary_restored"]:
            # Build the SET clause dynamically to handle optional code_hash
            set_clauses = ["n.summary = $summary"]
            params = {"id": namespace_id, "summary": data["summary"]}
            
            if data.get("code_hash"):
                set_clauses.append("n.code_hash = $code_hash")
                params["code_hash"] = data["code_hash"]
            
            update_query = f"MATCH (n:NAMESPACE {{id: $id}}) SET {', '.join(set_clauses)} REMOVE n.summaryEmbedding"
            self.neo4j_mgr.execute_autocommit_query(update_query, params)
        
        return {
            "key": namespace_id,
            "label": node_data["label"],
            "status": status,
            "data": data
        }
