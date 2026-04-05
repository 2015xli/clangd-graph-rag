# 040-recursive-inheritance-sccs.md

**Goal**: Correctly summarize classes involved in inheritance cycles (self-recursion) by using a 2-step group analysis.

#### **1. Pass 3.5: Recursive Cycle Resolution**
*   **Detection (Cypher)**:
    ```cypher
    MATCH (c:CLASS_STRUCTURE)-[:INHERITS*1..]->(c)
    WITH DISTINCT c
    MATCH (c)-[:INHERITS*1..]->(p) WHERE (p)-[:INHERITS*1..]->(c)
    RETURN c.id AS seed_id, collect(DISTINCT p.id) AS scc_group
    ```
*   **Logic**: Process these groups **after** Pass 3.1 (DAG classes) but **before** Pass 4 (Namespaces).

#### **2. Two-Step SCC Processing**
*   **Step 1: Collective Logic Analysis**:
    *   Retrieve `body_location` code for **all** nodes in the SCC Group.
    *   Ask the LLM: *"These classes form a recursive structure. Explain the collective logic and termination conditions (if visible)."*
*   **Step 2: Individual Role Summarization**:
    *   For each member of the SCC Group:
        *   Provide the **Collective Summary** + its **Individual Body**.
        *   Ask the LLM: *"Given the collective recursive logic, what is the specific role of this individual node?"*
