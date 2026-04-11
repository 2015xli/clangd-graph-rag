# Recursive Inheritance & SCC Handling

C++ template metaprogramming often involves recursive inheritance (e.g., `template<N> struct A : A<N-1>`). Standard BFS-based level logic fails on these structures because cyclic dependencies can never satisfy the "all parents visited" condition. The `summary_engine` solves this by identifying and resolving Strongly Connected Components (SCCs) before the standard inheritance-level pass.

## 1. Termination-Aware SCC Discovery
The engine identifies clusters consisting of **Core Cycle Nodes** and their **Termination Specializations**.

### 1.1 Discovery Query
```cypher
// 1. Identify core cycle nodes (self-loops or mutual recursion)
MATCH (c:CLASS_STRUCTURE) WHERE c.id IN $all_relevant_ids
MATCH (c)-[:INHERITS*1..]->(c)
WITH DISTINCT c AS cycle_node

// 2. Attach specializations (the base-case/exit for recursion)
OPTIONAL MATCH (spec:CLASS_STRUCTURE)-[:SPECIALIZATION_OF]->(cycle_node)
RETURN cycle_node.id AS core_id, collect(DISTINCT spec.id) AS termination_ids
```
**Rationale**: A recursive template cannot be semantically understood without its exit condition (specialization). By grouping the base-case with the recursive step, the LLM receives self-sufficient context to explain the recursion's logic and purpose.

## 2. Virtual Group Hashing
SCC clusters are virtual entities whose staleness is tracked via a composite **Virtual Group Hash**.

### 2.1 The SCC Waterfall Check
Before triggering an LLM call, the engine performs a three-stage validation:
1.  **Physical State**: The engine reads the current source code for every cluster member and calculates their current hashes. 
    *   `new_group_hash = Hash(sorted_current_member_hashes)`
2.  **Database Check**: It fetches the `code_hash` property from Neo4j for every member.
    *   `db_group_hash = Hash(sorted_db_member_hashes)`
    *   If `new_group_hash == db_group_hash` AND all members share a consistent `group_analysis`, the cluster is considered unchanged in the DB.
3.  **Cache Check**: It checks the L1 cache for an entry `SCC_CLUSTER:{sorted_member_ids}`.
    *   If `cache_group_hash == new_group_hash`, the analysis is restored from the cache.

## 3. Persistent Collective Logic
Every node in an SCC cluster stores a copy of the **`group_analysis`** property in Neo4j.
*   **Why**: This ensures the collective recursive logic is visible both in the graph and to AI agents. By reading any single class in the cycle, the agent understands the collective recursive purpose.
*   **Context Priority**: During individual class summarization, the `group_analysis` is explicitly injected into the prompt as "Recursive Context," providing higher accuracy than a simple child roll-up.

## 4. Two-Step SCC Process
1.  **Step A: Collective Analysis**: LLM analyzes the cluster's collective logic based on the combined bodies of all members.
2.  **Step B: Individual Role**: For each member, the LLM generates a specific summary using the collective analysis as context. This distinguishes the role of the "Recursive Step" from the "Base Case."

## 5. Seeding the DAG
Once an SCC cluster is resolved, its members are added to the `visited_ids` set. This "unlocks" the standard BFS logic for any downstream classes that inherit from the recursive structure, ensuring 100% coverage of the inheritance hierarchy.
