import os,sys
import logging
import json
import re
from typing import Dict, Any, List, Optional

from fastmcp import FastMCP
from neo4j_manager import Neo4jManager
from llm_client import get_embedding_client

# --- Configuration and Initialization ---
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize FastMCP
mcp = FastMCP()

# Initialize Neo4jManager and PathManager
# These will be initialized once when the server starts
neo4j_mgr: Optional[Neo4jManager] = None
project_root_path: Optional[str] = None

# --- Helper Functions ---
def _initialize_managers():
    global neo4j_mgr, path_manager, project_root_path
    if neo4j_mgr is None:
        neo4j_mgr = Neo4jManager()
        if not neo4j_mgr.check_connection():
            logger.critical("Failed to connect to Neo4j. Exiting.")
            raise ConnectionError("Failed to connect to Neo4j.")
        
        # Discover project path from Neo4j
        query = "MATCH (p:PROJECT) RETURN p.path AS path"
        result = neo4j_mgr.execute_read_query(query)
        if result and result[0] and result[0].get('path'):
            project_root_path = result[0]['path']
            logger.info(f"Find the project root: {project_root_path}")
        else:
            logger.critical("Could not determine project root path from Neo4j. A PROJECT node with a 'path' property must exist.")
            raise ValueError("Project root path not found in Neo4j.")


def _read_file_slice(file_path: str, start_line: int, end_line: int) -> str:
    """Reads a specific line range from a file."""
    try:
        with open(file_path, 'r', errors='ignore') as f:
            lines = f.readlines()
        # Adjust for 0-based indexing
        code_lines = lines[start_line : end_line + 1]
        return "".join(code_lines)
    except Exception as e:
        logger.error(f"Error reading file {file_path} lines {start_line}-{end_line}: {e}")
        return ""

# --- FastMCP Tools ---

@mcp.tool(name="get_graph_schema", description="Retrieves the Neo4j graph schema to understand node properties and relationships.")
def get_graph_schema() -> str:
    """
    Retrieves the content of the neo4j_current_schema.txt file.
    """
    schema_file_path = os.path.join(os.path.dirname(__file__), "neo4j_current_schema.txt")
    schema_content = ""
    if os.path.isfile(schema_file_path):
        try:
            with open(schema_file_path, 'r') as f:
                schema_content = f.read()
        except Exception as e:
            logger.error(f"Error reading graph schema file: {e}")
            return f"Error: Could not read graph schema: {e}"
    else: 
        logger.info(f"Schema file not found at {schema_file_path}. Read schema from graph directly.")
        schema_content =  neo4j_mgr.format_schema_for_display(neo4j_mgr.get_schema())
        
    return schema_content

@mcp.tool(name="get_embedding_vector_indexes", description="Retrieves the Neo4j vector embedding indexes available for similarity search.")
def get_embedding_vector_indexes() -> str:
    """
    Retrieves the vector embedding indexes from the Neo4j database for semantic search.
    """
    vector_indexes = neo4j_mgr.get_vector_indexes()
    if vector_indexes:
        indexes_content = "The following embedding vector indexes are available for similarity search:\n"
        for index in vector_indexes:
            indexes_content += f"- {index['name']} on {index['labelsOrTypes']}\n"
        return indexes_content

    return "No embedding vector indexes found."


@mcp.tool(name="generate_embeddings", description="Generates vector embeddings for a query string to be used for semantic similarity search.")
def generate_embeddings(query: str) -> list[float]:
    """
    Generates vector embeddings for the given query string to be used for semantic similarity search.
    
    Args:
        query (str): The query string to embed.
        
    Returns:
        list[float]: A list of embedding vectors for the query.
    """
    embedding_client = get_embedding_client("local")
    embeddings = embedding_client.generate_embeddings([query], show_progress_bar=False)
    
    return embeddings[0] if embeddings else []

@mcp.tool(name="get_project_info", description="Retrieves the project's name, root path and its high-level summary.")
def get_project_info() -> Dict[str, str]:
    """
    Queries the Neo4j database for the project's name, root path and summary.
    """
    try:
        query = "MATCH (p:PROJECT) RETURN p.name AS name, p.path AS path, p.summary AS summary"
        result = neo4j_mgr.execute_read_query(query)
        if result and result[0]:
            return {"name": result[0].get('name', ''), 
                    "path": result[0].get('path', ''), 
                    "summary": result[0].get("summary") or "No project summary available."
            }

        return {"name": "", "path": "", "summary": "No project node found."}
    except Exception as e:
        logger.error(f"Error getting project info: {e}")
        return {"name": "", "path": "", "summary": f"Error: Could not retrieve project info: {e}"}

