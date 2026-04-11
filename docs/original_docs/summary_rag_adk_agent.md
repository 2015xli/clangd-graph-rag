# Summary: `rag_adk_agent` - Example AI Coding Agent

## 1. High-Level Role

The `rag_adk_agent/` directory contains a complete, runnable example of an AI coding agent. This agent is built using the **Google Agent Development Kit (ADK)** and is designed to demonstrate the practical application of the Neo4j code graph.

Its purpose is to act as an expert software engineer that a user can interact with via a command-line interface. The agent intelligently uses the tools provided by the `graph_mcp_server.py` to answer complex questions about the codebase, such as "What does this function do?", "Where is this class used?", or "What is the impact of changing this method?".

## 2. Components

### `agent.py` - The Agent's Brain

*   **Framework**: This file defines a `MyAgent` class that inherits from `adk.Agent`.
*   **Persona & Instructions**: The agent is given a clear persona ("an expert software engineer") and a detailed set of instructions. These instructions form its core logic loop:
    1.  **Orient**: First, use the `get_graph_schema` and `get_project_info` tools to understand the available data and the project context.
    2.  **Query**: For a user's question, formulate and execute Cypher queries using the `execute_cypher_query` tool to find relevant nodes in the graph.
    3.  **Read**: Use the `get_source_code` tool to read the code of the specific nodes (functions, classes, etc.) found in the previous step.
    4.  **Synthesize**: Combine the information from the graph and the source code to generate a comprehensive, accurate answer for the user.
*   **Tool Consumption**: The agent is designed to be a "client." It does not define the tools itself but is initialized with a list of tools loaded from the MCP server.

### `run_agent.py` - Custom CLI Wrapper Example

*   **Purpose**: This script serves as a **custom command-line wrapper** that demonstrates how to programmatically load and run the agent. While the standard `adk` commands are recommended for general use, this script is a valuable example for developers who need more control over the agent's initialization and execution loop.
*   **Tool Loading**: On startup, it connects to the running `graph_mcp_server.py` instance and uses `adk.tools.mcp.load_tools_from_mcp_server()` to dynamically fetch the available tools.
*   **Agent Instantiation**: It injects the dynamically loaded tools into a new instance of `MyAgent`.
*   **Interactive Session**: It starts an interactive `while` loop, prompting the user for questions. It uses `adk.Terminal.display_response` to provide a rich user experience, streaming the agent's thought process and final answer directly to the console.

## 3. Running the Agent

To use the agent, you must first start the tool server, and then you can run the agent using one of the following methods.

### Step 1: Start the Tool Server

In one terminal, start the MCP server, which connects to the database and exposes the tools the agent needs.
```bash
python3 graph_mcp_server.py
```

### Step 2: Run the Agent

#### Recommended Method: Using the ADK CLI

The recommended way to run the agent is with the Google ADK command-line tools. This provides a standardized way to interact with the agent.

*   **For an interactive CLI session:**
    ```bash
    adk run rag_adk_agent
    ```
*   **For a web-based interface:**
    ```bash
    adk web
    ```

#### Alternative Method: Custom CLI Wrapper

For developers who want more control over the agent's execution or wish to see a programmatic example, the provided `run_agent.py` script can be used.

*   **Run the custom CLI:**
    ```bash
    python3 rag_adk_agent/run_agent.py
    ```
You can then start asking questions about your codebase in the prompt that appears.

This setup cleanly separates the agent's logic from the tools that access the project's data, showcasing a robust and modular agentic architecture.
