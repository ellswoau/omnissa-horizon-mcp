"""Tool definition functions for the Horizon MCP server.

Each ``register_*`` function wires fastmcp ``@tool`` decorators bound to a
resolved :class:`HorizonConfig`. Splitting imports keeps the server lean and
allows tests to build clients on demand.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from fastmcp import FastMCP
    from ..config import HorizonConfig

from . import connection_tools, machine_tools, pool_tools, session_tools, utilization_tools


def register_all(mcp: "FastMCP", config: "HorizonConfig") -> None:
    connection_tools.register(mcp, config)
    session_tools.register(mcp, config)
    utilization_tools.register(mcp, config)
    pool_tools.register(mcp, config)
    machine_tools.register(mcp, config)