import httpx
import asyncio
from typing import Any


class MCPClient:
    """
    Talks to all MCP servers.
    Discovers their tools, routes execution calls to the right server.
    """

    def __init__(self):
        self.servers = {
            "expense-server":  "http://expense-server:8001",
            "budget-server":   "http://budget-server:8002",
            "invest-server":   "http://invest-server:8003",
            "insights-server": "http://insights-server:8004",
            "goals-server":    "http://goals-server:8005",
        }
        self._tool_registry: dict[str, str] = {}

    async def discover_tools(self, retries: int = 5, delay: float = 3.0) -> list[dict]:
        """
        Calls /mcp/tools on every server and aggregates all tool definitions.

        Retries up to `retries` times with `delay` seconds between attempts.
        This handles the race condition where the gateway starts before MCP
        servers finish their own startup (table creation, etc.).
        """
        for attempt in range(1, retries + 1):
            all_tools = []
            self._tool_registry = {}   # reset registry on each attempt

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

            if len(all_tools) == len(self.servers) * 0 or all_tools:
                # At least some tools found — but warn if not all servers responded
                missing = [s for s in self.servers if not any(
                    self._tool_registry.get(t) == s for t in self._tool_registry
                )]
                if missing:
                    print(f"⚠️  Servers not yet ready: {missing}")
                    if attempt < retries:
                        print(f"🔄 Retrying in {delay}s (attempt {attempt}/{retries})...")
                        await asyncio.sleep(delay)
                        continue

            if all_tools:
                print(f"✅ Tool discovery complete — {len(all_tools)} tools from {len(self.servers)} servers")
                return all_tools

            if attempt < retries:
                print(f"🔄 No tools found, retrying in {delay}s (attempt {attempt}/{retries})...")
                await asyncio.sleep(delay)

        print("❌ Tool discovery failed after all retries")
        return all_tools

    async def execute_tool(self, tool_name: str, args: dict) -> Any:
        """
        Routes a tool call to the correct MCP server and returns the result.
        Called when the AI decides to use a tool.
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
