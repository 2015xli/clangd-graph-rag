# 040-recursive-inheritance-sccs.md

**Goal**: Correctly summarize inheritance cycles (recursion) and ensure they provide the necessary context to seed the subsequent DAG-based class roll-up, while maintaining robust physical staleness tracking using a nested waterfall hashing strategy.

### **The Architecture: SCC-First Seeding**
The summarizer will solve recursion **before** the standard inheritance-level pass. This prevents the BFS logic from "stalling" on cyclic dependencies.

---

### **Step 1: Scope Closure (Discovery)**
**Operation**: Identify all classes in the transitive inheritance closure of the target IDs.
**Query**:
```cypher
MATCH (c:CLASS_STRUCTURE) WHERE c.id IN $target_ids
OPTIONAL MATCH (c)-[:INHERITS*0..]->(ancestor:CLASS_STRUCTURE)
RETURN DISTINCT ancestor.id AS id
```
**Why**: Ensures the complete inheritance boundary is known for cycle detection and consistent roll-up.

---

### **Step 2: Termination-Aware SCC Discovery**
**Operation**: Group nodes into SCC Clusters consisting of "Core Cycle Nodes" and their "Termination Specializations."
**Query**:
```cypher
// 1. Identify core cycle nodes
MATCH (c:CLASS_STRUCTURE) WHERE c.id IN $all_relevant_ids
MATCH (c)-[:INHERITS*1..]->(c)
WITH DISTINCT c AS cycle_node

// 2. Attach specializations (termination base-cases)
OPTIONAL MATCH (spec:CLASS_STRUCTURE)-[:SPECIALIZATION_OF]->(cycle_node)
RETURN cycle_node.id AS core_id, collect(DISTINCT spec.id) AS termination_ids
```
**Why**: Collects both the recursive logic and its exit conditions to give the LLM self-sufficient context.

---

### **Step 3: Cluster-Level Cache & Waterfall Group Hashing**
**Operation**: Use a three-stage waterfall to validate the collective analysis of the cluster.

1.  **Stage 1: Physical Hashing**: 
    *   Read the actual source code for every cluster member.
    *   Calculate `current_member_hash` (SHA1 of body or Node ID if no body).
    *   Compute **`new_group_hash`** = `Hash(sorted_current_member_hashes)`.
2.  **Stage 2: Database Check**:
    *   Fetch the `code_hash` property from Neo4j for every member.
    *   Compute **`db_group_hash`** = `Hash(sorted_db_member_hashes)`.
    *   Check: If `new_group_hash == db_group_hash`, the cluster is physically up-to-date in the DB.
3.  **Stage 3: Cache Check**:
    *   Read the L1 cache for `SCC_CLUSTER:{sorted_ids}`.
    *   Extract **`cache_group_hash`**.
    *   Check: If `new_group_hash == cache_group_hash`, restore the analysis from cache.
4.  **Regeneration**: If all stages fail, trigger the LLM for `collective_analysis`.

**Why**: This mirrors the individual class logic. It ensures that the cluster's collective logic is only re-evaluated if the underlying physical code has changed, prioritizing restoration from cache whenever possible.

---

### **Step 4: Individual Member Summaries & Hashing Sync**
**Operation**: Generate individual summaries for every cluster member.
1.  **Summarize**: Inject the SCC Collective Analysis into the manifest prompt.
2.  **Synchronize**: 
    *   Update Neo4j: `SET n.summary = $summary, n.code_hash = $current_member_hash`.
    *   Update L1 Cache: Sync the node's individual `code_hash`.

**Why**: Solves the "Chicken and Egg" problem by ensuring that once a cluster is processed, all its members have their physical `code_hash` persisted in the DB for subsequent DAG BFS logic.

---

### **Step 5: Seeded DAG BFS (The "Residue" Pass)**
**Operation**: Execute the standard BFS level logic, seeding the "Visited" set with the Cluster IDs.
**Modified Level 0 Logic**:
```cypher
MATCH (c:CLASS_STRUCTURE)
WHERE c.id IN $all_relevant_ids 
  AND NOT (c.id IN $scc_visited_ids)
  AND (
    NOT (c)-[:INHERITS]->(:CLASS_STRUCTURE)
    OR all(p IN [(c)-[:INHERITS]->(parent) | parent.id] WHERE p IN $scc_visited_ids)
  )
RETURN collect({id: c.id, name: c.name}) AS classes
```
**Why**: "Unlocks" nodes that inherit from the solved SCC clusters.

---

### **Step 6: Standard Level-by-Level Roll-up**
**Operation**: Summarize all remaining DAG nodes level-by-level.
**Why**: Completes the hierarchy with 100% coverage.
