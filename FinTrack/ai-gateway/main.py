from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager
import anthropic
import json
from mcp_client import MCPClient

mcp = MCPClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await mcp.discover_tools()
    print("✅ ai-gateway ready")
    yield


app = FastAPI(title="FinTrack AI Gateway", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message:     str
    user_id:     str
    api_key:     str           # BYOK — user's own Claude API key
    history:     list = []     # previous messages for multi-turn


class ChatResponse(BaseModel):
    reply:       str
    tools_used:  list[str]
    raw_results: list


SYSTEM_PROMPT = """You are FinTrack, a smart personal finance assistant for young Indians
who just started earning. You help them log expenses, track income, manage budgets,
and build good financial habits.

Be conversational, warm, and practical. Use ₹ for currency. When someone logs an expense,
give them useful context — are they on track? Should they be concerned? Any quick tip?

You have access to tools to log and retrieve financial data. Always use the appropriate
tool when the user mentions money, spending, income, or wants to see their data.

IMPORTANT — Budget check rule:
After EVERY call to log_expense, you MUST immediately call check_budget_on_expense
with the same category. Use the returned alert_level to shape your reply:
  - ok       → no budget mention needed, just confirm the expense
  - warning  → mention they've used a significant portion, show ₹ remaining and days left
  - danger   → stronger tone, tell them exactly how little is left daily
  - exceeded → tell them clearly they've gone over, suggest how to recover

Keep responses concise. No bullet-point overload. Talk like a knowledgeable friend,
not a financial textbook."""


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    # Discover tools fresh (in production, cache this)
    tools = await mcp.discover_tools()

    if not tools:
        raise HTTPException(status_code=503, detail="No MCP servers reachable")

    # Build message history
    messages = req.history + [{"role": "user", "content": req.message}]

    # Use the user's own API key (BYOK)
    client = anthropic.Anthropic(api_key=req.api_key)

    tools_used  = []
    raw_results = []

    # ── Agentic tool loop ──────────────────────────────────────────────────────
    # Claude may call multiple tools before giving a final reply.
    # We loop until Claude stops calling tools.

    while True:
        response = client.messages.create(
            model      = "claude-sonnet-4-6",
            max_tokens = 1024,
            system     = SYSTEM_PROMPT,
            tools      = tools,
            messages   = messages,
        )

        # If Claude is done (no more tool calls) → return final reply
        if response.stop_reason == "end_turn":
            final_text = next(
                (block.text for block in response.content if hasattr(block, "text")),
                "I processed your request."
            )
            return ChatResponse(
                reply       = final_text,
                tools_used  = tools_used,
                raw_results = raw_results,
            )

        # Claude wants to call tools
        if response.stop_reason == "tool_use":
            # Add Claude's response to history
            messages.append({
                "role":    "assistant",
                "content": response.content
            })

            # Execute each tool Claude requested
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_args = block.input

                    # Inject user_id automatically — user can't fake this
                    tool_args["user_id"] = req.user_id

                    print(f"🔧 Calling tool: {tool_name} with {tool_args}")
                    result = await mcp.execute_tool(tool_name, tool_args)

                    tools_used.append(tool_name)
                    raw_results.append(result)

                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     json.dumps(result),
                    })

            # Feed tool results back to Claude for next iteration
            messages.append({
                "role":    "user",
                "content": tool_results
            })

        else:
            # Unexpected stop reason
            break

    return ChatResponse(
        reply       = "Something unexpected happened. Please try again.",
        tools_used  = tools_used,
        raw_results = raw_results,
    )


@app.get("/health")
async def health():
    tools = await mcp.discover_tools()
    return {
        "status":      "ok",
        "tools_count": len(tools),
        "tools":       [t["name"] for t in tools]
    }