@mcp.tool(name="get_node_source_code_by_id", description="Retrieves the source code for a non-file node (function, class, etc.) by its ID.")
def get_node_source_code_by_id(node_id: str) -> Dict[str, str]:
    """
    Retrieves the source code for a given non-file node (function, class, etc.) by its node ID.
    """
    try:
        # Query for path and body_location using the 'path' property
        query = f"MATCH (n {{id: '{node_id}'}}) RETURN n.path AS path, n.body_location AS body_location"
        result = neo4j_mgr.execute_read_query(query)

        if not result or not result[0]:
            return {"id": node_id, "source_code": "Error: Entity not found in graph."}

        node_path = result[0].get('path')
        body_location = result[0].get('body_location')

        if not node_path:
            return {"id": node_id, "source_code": "Error: Entity has no associated file path."}
        
        # Construct absolute path
        abs_file_path = os.path.join(project_root_path, node_path)

        if not os.path.exists(abs_file_path):
            return {"id": node_id, "source_code": f"Error: File not found on disk: {abs_file_path}"}

        if body_location and len(body_location) == 4:
            start_line, _, end_line, _ = body_location
            source_code_content = _read_file_slice(abs_file_path, start_line, end_line)
        else:
            # If no body_location, read the entire file
            with open(abs_file_path, 'r', errors='ignore') as f:
                source_code_content = f.read()
        
        return {"id": node_id, "source_code": source_code_content}
    except Exception as e:
        logger.error(f"Error getting source code for node {node_id}: {e}")
        return {"id": node_id, "source_code": f"Error: Could not retrieve source code: {e}"}

@mcp.tool(name="get_file_source_code_by_path", description="Retrieves the source code for a specific file by its relative path.")
def get_file_source_code_by_path(file_path: str) -> Dict[str, str]:
    """
    Retrieves the source code for a given file path.
    """
    try:
        if not file_path:
            return {"path": file_path, "source_code": "Error: File path is empty."}
        
        # Construct absolute path
        abs_file_path = os.path.join(project_root_path, file_path)

        if not os.path.exists(abs_file_path):
            return {"path": file_path, "source_code": f"Error: File not found on disk: {abs_file_path}"}

        with open(abs_file_path, 'r', errors='ignore') as f:
            source_code_content = f.read()
        
        return {"path": file_path, "source_code": source_code_content}
    except Exception as e:
        logger.error(f"Error getting source code for file {file_path}: {e}")
        return {"path": file_path, "source_code": f"Error: Could not retrieve source code: {e}"}

@mcp.tool(name="execute_cypher_query", description="Executes a read-only Cypher query against the Neo4j graph and returns the results.")
def execute_cypher_query(query: str) -> Dict[str, Any]:
    """
    Executes a read-only Cypher query and returns the results.
    """
    # Safety check: ensure the query is read-only
    read_only_keywords = ['MATCH', 'OPTIONAL MATCH', 'WHERE', 'RETURN', 'UNWIND', 'CALL']
    if not any(re.search(r'\b' + keyword + r'\b', query, re.IGNORECASE) for keyword in read_only_keywords):
        return {"error": "Query must contain at least one read-only keyword (MATCH, OPTIONAL MATCH, WHERE, RETURN, UNWIND, CALL)."}

    write_keywords = ['CREATE', 'SET', 'DELETE', 'MERGE', 'REMOVE', 'DETACH']
    if any(re.search(r'\b' + keyword + r'\b', query, re.IGNORECASE) for keyword in write_keywords):
        return {"error": "Write operations (CREATE, SET, DELETE, MERGE, REMOVE, DETACH) are not allowed."}

    try:
        results = neo4j_mgr.execute_read_query(query)
        # Convert Record objects to dictionaries for JSON serialization
        converted_results = [dict(record) for record in results]
        return {"results": converted_results}
    except Exception as e:
        logger.error(f"Error executing Cypher query: {query} - {e}")
        return {"error": f"Could not execute query: {e}"}


# --- FastMCP Application ---
# The FastMCP app instance is automatically exposed as a FastAPI app.
# To run: uvicorn graph_mcp_server:mcp.app --reload --port 8000
# Or, if you want to run it directly from this script:
if __name__ == "__main__":
    # This block is for direct execution via `python graph_mcp_server.py`
    # It will start the uvicorn server.
    import uvicorn
    logger.info("Starting FastMCP server...")
    _initialize_managers()
    
    # FastMCP exposes its underlying FastAPI app as mcp.app
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8800)
    #uvicorn.run(mcp, host="0.0.0.0", port=8800)
    neo4j_mgr.close()
