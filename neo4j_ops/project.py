import os
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class ProjectMixin:
    """Methods for project-level metadata and path verification."""

    def update_project_node(self, project_path: str, properties: Dict[str, Any]) -> None:
        with self.driver.session() as session:
            if 'name' not in properties:
                properties['name'] = os.path.basename(project_path) or "Project"
            session.run(
                "MERGE (p:PROJECT {path: $path}) SET p += $properties",
                {"path": project_path, "properties": properties}
            )
    
    def get_graph_commit_hash(self, project_path: str) -> Optional[str]:
        query = "MATCH (p:PROJECT {path: $path}) RETURN p.commit_hash AS hash"
        result = self.execute_read_query(query, {"path": project_path})
        if result and result[0] and result[0].get('hash'):
            return result[0]['hash']
        return None

    def verify_project_path(self, project_path: str) -> bool:
        query = "MATCH (p:PROJECT) RETURN p.path AS path"
        result = self.execute_read_query(query)
        if not result:
            logger.warning("No PROJECT node found. Assuming new graph.")
            return True
        if len(result) > 1:
            logger.error("Multiple PROJECT nodes found. Ambiguous state.")
            return False
        graph_project_path = result[0].get('path')
        if graph_project_path != project_path:
            logger.critical(f"Project path mismatch! Graph: {graph_project_path}")
            return False
        logger.info("Project path verification successful.")
        return True
