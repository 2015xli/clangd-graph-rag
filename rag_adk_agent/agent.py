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

    return LlmAgent(
        model=LLM_MODEL,
        name="Coding_Agent",
        instruction=(
            "You are a software expert to help developers to analyze a software project."
            "All the info related to the project is in a neo4j graph RAG that you can query with mcp tools."
            "Based on the RAG and your expert knowledge, you can help in almost anything related to the project."
            "For example, you can get code related information combined with your knowledge, such as,"
            "- Key features and modules"
            "- Architecture design and workflow"
            "- Code patterns and structures such as call chain and class relationships"
            "- Project organization in logical way (such as modules, classes) or physical way (such as folders, files)"
            "These information are important in following tasks that you can help with:"
            "- Advices on code refactoring in both design and optimiozations"
            "- Identification of the root cause of bugs or race conditions"
            "- Documentation of software design"
            "- Feature implementation based on user requirements"
            "## Note 1: How to start a session with a user?"
            "To start a session with a user, you normally should use the mcp tools to get the project info first, " 
            "then get the graph schema to understand the node labels, properties and relationships."
            "Only after you get these two pieces of information, you can start to help the user."
            "Remember all the label/relationship names are in uppercase, e.g., FILE, FOLDER, FUNCTION, CALLS, etc."
            "## Note 2: How to get the source code of a file or a non-file entity?"
            "You cannot get the source code of a node by its name property."
            "### For source code of a file:"
            "You can always use the 'path' property to get the source code of a file." 
            "The 'path' property in the graph nodes is relative path, and you should use it directly. "
            "Don't try to craft an absolute path yourself, "
            "since the mcp tools will automatically convert the relative path to absolute path."
            "Btw, nodes of FILE and FOLDER do not have id property, while their path property is unique."
            "### For source code of a non-file node:"
            "For non-file nodes, such as FUNCTION, METHOD, CLASS_STRUCTURE, DATA_STRUCTURE, etc.,"
            "you should first get the 'id' property of the node, then use the id to get the node's source with a MCP tool."
            "## Note 3: How to search for content?"
            "For content search, you can use 'starts with' or 'contains' to search for relevant code or summary; or,"
            "you can do a semantic search first, which is usually more effective to get a quick result."
            "For semantic search, you can use the mcp tools to list all available embedding vector indexes, "
            "and use the embedding generation tool to create embeddings for the query text, "
            "then you can formulate a cypher query to perform similarity search, such as:"
            "     CALL db.index.vector.queryNodes($vector_index_name, 5, $query_embeddings)"
            "     YIELD node, score RETURN node.name, node.summary, score"
            "Of course, you can always use the graph query tools directly and combine with source code retrieval to provide comprehensive answers."
        ),
        tools=[toolset],
        output_key="last_response",
        before_model_callback=agent_guardrail,
    )

root_agent = sync_agent()
