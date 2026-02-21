from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool import StreamableHTTPConnectionParams, MCPToolset
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types # For creating response content
from typing import Optional
from pprint import pprint
import os

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[1]))
from neo4j_manager import Neo4jManager

MCP_URL = "http://127.0.0.1:8800/mcp"
LLM_MODEL = LiteLlm(model="deepseek/deepseek-chat")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
#LLM_MODEL = LiteLlm(model="openai/gpt-4o")
#LLM_MODEL = LiteLlm(model="ollama/llama2")

def agent_guardrail(
    callback_context: CallbackContext, llm_request: LlmRequest) -> Optional[LlmResponse]:

    agent_name = callback_context.agent_name # Get the name of the agent whose model call is being intercepted
    if llm_request.contents:
        content = llm_request.contents[-1]
        if content.role == "user" and content.parts[0].text:
            if "shit" in content.parts[0].text.lower():
                print(f"{agent_name} Guardrail triggered. Here is the conversation so far:")
                for content in llm_request.contents:
                    pprint(content)

                return LlmResponse(
                    content=types.Content(
                        role="assistant", 
                        parts=[types.Part(text="I'm sorry, but I can't assist with that.")]
                    )
                )

    return None 

def sync_agent(): 
    connection_params = StreamableHTTPConnectionParams(url=MCP_URL)
    toolset = MCPToolset(connection_params=connection_params)

    # --- Dynamically build the instruction prompt ---
    with Neo4jManager() as neo4j_mgr:
        has_summary = neo4j_mgr.check_property_exists('summary', ['FUNCTION','ENTITY'])
        has_embeddings = neo4j_mgr.check_property_exists('summaryEmbedding', ['FUNCTION','ENTITY'])

    base_instruction = (
        "You are an expert software engineer helping developers analyze a C/C++ project."
        "All project info is in a Neo4j graph RAG that you can query with tools."
        "The graph basically starts from a root PROJECT node. It CONTAINS the nodes of FOLDER and FILE like a tree file system."
        "The FILE nodes then DEFINES nodes of FUNCTION, DATA_STRUCTURE, VARIABLES, etc."
        "For C++ project, the graph has additional node types like METHOD, CLASS_STRUCTURE, NAMESPACE,"
        "where a CLASS_STRUCTURE node INHERITS from other CLASS_STRUCTURE nodes, and their METHODS may be OVERIDDEN_BY other METHODS."
        "A FUNCTION or METHOD node CALLS other FUNCTION or METHOD nodes. A FILE node INCLUDES another FILE node that represents aheader file." 

        "\n## What you can do"
        "\nBased on the RAG and your expert knowledge, you can help in almost anything related to the project."
        "\n- Key features and modules"
        "\n- Architecture design and workflow"
        "\n- Code patterns and structures such as call chain and class relationships"
        "\n- Project organization in logical way (such as modules, classes) or physical way (such as folders, files)"
        
        "\n\nThese information are important in following tasks that you can help with:"
        "\n- Advices on code refactoring in both design and optimiozations"
        "\n- Feature implementation based on user requirements"
        "\n- Identification of the root cause of bugs or race conditions"
        "\n- Documentation of software design"

        "\n\n## Note 1: How to Start a Session"
        "\n- Always start by using the `get_project_info` and `get_graph_schema` tools. "
        "\n- The schema will show you the primary 'semantic' node labels (like `FILE`, `FUNCTION`, `CLASS_STRUCTURE`), their properties, and their relationships. "
        "\n- You can formulate your own queries based on the schema and then use the `execute_cypher_query` tool to execute them."
        "\n- Remember all label and relationship names are uppercase."

        "\n\n## Note 2: Core Properties & Labels"
        "\n- **Universal `id`**: Every node in the graph (FILE, FUNCTION, CLASS_STRUCTURE, etc.) has a globally unique `id` property that you can return from query like `MATCH(node:FUNCTION|METHOD) RETURN node.id`. "
        "      You can use this `id` to retrieve a node's specific details."
        "\n- **Semantic labels**: Nodes may have multiple labels, e.g., `['FUNCTION', 'ENTITY']`. For graph traversals, you MUST use the specific 'semantic' label (the one that is NOT 'ENTITY'). "
        "      If you are ever unsure, you can use the `get_semantic_label` tool with node.id to get the semantic label."
        "\n- **`path` property**: The project root path is stored in the `PROJECT` node's `path` property,  "
        "      while the `path` property of other nodes is relative to the project root."

        "\n\n## Note 3: How to Query the Graph"
        "\n- **Always use semantic labels**: for node matching, use `MATCH (f:FUNCTION|METHOD) or MATCH(c:CLASS_STRUCTURE)`, not `MATCH (e:ENTITY)` or `MATCH (n)`"
        "\n- **Always return specific properties**: when query for nodes, always return their specific properties, not just the nodes themselves."
        "\n    For example, when querying for a FUNCTION node, always return `node.id`, `node.name`, or `node.path`, etc., not just `node`."
        "\n    Another example, if you want to know the call path (i.e., call chain) from one function to another, you can return the path nodes with their properties, like below:\n"
        "         `MATCH p = (f:FUNCTION|METHOD {name: 'function_A'})-[:CALLS*]->(n:FUNCTION|METHOD {name: 'function_B'})`"
        "         `RETURN [node IN nodes(p) | {id: node.id, name: node.name}] AS call_path_nodes LIMIT 5`"
        "\n- **Control result size with constraints**: When appropriate, always include a constraint on result size in your Cypher queries." 
        "      Some often used keywords are like `LIMIT N` (to cap rows), `SKIP/OFFSET` (for paging), `DISTINCT` (to deduplicate)."
        "      When matching paths use path selectors like `SHORTEST k` or `ANY k` so that result sets stay bounded and manageable. An example below,\n"
        "          `MATCH p = SHORTEST 3 (f:FUNCTION|METHOD {name:'function_A'})-[:CALLS*]->(n:FUNCTION|METHOD {name:'function_B'}) RETURN [x IN nodes(p) | {id: x.id, name: x.name}]`\n"
    )

    source_code_instruction = (
        "\n\n## Note 4: How to Get Source Code"
        "\n- **Get source code with id**: After finding the `id` property of a node (e.g., FUNCTION, METHOD, DATA_STRUCTURE, FILE, etc.) through a query, use the `get_source_code_by_id` tool with the `id` property to read its source code."
        "\n    Note, not all nodes have source code (e.g., FOLDER, NAMESPACE nodes do not have source code), use your common sense to determine if the node has source code."
        "\n- **Get full file with path**: If you only want to get the full source code of a file (not just the code of a specific function, method, or data structure), you can use the `get_source_code_by_path` tool with the 'path' property."
    )

    keyword_search_instruction = (
     "\n\n## Note 5: How to Perform Searches"
        "\n- **Keyword match search with cypher query**: Use `STARTS WITH` or `CONTAINS` on properties like `name` or `path` or `summary` for keyword searches. "
        "\n    e.g., `MATCH (f:FILE) WHERE f.path CONTAINS 'utils' RETURN f.id LIMIT 5`"
        "\n- **Name search**: If you know the keyword is a name of a user defined types or macro symbols (e.g., struct, class, enum, typedef, using, macro, etc.), you can specifically search for type nodes." 
        "\n    e.g., `MATCH (t:TYPE_ALIAS|MACRO|DATA_STRUCTURE|CLASS_STRUCTURE) WHERE t.name = 'MyType' RETURN t.id, t.kind`"
        "\n    Then you can use the id to retrieve source code of the type's definition with `get_source_code_by_id` tool."
        "\n    If the returned result is a definition involving other unknown symbols (a macro definition uses other macros, a type alias definition uses other aliases), you may have to recursively search for them."
    )
    
    semantic_search_instruction = (
        "\n- **Semantic similarity search with tools**: To find nodes related to a concept, you should use the `search_nodes_for_semantic_similarity` tool."
        "\n    Example: `search_nodes_for_semantic_similarity(query='user authentication', num_results=5)`"
        "\n    For more advanced or custom queries, you can fall back to the lower-level tools using embeddings: "
        "\n    First use `generate_embeddings` to generate embeddings for the query, then formulate a cypher query with vector index of 'summary_embeddings', and then use `execute_cypher_query` to execute the query."
        "\n    An example cypher query with the generated embedding:\n"
        "         CALL db.index.vector.queryNodes('summary_embeddings', 20, embedding) " 
        "         YIELD node, score WHERE n:FUNCTION|METHOD "
        "         RETURN n.id, n.name, score "
        "         ORDER BY score DESC LIMIT 5;"
    )

    graph_summary_instruction = (
     "\n\n## Note 6: How to Quickly Understand Part Of or Whole Codebase and its Structure"
        "\n- **Get summary property when available**: To understand the codebase, the fastest way is to get the `summary` property of the nodes. It's the result of offline analysis when the graph is built."
        "\n    The graph is organized in a hierarchical structure where `PROJECT` -[:CONTAINS]-> `FOLDER`/`FILE`, `FILE` -[:DEFINES]-> `DATA_STRUCTURE/`CLASS_STRUCTURE`, which in turn -[:HAS_METHOD]->`FUNCTION`/`METHOD`. "
        "\n    For example, if you want to understand what all the files under a folder do, assuming the folder path is `relative/path/to/folder_name`, "
        "\n    then you can use `MATCH (n:FOLDER) WHERE n.name = 'folder_name' AND n.path = 'relative/path/to' RETURN n.summary` to get the folder's analysis summary"
        "\n    To understand more details about a project's high-level structure, you can match the PROJECT node's first-level children FOLDER/FILE nodes and get their summaries."
        "\n    Unless necessary, you don't need to go down to the hierarchy levels to understand the codebase."
        "\n    Note all the path property values are relative path to the project root, except the project's path property is absolute path of the project root."
        "\n    The summary property is not always available to all nodes. Only the nodes whose subtree has essential source code will have summary property."
    )

    final_instruction = base_instruction + source_code_instruction + keyword_search_instruction
    if has_embeddings:
        final_instruction += semantic_search_instruction
    if has_summary:
        final_instruction += graph_summary_instruction

    # --- End of dynamic instruction prompt build ---

    return LlmAgent(
        model=LLM_MODEL,
        name="Coding_Agent",
        instruction=final_instruction,
        tools=[toolset],
        output_key="last_response",
        before_model_callback=agent_guardrail,
    )

root_agent = sync_agent()
