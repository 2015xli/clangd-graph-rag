#!/usr/bin/env python3
"""
Neo4j Manager package for database operations, schema management, and maintenance.
"""

from .base import Neo4jBase
from .schema import SchemaMixin
from .purge import PurgeMixin
from .project import ProjectMixin

class Neo4jManager(Neo4jBase, SchemaMixin, PurgeMixin, ProjectMixin):
    """
    Unified manager for Neo4j database operations.
    Inherits modular functionality from specialized mixins.
    """
    pass

__all__ = ['Neo4jManager']
