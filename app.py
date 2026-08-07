"""
ASGI application for Yargı MCP Server (simple deployment variant).

This is a minimal ASGI application that can be run with:
    uvicorn app:app --host 0.0.0.0 --port 8000

The MCP server will be available at:
    http://localhost:8000/mcp/

For the FastAPI-wrapped variant with CORS and extra metadata routes,
see asgi_app.py instead.
"""

from health import register_health_routes
from mcp_server_main import create_app
from runtime_state import RuntimeTrackingMiddleware

mcp = create_app()
mcp.add_middleware(RuntimeTrackingMiddleware())
register_health_routes(mcp)

# Create ASGI app directly from FastMCP server
app = mcp.http_app()

# Endpoints:
# - /mcp/   - MCP server (Streamable HTTP transport, default FastMCP path)
# - /health - Any failed server or upstream dependency check returns 503
