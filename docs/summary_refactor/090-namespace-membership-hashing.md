# 090-namespace-membership-hashing.md

**Goal**: Implement membership-based hashing for namespaces to detect logical changes and enrich the context with Type Alias details.

---

### **Step 1: Membership Hashing (`node_summarizer.py`)**
**Operation**: Calculate a `code_hash` for the namespace based on its logical contents.
1.  **Identity Construction**: For every member in the child inventory, construct an identity string: `"{id}:{name}:{aliased_canonical_spelling}"`.
2.  **Aggregation**: Sort the identity strings and compute a SHA1/MD5 digest of the joined list.
**Rationale**: This ensures that adding, removing, or renaming a namespace member (even an unsummarized one like a Variable) triggers a staleness check.

---

### **Step 2: Type Alias Enrichment (`scope_processor.py`)**
**Operation**: Fetch the "Ground Truth" for Type Aliases within the namespace.
1.  **Query**: Update the context query to match `[:SCOPE_CONTAINS|DEFINES_TYPE_ALIAS]`.
2.  **Metadata**: Retrieve `child.aliased_canonical_spelling` for each child.
3.  **Manifest Prompt**: Use this metadata to enrich the inventory: e.g., `"- TypeAlias 'Handle' (alias of 'void*')"`.

---

### **Step 3: Three-Stage Waterfall Logic**
**Operation**: Consistently apply the DB -> Cache -> LLM priority.
1.  **DB Check**: Compare `current_code_hash` with the `code_hash` property on the Neo4j `NAMESPACE` node.
2.  **Cache Check**: If DB is stale, check if the L1 cache contains a matching entry.
3.  **Synchronization**: The orchestrator persists the `code_hash` back to Neo4j upon successful summarization.

---

### **Step 4: Logical Synchronization**
*   **Consistency**: Brings `NAMESPACE` into the same physical-logical tracking model used for Files and Classes.
*   **Precision**: Prevents "invisible" changes to namespaces (e.g., adding a new configuration variable) from being missed by the summarizer.
