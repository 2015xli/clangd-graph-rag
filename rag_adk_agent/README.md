# RAG ADK Agent: Example AI Expert

This directory contains an example implementation of an AI coding agent built with the **Google Agent Development Kit (ADK)**. It demonstrates how to leverage the code graph for natural language software analysis.

## Components

### 1. The Agent's Brain (`agent.py`)
Defines the persona and instructions for the agent. It follows a multi-step reasoning loop:
1.  **Orient**: Uses tools to understand the graph schema and project context.
2.  **Query**: Formulates Cypher queries to find relevant nodes.
3.  **Read**: Retrieves the actual source code of candidate symbols.
4.  **Synthesize**: Combines graph metadata and code implementation to answer the user.

### 2. Custom Wrapper (`run_agent.py`)
A programmatic example of how to load tools from an MCP server and instantiate the agent. It provides a rich, streaming terminal interface.

---

## Running the Agent

### Step 1: Start the Tool Server
The agent depends on the `graph_mcp_server.py` to interact with Neo4j.
```bash
python3 graph_mcp_server.py
```

### Step 2: Launch the Agent
**Recommended (Standard ADK):**
```bash
# For interactive terminal
adk run rag_adk_agent

# For Web UI
adk web
```

**Alternative (Custom Wrapper):**
```bash
python3 rag_adk_agent/run_agent.py
```
