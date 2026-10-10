"""Minimal stdio MCP server for the dsh spike: exposes exactly one tool, `echo`."""

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="hello")


@server.tool()
def echo(text: str) -> str:
    """Return the given text unchanged."""
    return text


if __name__ == "__main__":
    server.run(transport="stdio")
