import os
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class ProjectMixin:
    """Methods for project-level metadata and path verification."""

    def update_project_node(self, project_path: str, properties: Dict[str, Any]) -> None:
        """Finds or creates the PROJECT node and updates its properties."""
        with self.driver.session() as session:
            # Ensure the name is set if not already present
            if 'name' not in properties:
                properties['name'] = os.path.basename(project_path) or "Project"
            
            session.run(
                "MERGE (p:PROJECT {path: $path}) SET p += $properties",
                {"path": project_path, "properties": properties}
            )
    
    def get_graph_commit_hash(self, project_path: str) -> Optional[str]:
        """Fetches the commit_hash property from the PROJECT node."""
        query = "MATCH (p:PROJECT {path: $path}) RETURN p.commit_hash AS hash"
        result = self.execute_read_query(query, {"path": project_path})
        if result and result[0] and result[0].get('hash'):
            return result[0]['hash']
        return None

    def verify_project_path(self, project_path: str) -> bool:
        """Verifies that the project path in the graph matches the provided path."""
        query = "MATCH (p:PROJECT) RETURN p.path AS path"
        result = self.execute_read_query(query)

        if not result:
            logger.warning("No PROJECT node found in the graph. Skipping path verification, assuming new graph.")
            return True # Allow proceeding on an empty graph

        if len(result) > 1:
            logger.error(f"Multiple PROJECT nodes found in the graph. Aborting due to ambiguous state.")
            return False

        graph_project_path = result[0].get('path')
        if graph_project_path != project_path:
            logger.critical(f"Project path mismatch! Provided: '{project_path}', Graph contains: '{graph_project_path}'. Aborting.")
            return False

        logger.info("Project path verification successful.")
        return True
