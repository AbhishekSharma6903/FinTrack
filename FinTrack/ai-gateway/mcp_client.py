import httpx
from typing import Any


class MCPClient:
    """
    Talks to all MCP servers.
    Discovers their tools, routes execution calls to the right server.
    """

    def __init__(self):
        self.servers = {
            "expense-server": "http://expense-server:8001",
            "budget-server":  "http://budget-server:8002",   # Phase 2: cross-server tool chaining
            # Phase 3: "invest-server": "http://invest-server:8003",
        }
        self._tool_registry: dict[str, str] = {}  # tool_name → server_name

    async def discover_tools(self) -> list[dict]:
        """
        Calls /mcp/tools on every server and aggregates all tool definitions.
        Returns the list Claude receives as its tool definitions.
        """
        all_tools = []

        async with httpx.AsyncClient(timeout=10.0) as client:
            for server_name, base_url in self.servers.items():
                try:
                    response = await client.get(f"{base_url}/mcp/tools")
                    response.raise_for_status()
                    tools = response.json().get("tools", [])

                    for tool in tools:
                        self._tool_registry[tool["name"]] = server_name

                    all_tools.extend(tools)
                    print(f"✅ Discovered {len(tools)} tools from {server_name}")

                except Exception as e:
                    print(f"⚠️  Could not reach {server_name}: {e}")

        return all_tools

    async def execute_tool(self, tool_name: str, args: dict) -> Any:
        """
        Routes a tool call to the correct MCP server and returns the result.
        Called when Claude decides to use a tool.
        """
        server_name = self._tool_registry.get(tool_name)

        if not server_name:
            return {"error": f"No server registered for tool '{tool_name}'"}

        base_url = self.servers[server_name]

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(
                    f"{base_url}/mcp/execute",
                    json={"tool": tool_name, "args": args}
                )
                response.raise_for_status()
                return response.json()
            except Exception as e:
                return {"error": f"Tool execution failed: {str(e)}"}